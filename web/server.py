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
from poker.game_state import GameState

# ---------------------------------------------------------------------------
# Per-user game session
# ---------------------------------------------------------------------------
@dataclass
class GameSession:
    engine: GameEngine
    advisor: GPTAdvisor
    opponents: dict
    lock: Lock = field(default_factory=Lock)
    rit_event: Event = field(default_factory=Event)
    rit_human_choice: bool | None = None
    rit_pending: bool = False
    rit_lock: Lock = field(default_factory=Lock)
    pending_analysis: dict | None = None
    last_active: float = field(default_factory=time.time)


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

    # Config subset passed to frontend via template
    frontend_config = json.dumps({
        "show_opponent_styles": show_styles,
        "run_it_twice_enabled": run_it_twice_enabled,
        "starting_chips": starting_chips,
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
        return render_template("index.html", frontend_config=frontend_config, starting_chips=starting_chips)

    @app.route("/api/session/start", methods=["POST"])
    @require_auth
    def api_start_session():
        client_id = _get_client_id()
        if not client_id:
            return jsonify({"error": "缺少 X-Client-Id 请求头"}), 400

        data = request.get_json() or {}
        client_chips = data.get("starting_chips")
        user_config = {
            **_config,
            "table": {
                **_config.get("table", {}),
                "starting_chips": client_chips
                    if isinstance(client_chips, int) and client_chips > 0
                    else _config.get("table", {}).get("starting_chips", 1000),
            },
        }

        engine = GameEngine(user_config)
        advisor = GPTAdvisor(show_styles=show_styles)
        opponents = {
            p.idx: GPTOpponent(style=p.style)
            for p in engine.state.players
            if not p.is_human
        }
        session = GameSession(engine=engine, advisor=advisor, opponents=opponents)

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
        advice = session.advisor.advise_run_it_twice(state, human)
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
        hint = session.advisor.get_hint(state, human, valid)
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

        socketio.start_background_task(
            _run_analysis_bg,
            session.advisor,
            session.pending_analysis["hand_data"],
            client_id,
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
                    "style": p.style if show_styles else "未知",
                    "is_human": p.is_human,
                    "hole_cards": [str(c) for c in p.hole_cards],
                    "chips_after": p.chips,
                }
                for p in state.players
            ],
        }

        session.pending_analysis = {"hand_data": hand_data}

        payload = {
            "state": session.engine.get_state_snapshot(reveal_all=True),
            "result": result,
        }
        socketio.emit("hand_result", payload, room=client_id)
        return payload

    def _run_analysis_bg(advisor: GPTAdvisor, hand_data: dict, client_id: str) -> None:
        analysis: HandAnalysis = advisor.analyze_hand(hand_data)
        socketio.emit("hand_analysis", {"analysis": analysis.model_dump()}, room=client_id)

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

        state = session.engine.state
        ai_player = next(
            (p for p in state.players if not p.is_human and not p.is_folded),
            None,
        )

        ai_run_twice = False
        ai_reasoning = "无法获取AI决策"

        if ai_player:
            opponent = session.opponents.get(ai_player.idx)
            if opponent:
                try:
                    decision = opponent.decide_run_it_twice(state, ai_player)
                    ai_run_twice = decision.run_twice
                    ai_reasoning = decision.reasoning
                except Exception as e:
                    ai_reasoning = f"决策失败: {e}"

        with session.rit_lock:
            session.rit_pending = True
            session.rit_human_choice = None

        sio.emit("run_it_twice_prompt", {
            "ai_run_twice": ai_run_twice,
            "ai_reasoning": ai_reasoning,
        }, room=client_id)

        session.rit_event.clear()
        session.rit_event.wait(timeout=120)

        with session.rit_lock:
            human_choice = session.rit_human_choice
            session.rit_pending = False

        final_run_twice = (human_choice is True) and ai_run_twice

        with session.lock:
            result = session.engine.runout(run_twice=final_run_twice)
        _finalize_hand(session, client_id, result)

    def _process_ai_turns(sio, session: GameSession, client_id: str) -> None:
        delay = _config.get("ai", {}).get("response_delay", 1.5)
        while True:
            state = session.engine.state
            if state.street == "showdown":
                break
            current_p = state.players[state.current_player_idx]
            if current_p.is_human:
                break

            time.sleep(delay)

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
                decision = opponent.decide(state, current_p, valid)

                action = decision.action
                amount = decision.raise_to or 0

                valid_names = {a.action for a in valid}
                if action not in valid_names:
                    call_opt = next((a for a in valid if a.action == "call"), None)
                    if call_opt:
                        action, amount = "call", call_opt.call_amount
                    elif any(a.action == "check" for a in valid):
                        action, amount = "check", 0
                    else:
                        action, amount = "fold", 0

                sio.emit("ai_action", {
                    "player_name": current_p.name,
                    "action": action,
                    "amount": amount,
                    "reasoning": decision.reasoning,
                }, room=client_id)

                result = session.engine.apply_action(action, amount)

            if result.get("run_it_twice_prompt"):
                _handle_run_it_twice_bg(sio, session, client_id)
                break

            with session.lock:
                if result["hand_over"]:
                    _finalize_hand(session, client_id, result)
                    break

                sio.emit("state_update", {
                    "state": session.engine.get_state_snapshot(),
                    "valid_actions": session.engine.get_valid_actions_dict(),
                    "street_changed": result.get("street_changed", False),
                }, room=client_id)

    return app, socketio
