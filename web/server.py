"""Flask + Flask-SocketIO web server for the poker trainer."""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from pathlib import Path
from threading import Lock, Event

from flask import Flask, jsonify, request, render_template, session as flask_session
from flask_socketio import SocketIO, join_room

from ai import gpt_client
from ai.advisor import GPTAdvisor
from ai.opponent import GPTOpponent
from ai.schemas import HandAnalysis
from poker.game_engine import GameEngine

# ---------------------------------------------------------------------------
# Per-user game session
# ---------------------------------------------------------------------------
@dataclass
class GameSession:
    engine: GameEngine
    advisor: GPTAdvisor
    opponents: dict
    show_styles: bool = True
    lock: Lock = field(default_factory=Lock)
    rit_event: Event = field(default_factory=Event)
    rit_human_choice: bool | None = None
    rit_pending: bool = False
    rit_lock: Lock = field(default_factory=Lock)
    rit_ai_run_twice: bool | None = None
    rit_ai_decided: bool = False
    rit_ai_retry_event: Event = field(default_factory=Event)
    pending_analysis: dict | None = None
    last_active: float = field(default_factory=time.time)
    ai_retry_pending: bool = False
    ai_processing: bool = False


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_sessions: dict[str, GameSession] = {}
_sessions_lock = Lock()
_config: dict = {}


def _get_secret_key() -> str:
    key_file = Path(__file__).parent.parent / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


def create_app(config: dict):
    global _config
    _config = config

    passwords: list = config.get("auth", {}).get("passwords", [])
    auth_required = bool(passwords)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = _get_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=365)
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    try:
        gpt_client.init(config)
    except EnvironmentError as e:
        print(f"[警告] {e}")

    show_styles = config.get("training", {}).get("show_opponent_styles", True)
    run_it_twice_enabled = config.get("features", {}).get("run_it_twice", False)
    starting_chips = config.get("table", {}).get("starting_chips", 1000)
    max_chips = config.get("table", {}).get("max_chips") or None

    # Config subset passed to frontend via template (used as initial/default values)
    frontend_config = json.dumps({
        "show_opponent_styles": show_styles,
        "run_it_twice_enabled": run_it_twice_enabled,
        "starting_chips":       starting_chips,
        "max_chips":            max_chips,
        "game_mode":            config.get("game",     {}).get("mode",              "cash"),
        "num_opponents":        config.get("table",    {}).get("num_opponents",     3),
        "small_blind":          config.get("blinds",   {}).get("small_blind",       10),
        "big_blind":            config.get("blinds",   {}).get("big_blind",         20),
        "ante":                 config.get("blinds",   {}).get("ante",              0),
        "hint_enabled":         config.get("training", {}).get("hint_enabled",      True),
        "post_hand_analysis":   config.get("training", {}).get("post_hand_analysis",True),
        "opponent_styles":      config.get("training", {}).get("opponent_styles",   ["random"]),
        "four_color_deck":      config.get("features", {}).get("four_color_deck",   True),
        "action_timeout":       config.get("ai", {}).get("action_timeout",          30),
        "hint_timeout":         config.get("ai", {}).get("hint_timeout",            20),
        "analysis_timeout":     config.get("ai", {}).get("analysis_timeout",        60),
    })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_client_id() -> str | None:
        return request.headers.get("X-Client-Id") or None

    def _get_session(client_id: str) -> GameSession | None:
        with _sessions_lock:
            s = _sessions.get(client_id)
            if s:
                s.last_active = time.time()
            return s

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def require_auth(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if auth_required and not flask_session.get("authenticated"):
                return jsonify({"error": "未授权"}), 401
            return f(*args, **kwargs)
        return decorated

    @app.route("/api/auth/status", methods=["GET"])
    def api_auth_status():
        if not auth_required:
            return jsonify({"authenticated": True, "required": False})
        return jsonify({
            "authenticated": bool(flask_session.get("authenticated")),
            "required": True,
        })

    @app.route("/api/auth", methods=["POST"])
    def api_auth():
        pwd = (request.get_json() or {}).get("password", "")
        if pwd in passwords:
            flask_session.permanent = True
            flask_session["authenticated"] = True
            return jsonify({"ok": True})
        return jsonify({"error": "密码错误"}), 401

    # ------------------------------------------------------------------
    # REST API
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template("index.html", frontend_config=frontend_config)

    def _ci(val, default, lo=None, hi=None):
        """Coerce to int with optional clamp; returns default on failure."""
        try:
            v = int(val)
            if lo is not None: v = max(lo, v)
            if hi is not None: v = min(hi, v)
            return v
        except (TypeError, ValueError):
            return default

    @app.route("/api/session/start", methods=["POST"])
    @require_auth
    def api_start_session():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400

        data = request.get_json() or {}
        bt  = _config.get("table",    {})
        bb  = _config.get("blinds",   {})
        btr = _config.get("training", {})
        bf  = _config.get("features", {})
        bg  = _config.get("game",     {})

        user_config = {
            **_config,
            "game": {
                **bg,
                "mode": data.get("game_mode", bg.get("mode", "cash")),
            },
            "table": {
                **bt,
                "num_opponents":  _ci(data.get("num_opponents"),  bt.get("num_opponents",  3), 2, 5),
                "starting_chips": _ci(data.get("starting_chips"), bt.get("starting_chips", 1000), 1),
                "max_chips":      _ci(data.get("max_chips"), 0) or None,
            },
            "blinds": {
                **bb,
                "small_blind": _ci(data.get("small_blind"), bb.get("small_blind", 10), 1),
                "big_blind":   _ci(data.get("big_blind"),   bb.get("big_blind",   20), 1),
                "ante":        _ci(data.get("ante"),        bb.get("ante",         0), 0),
            },
            "training": {
                **btr,
                "show_opponent_styles": bool(data.get("show_opponent_styles",
                                                       btr.get("show_opponent_styles", True))),
                "opponent_styles": data.get("opponent_styles")
                    if isinstance(data.get("opponent_styles"), list)
                    else btr.get("opponent_styles", ["random"]),
            },
            "features": {
                **bf,
                "run_it_twice": bool(data.get("run_it_twice", bf.get("run_it_twice", False))),
            },
        }
        user_show_styles = user_config["training"]["show_opponent_styles"]

        engine = GameEngine(user_config)
        advisor = GPTAdvisor(show_styles=user_show_styles)
        opponents = {
            p.idx: GPTOpponent(style=p.style)
            for p in engine.state.players
            if not p.is_human
        }
        session = GameSession(engine=engine, advisor=advisor, opponents=opponents,
                              show_styles=user_show_styles)

        with _sessions_lock:
            _sessions[client_id] = session

        with session.lock:
            state = engine.start_hand()
            if not state.players[state.current_player_idx].is_human:
                socketio.start_background_task(_process_ai_turns, socketio, session, client_id)

        return jsonify({
            "state": engine.get_state_snapshot(),
            "valid_actions": engine.get_valid_actions_dict(),
        })

    @app.route("/api/session/chips", methods=["POST"])
    @require_auth
    def api_adjust_chips():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        with _sessions_lock:
            session = _sessions.get(client_id)
        if not session:
            return jsonify({"error": "没有活跃的游戏会话"}), 400

        data = request.get_json() or {}
        with session.lock:
            players = session.engine.state.players
            if (pc := _ci(data.get("player_chips"), 0, 1)) > 0:
                human = next((p for p in players if p.is_human), None)
                if human:
                    human.chips = pc
            ai_players = [p for p in players if not p.is_human]
            for i, val in enumerate(data.get("opponent_chips") or []):
                if i < len(ai_players):
                    if (oc := _ci(val, 0, 1)) > 0:
                        ai_players[i].chips = oc

        return jsonify({"state": session.engine.get_state_snapshot()})

    @app.route("/api/game/action", methods=["POST"])
    @require_auth
    def api_action():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400

        data = request.get_json()
        action = data.get("action")
        amount = int(data.get("amount", 0))

        with session.lock:
            current_p = session.engine.state.players[session.engine.state.current_player_idx]
            if not current_p.is_human:
                return jsonify({"error": "当前不是玩家回合"}), 400

            result = session.engine.apply_action(action, amount)

            if result["hand_over"]:
                return _handle_hand_over(session, client_id, result)

            if result.get("run_it_twice_prompt"):
                socketio.start_background_task(_handle_run_it_twice_bg, socketio, session, client_id)
                return jsonify({
                    "state": session.engine.get_state_snapshot(),
                    "valid_actions": [],
                    "run_it_twice_pending": True,
                    "result": result,
                })

            if result.get("all_in_runout"):
                socketio.start_background_task(_handle_allin_runout_bg, socketio, session, client_id)
                return jsonify({
                    "state": session.engine.get_state_snapshot(),
                    "valid_actions": [],
                    "result": result,
                })

            socketio.start_background_task(_process_ai_turns, socketio, session, client_id)

        return jsonify({
            "state": session.engine.get_state_snapshot(),
            "valid_actions": session.engine.get_valid_actions_dict(),
            "result": result,
        })

    @app.route("/api/game/run-it-twice", methods=["POST"])
    @require_auth
    def api_run_it_twice():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400
        if not session.rit_pending:
            return jsonify({"error": "当前没有待处理的发牌决策"}), 400

        data = request.get_json()
        run_twice = bool(data.get("run_twice", False))
        with session.rit_lock:
            session.rit_human_choice = run_twice
        session.rit_event.set()
        return jsonify({"ok": True})

    @app.route("/api/game/retry-ai", methods=["POST"])
    @require_auth
    def api_retry_ai():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400
        if not session.ai_retry_pending:
            return jsonify({"error": "无待重试的 AI 操作"}), 400
        session.ai_retry_pending = False
        socketio.start_background_task(_process_ai_turns, socketio, session, client_id)
        return jsonify({"ok": True})

    @app.route("/api/game/retry-rit-ai", methods=["POST"])
    @require_auth
    def api_retry_rit_ai():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400
        # AI vs AI scenario: wake up the waiting retry loop
        session.rit_ai_retry_event.set()
        # Human vs AI scenario (rit_pending): spawn a fresh retry task
        if session.rit_pending:
            socketio.start_background_task(_retry_ai_rit_decision_bg, socketio, session, client_id)
        return jsonify({"ok": True})

    @app.route("/api/game/run-it-twice-hint", methods=["POST"])
    @require_auth
    def api_run_it_twice_hint():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400

        state = session.engine.state
        human = next((p for p in state.players if p.is_human), None)
        if human is None:
            return jsonify({"error": "找不到玩家"}), 400
        hint_timeout = _config.get("ai", {}).get("hint_timeout", 20)
        try:
            advice = session.advisor.advise_run_it_twice(state, human, timeout=hint_timeout)
        except TimeoutError:
            return jsonify({"error": "获取建议超时，请重试", "is_timeout": True}), 504
        return jsonify(advice.model_dump())

    @app.route("/api/game/hint", methods=["POST"])
    @require_auth
    def api_hint():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400

        state = session.engine.state
        human = next((p for p in state.players if p.is_human), None)
        if human is None:
            return jsonify({"error": "找不到玩家"}), 400
        valid = session.engine.get_valid_actions()
        hint_timeout = _config.get("ai", {}).get("hint_timeout", 20)
        try:
            hint = session.advisor.get_hint(state, human, valid, timeout=hint_timeout)
        except TimeoutError:
            return jsonify({"error": "获取建议超时，请重试", "is_timeout": True}), 504
        return jsonify(hint.model_dump())

    @app.route("/api/game/next-hand", methods=["POST"])
    @require_auth
    def api_next_hand():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400

        with session.lock:
            state = session.engine.start_hand()
            if not state.players[state.current_player_idx].is_human:
                socketio.start_background_task(_process_ai_turns, socketio, session, client_id)

        return jsonify({
            "state": session.engine.get_state_snapshot(),
            "valid_actions": session.engine.get_valid_actions_dict(),
        })

    @app.route("/api/game/analyze", methods=["POST"])
    @require_auth
    def api_analyze():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400
        session = _get_session(client_id)
        if session is None:
            return jsonify({"error": "游戏未开始"}), 400
        if session.pending_analysis is None:
            return jsonify({"error": "没有待分析的牌局"}), 400

        analysis_timeout = _config.get("ai", {}).get("analysis_timeout", 60)
        socketio.start_background_task(
            _run_analysis_bg,
            session.advisor,
            session.pending_analysis["hand_data"],
            client_id,
            analysis_timeout,
        )
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # WebSocket events
    # ------------------------------------------------------------------

    @socketio.on("connect")
    def on_connect():
        if auth_required and not flask_session.get("authenticated"):
            return False

    @socketio.on("join_session")
    def on_join_session(data):
        client_id = data.get("client_id", "")
        if client_id:
            join_room(client_id)

    # ------------------------------------------------------------------
    # TTL cleanup background task
    # ------------------------------------------------------------------

    def _cleanup_sessions_bg():
        while True:
            time.sleep(1800)
            now = time.time()
            with _sessions_lock:
                stale = [k for k, v in _sessions.items() if now - v.last_active > 7200]
                for k in stale:
                    del _sessions[k]

    socketio.start_background_task(_cleanup_sessions_bg)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finalize_hand(session: GameSession, client_id: str, result: dict) -> dict:
        state = session.engine.state

        # Capture natural post-hand chips before rebuy/cashout (for log net display)
        chips_end = {p.name: p.chips for p in state.players}

        # Apply rebuy/cashout — mutates p.chips and p.chip_adjustment
        session.engine.apply_rebuy_cashout()

        pot_results = result.get("side_pot_results", [])

        hand_data = {
            "hand_log": result.get("hand_log", []),
            "community_cards": [str(c) for c in state.community_cards],
            "reveal": result.get("reveal", {}),
            "pot_results": pot_results,
            "hand_number": state.hand_number,
            "run_twice": result.get("run_twice", False),
            "run_1_community": result.get("run_1_community"),
            "run_2_community": result.get("run_2_community"),
            "run_1_results": result.get("run_1_results"),
            "run_2_results": result.get("run_2_results"),
            "players_info": [
                {
                    "name": p.name,
                    "style": p.style if session.show_styles else "未知",
                    "is_human": p.is_human,
                    "hole_cards": [str(c) for c in p.hole_cards],
                    "chips_after": chips_end[p.name],
                }
                for p in state.players
            ],
        }

        session.pending_analysis = {"hand_data": hand_data}

        payload = {
            "state": session.engine.get_state_snapshot(reveal_all=True),
            "result": {**result, "chips_end": chips_end},
        }
        socketio.emit("hand_result", payload, room=client_id)
        return payload

    def _run_analysis_bg(advisor: GPTAdvisor, hand_data: dict, client_id: str, timeout: float = 60.0) -> None:
        try:
            analysis: HandAnalysis = advisor.analyze_hand(hand_data, timeout=timeout)
            socketio.emit("hand_analysis", {"analysis": analysis.model_dump()}, room=client_id)
        except TimeoutError:
            socketio.emit("hand_analysis_failed", {}, room=client_id)

    def _handle_hand_over(session: GameSession, client_id: str, result: dict):
        payload = _finalize_hand(session, client_id, result)
        return jsonify(payload)

    def _handle_run_it_twice_bg(sio, session: GameSession, client_id: str) -> None:
        rit_cfg = _config.get("features", {}).get("run_it_twice", False)
        if not rit_cfg:
            with session.lock:
                result = session.engine.runout(run_twice=False)
            _finalize_hand(session, client_id, result)
            return

        action_timeout = _config.get("ai", {}).get("action_timeout", 30)
        state = session.engine.state
        human = next((p for p in state.players if p.is_human), None)
        human_in_hand = human is not None and not human.is_folded

        # ── AI vs AI (human already folded) ──────────────────────────────
        if not human_in_hand:
            ai_run_twice = True
            for p in state.players:
                if p.is_human or p.is_folded:
                    continue
                opponent = session.opponents.get(p.idx)
                if opponent is None:
                    continue
                sio.emit("ai_thinking", {"player_name": p.name}, room=client_id)
                while True:
                    session.rit_ai_retry_event.clear()
                    try:
                        decision = opponent.decide_run_it_twice(state, p, timeout=action_timeout)
                        sio.emit("ai_action", {
                            "player_name": p.name,
                            "action": "run_twice_decision",
                            "amount": 0,
                            "reasoning": decision.reasoning,
                            "run_twice": decision.run_twice,
                        }, room=client_id)
                        ai_run_twice = ai_run_twice and decision.run_twice
                        break
                    except TimeoutError:
                        sio.emit("ai_action_failed", {
                            "player_name": p.name,
                            "player_idx": p.idx,
                            "retry_type": "rit",
                        }, room=client_id)
                        signalled = session.rit_ai_retry_event.wait(timeout=300)
                        if not signalled:
                            ai_run_twice = False
                            break
                    except Exception as e:
                        sio.emit("ai_action", {
                            "player_name": p.name,
                            "action": "run_twice_decision",
                            "amount": 0,
                            "reasoning": f"决策出错: {e}",
                            "run_twice": False,
                        }, room=client_id)
                        ai_run_twice = False
                        break

            with session.lock:
                result = session.engine.runout(run_twice=ai_run_twice)
            _finalize_hand(session, client_id, result)
            return

        # ── Human vs AI (human is all-in) ────────────────────────────────
        # Show dialog immediately, then get AI decision
        with session.rit_lock:
            session.rit_pending = True
            session.rit_human_choice = None
            session.rit_ai_decided = False
            session.rit_ai_run_twice = None

        sio.emit("run_it_twice_prompt", {"ai_pending": True}, room=client_id)

        ai_player = next(
            (p for p in state.players if not p.is_human and not p.is_folded),
            None,
        )
        if ai_player:
            opponent = session.opponents.get(ai_player.idx)
            if opponent:
                sio.emit("ai_thinking", {"player_name": ai_player.name}, room=client_id)
                try:
                    decision = opponent.decide_run_it_twice(state, ai_player, timeout=action_timeout)
                    with session.rit_lock:
                        session.rit_ai_run_twice = decision.run_twice
                        session.rit_ai_decided = True
                    sio.emit("run_it_twice_ai_result", {
                        "ai_run_twice": decision.run_twice,
                        "ai_reasoning": decision.reasoning,
                    }, room=client_id)
                except TimeoutError:
                    sio.emit("run_it_twice_ai_failed", {}, room=client_id)
                except Exception as e:
                    with session.rit_lock:
                        session.rit_ai_run_twice = False
                        session.rit_ai_decided = True
                    sio.emit("run_it_twice_ai_result", {
                        "ai_run_twice": False,
                        "ai_reasoning": f"决策出错: {e}",
                    }, room=client_id)
        else:
            with session.rit_lock:
                session.rit_ai_decided = True
                session.rit_ai_run_twice = False

        session.rit_event.clear()
        session.rit_event.wait(timeout=120)

        with session.rit_lock:
            human_choice = session.rit_human_choice
            ai_run_twice = session.rit_ai_run_twice if session.rit_ai_decided else False
            session.rit_pending = False

        final_run_twice = (human_choice is True) and (ai_run_twice is True)

        with session.lock:
            result = session.engine.runout(run_twice=final_run_twice)
        _finalize_hand(session, client_id, result)

    def _retry_ai_rit_decision_bg(sio, session: GameSession, client_id: str) -> None:
        """Retry AI run-it-twice decision for human-vs-AI scenario."""
        action_timeout = _config.get("ai", {}).get("action_timeout", 30)
        state = session.engine.state
        ai_player = next(
            (p for p in state.players if not p.is_human and not p.is_folded),
            None,
        )
        if ai_player is None:
            return
        opponent = session.opponents.get(ai_player.idx)
        if opponent is None:
            return
        try:
            decision = opponent.decide_run_it_twice(state, ai_player, timeout=action_timeout)
            with session.rit_lock:
                session.rit_ai_run_twice = decision.run_twice
                session.rit_ai_decided = True
            sio.emit("run_it_twice_ai_result", {
                "ai_run_twice": decision.run_twice,
                "ai_reasoning": decision.reasoning,
            }, room=client_id)
        except TimeoutError:
            sio.emit("run_it_twice_ai_failed", {}, room=client_id)

    def _handle_allin_runout_bg(sio, session: GameSession, client_id: str) -> None:
        """Step-by-step all-in runout: deal one street at a time with 3s pauses."""
        while True:
            with session.lock:
                step_result = session.engine.step_runout()
                snap = session.engine.get_state_snapshot()

            snap["current_player_idx"] = -1
            sio.emit("state_update", {
                "state": snap,
                "valid_actions": [],
                "street_changed": True,
            }, room=client_id)

            time.sleep(3)

            if step_result["done"]:
                with session.lock:
                    result = session.engine.settle_runout()
                _finalize_hand(session, client_id, result)
                return

    def _process_ai_turns(sio, session: GameSession, client_id: str) -> None:
        # Guard: only one _process_ai_turns task at a time per session
        with session.lock:
            if session.ai_processing:
                return
            session.ai_processing = True

        delay = _config.get("ai", {}).get("response_delay", 1.5)
        action_timeout = _config.get("ai", {}).get("action_timeout", 30)

        try:
            while True:
                # ── 1. Read state (outside lock is fine for a quick peek) ──
                state = session.engine.state
                if state.street == "showdown":
                    break
                current_p = state.players[state.current_player_idx]
                if current_p.is_human:
                    break

                time.sleep(delay)

                # ── 2. Re-read inside lock, capture what we need ──────────
                with session.lock:
                    state = session.engine.state
                    if state.street == "showdown":
                        break
                    current_p = state.players[state.current_player_idx]
                    if current_p.is_human:
                        break
                    opponent = session.opponents.get(current_p.idx)
                    if opponent is None:
                        break
                    valid = session.engine.get_valid_actions()
                    player_idx = current_p.idx
                    player_name = current_p.name

                # ── 3. GPT call — lock is released ────────────────────────
                sio.emit("ai_thinking", {"player_name": player_name}, room=client_id)
                try:
                    decision = opponent.decide(state, current_p, valid, timeout=action_timeout)
                except TimeoutError:
                    session.ai_retry_pending = True
                    sio.emit("ai_action_failed", {
                        "player_name": player_name,
                        "player_idx": player_idx,
                    }, room=client_id)
                    return

                action = decision.action
                amount = decision.raise_to or 0

                # ── 4. Apply action (inside lock, re-validate player idx) ──
                with session.lock:
                    state = session.engine.state
                    if state.street == "showdown":
                        break
                    current_p_now = state.players[state.current_player_idx]
                    if current_p_now.idx != player_idx:
                        break

                    valid_now = session.engine.get_valid_actions()
                    valid_names = {a.action for a in valid_now}
                    if action not in valid_names:
                        call_opt = next((a for a in valid_now if a.action == "call"), None)
                        if call_opt:
                            action, amount = "call", call_opt.call_amount
                        elif any(a.action == "check" for a in valid_now):
                            action, amount = "check", 0
                        else:
                            action, amount = "fold", 0

                    sio.emit("ai_action", {
                        "player_name": player_name,
                        "action": action,
                        "amount": amount,
                        "reasoning": decision.reasoning,
                    }, room=client_id)

                    result = session.engine.apply_action(action, amount)

                if result.get("run_it_twice_prompt"):
                    _handle_run_it_twice_bg(sio, session, client_id)
                    return

                if result.get("all_in_runout"):
                    _handle_allin_runout_bg(sio, session, client_id)
                    return

                street_changed = result.get("street_changed", False)

                with session.lock:
                    if result["hand_over"]:
                        _finalize_hand(session, client_id, result)
                        return

                    if street_changed:
                        snap = session.engine.get_state_snapshot()
                        snap["current_player_idx"] = -1
                        sio.emit("state_update", {
                            "state": snap,
                            "valid_actions": [],
                            "street_changed": True,
                        }, room=client_id)

                if street_changed:
                    time.sleep(3)

                with session.lock:
                    sio.emit("state_update", {
                        "state": session.engine.get_state_snapshot(),
                        "valid_actions": session.engine.get_valid_actions_dict(),
                        "street_changed": street_changed,
                    }, room=client_id)
        finally:
            with session.lock:
                session.ai_processing = False

    return app, socketio
