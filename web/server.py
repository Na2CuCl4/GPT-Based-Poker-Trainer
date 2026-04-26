"""Flask + Flask-SocketIO web server for the poker trainer."""
from __future__ import annotations

import json
import secrets
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path
from threading import Lock, Event

from flask import Flask, jsonify, request, render_template, session as flask_session
from flask_socketio import SocketIO

from ai import gpt_client
from ai.advisor import GPTAdvisor
from ai.opponent import GPTOpponent
from ai.schemas import HandAnalysis
from data import recorder, analyzer
from poker.game_engine import GameEngine
from poker.game_state import GameState

# ---------------------------------------------------------------------------
# Global game state (single session per server process)
# ---------------------------------------------------------------------------
_engine: GameEngine | None = None
_advisor: GPTAdvisor | None = None
_opponents: dict[int, GPTOpponent] = {}
_session_id: int | None = None
_current_hand_id: int | None = None
_config: dict = {}
_lock = Lock()
_pending_analysis: dict | None = None   # hand_data + hand_id awaiting user-triggered analysis

# Run-it-twice coordination
_rit_event: Event = Event()         # fired when human makes run-it-twice choice
_rit_human_choice: bool | None = None
_rit_pending: bool = False
_rit_lock = Lock()


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

    recorder.init_db()

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
        global _engine, _advisor, _opponents, _session_id, _current_hand_id
        with _lock:
            _engine = GameEngine(_config)
            _advisor = GPTAdvisor(show_styles=show_styles)
            _opponents = {
                p.idx: GPTOpponent(style=p.style)
                for p in _engine.state.players
                if not p.is_human
            }
            _session_id = recorder.create_session(_config)
            _current_hand_id = None

            state = _engine.start_hand()
            _current_hand_id = _start_hand_record(state)

            # If first actor is AI, kick off AI turns
            if not state.players[state.current_player_idx].is_human:
                socketio.start_background_task(_process_ai_turns, socketio)

        return jsonify({
            "session_id": _session_id,
            "state": _engine.get_state_snapshot(),
            "valid_actions": _engine.get_valid_actions_dict(),
        })

    @app.route("/api/game/action", methods=["POST"])
    @require_auth
    def api_action():
        if _engine is None:
            return jsonify({"error": "游戏未开始"}), 400
        data = request.get_json()
        action = data.get("action")
        amount = int(data.get("amount", 0))

        with _lock:
            current_p = _engine.state.players[_engine.state.current_player_idx]
            if not current_p.is_human:
                return jsonify({"error": "当前不是玩家回合"}), 400

            recorder.record_decision(
                _current_hand_id, current_p.name,
                _engine.state.street, action, amount,
            )

            result = _engine.apply_action(action, amount)

            if result["hand_over"]:
                return _handle_hand_over(result)

            if result.get("run_it_twice_prompt"):
                # Start background task to handle run-it-twice coordination
                socketio.start_background_task(_handle_run_it_twice_bg, socketio)
                return jsonify({
                    "state": _engine.get_state_snapshot(),
                    "valid_actions": [],
                    "run_it_twice_pending": True,
                    "result": result,
                })

            socketio.start_background_task(_process_ai_turns, socketio)

        return jsonify({
            "state": _engine.get_state_snapshot(),
            "valid_actions": _engine.get_valid_actions_dict(),
            "result": result,
        })

    @app.route("/api/game/run-it-twice", methods=["POST"])
    @require_auth
    def api_run_it_twice():
        """Receive human's run-it-twice choice and signal the waiting background task."""
        global _rit_human_choice
        if not _rit_pending:
            return jsonify({"error": "当前没有待处理的发牌决策"}), 400
        data = request.get_json()
        run_twice = bool(data.get("run_twice", False))
        with _rit_lock:
            _rit_human_choice = run_twice
        _rit_event.set()
        return jsonify({"ok": True})

    @app.route("/api/game/run-it-twice-hint", methods=["POST"])
    @require_auth
    def api_run_it_twice_hint():
        """Advisor hint for run-it-twice decision."""
        if _engine is None or _advisor is None:
            return jsonify({"error": "游戏未开始"}), 400
        state = _engine.state
        human = next((p for p in state.players if p.is_human), None)
        if human is None:
            return jsonify({"error": "找不到玩家"}), 400
        advice = _advisor.advise_run_it_twice(state, human)
        return jsonify(advice.model_dump())

    @app.route("/api/game/hint", methods=["POST"])
    @require_auth
    def api_hint():
        if _engine is None or _advisor is None:
            return jsonify({"error": "游戏未开始"}), 400
        state = _engine.state
        human = next((p for p in state.players if p.is_human), None)
        if human is None:
            return jsonify({"error": "找不到玩家"}), 400
        valid = _engine.get_valid_actions()
        hint = _advisor.get_hint(state, human, valid)
        return jsonify(hint.model_dump())

    @app.route("/api/game/next-hand", methods=["POST"])
    @require_auth
    def api_next_hand():
        global _current_hand_id
        if _engine is None:
            return jsonify({"error": "游戏未开始"}), 400
        with _lock:
            state = _engine.start_hand()
            _current_hand_id = _start_hand_record(state)
            if not state.players[state.current_player_idx].is_human:
                socketio.start_background_task(_process_ai_turns, socketio)
        return jsonify({
            "state": _engine.get_state_snapshot(),
            "valid_actions": _engine.get_valid_actions_dict(),
        })

    @app.route("/api/game/analyze", methods=["POST"])
    @require_auth
    def api_analyze():
        """User-triggered: start GPT hand analysis for the last completed hand."""
        if _pending_analysis is None:
            return jsonify({"error": "没有待分析的牌局"}), 400
        socketio.start_background_task(
            _run_analysis_bg,
            _pending_analysis["hand_data"],
            _pending_analysis["hand_id"],
        )
        return jsonify({"ok": True})

    @app.route("/api/stats", methods=["GET"])
    @require_auth
    def api_stats():
        if _session_id is None:
            return jsonify({"error": "游戏未开始"}), 400
        return jsonify(analyzer.full_stats(_session_id))

    # ------------------------------------------------------------------
    # WebSocket events
    # ------------------------------------------------------------------

    @socketio.on("connect")
    def on_connect():
        if auth_required and not flask_session.get("authenticated"):
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_hand_record(state: GameState) -> int:
        return recorder.record_hand(
            session_id=_session_id,
            hand_num=state.hand_number,
            pot=0,
            winner="",
            community_cards=[],
            result={},
        )

    def _finalize_hand(result: dict) -> dict:
        """
        1. Immediately emit hand_result (cards + winner) — no GPT wait.
        2. Update DB with basic result.
        3. Start background task for GPT analysis → emit hand_analysis when done.
        """
        state = _engine.state
        pot_results = result.get("side_pot_results", [])
        winner = pot_results[0]["winners"][0] if pot_results else ""

        community_cards_dicts = [c.to_dict() for c in state.community_cards]
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
        hand_id = _current_hand_id

        recorder.update_hand(
            hand_id=hand_id,
            pot=state.pot,
            winner=winner,
            community_cards=community_cards_dicts,
            result=result,
        )

        payload = {
            "state": _engine.get_state_snapshot(reveal_all=True),
            "result": result,
        }
        socketio.emit("hand_result", payload)

        # Store for user-triggered analysis (not auto-started)
        global _pending_analysis
        _pending_analysis = {"hand_data": hand_data, "hand_id": hand_id}
        return payload

    def _run_analysis_bg(hand_data: dict, hand_id: int) -> None:
        analysis: HandAnalysis = _advisor.analyze_hand(hand_data)
        analysis_dict = analysis.model_dump()
        recorder.update_hand_analysis(hand_id, analysis_dict)
        socketio.emit("hand_analysis", {"analysis": analysis_dict})

    def _handle_hand_over(result: dict):
        payload = _finalize_hand(result)
        return jsonify(payload)

    def _handle_run_it_twice_bg(sio) -> None:
        """Background task: coordinate run-it-twice decision, then run out the board."""
        global _rit_pending, _rit_human_choice

        rit_cfg = _config.get("features", {}).get("run_it_twice", False)
        if not rit_cfg:
            with _lock:
                result = _engine.runout(run_twice=False)
            _finalize_hand(result)
            return

        # Find AI opponent still in the hand
        state = _engine.state
        ai_player = next(
            (p for p in state.players if not p.is_human and not p.is_folded),
            None,
        )

        ai_run_twice = False
        ai_reasoning = "无法获取AI决策"

        if ai_player:
            opponent = _opponents.get(ai_player.idx)
            if opponent:
                try:
                    decision = opponent.decide_run_it_twice(state, ai_player)
                    ai_run_twice = decision.run_twice
                    ai_reasoning = decision.reasoning
                except Exception as e:
                    ai_reasoning = f"决策失败: {e}"

        with _rit_lock:
            _rit_pending = True
            _rit_human_choice = None

        # Notify frontend of AI decision and request human's choice
        sio.emit("run_it_twice_prompt", {
            "ai_run_twice": ai_run_twice,
            "ai_reasoning": ai_reasoning,
        })

        # Wait for human's choice (up to 120 seconds)
        _rit_event.clear()
        _rit_event.wait(timeout=120)

        with _rit_lock:
            human_choice = _rit_human_choice
            _rit_pending = False

        # Both must agree for run-twice
        final_run_twice = (human_choice is True) and ai_run_twice

        with _lock:
            result = _engine.runout(run_twice=final_run_twice)
        _finalize_hand(result)

    def _process_ai_turns(sio) -> None:
        delay = _config.get("ai", {}).get("response_delay", 1.5)
        while _engine is not None:
            state = _engine.state
            if state.street == "showdown":
                break
            current_p = state.players[state.current_player_idx]
            if current_p.is_human:
                break

            time.sleep(delay)

            with _lock:
                state = _engine.state
                if state.street == "showdown":
                    break
                current_p = state.players[state.current_player_idx]
                if current_p.is_human:
                    break

                opponent = _opponents.get(current_p.idx)
                if opponent is None:
                    break
                valid = _engine.get_valid_actions()
                decision = opponent.decide(state, current_p, valid)

                action = decision.action
                amount = decision.raise_to or 0

                # Validate action against valid_actions; substitute if GPT went off-script
                valid_names = {a.action for a in valid}
                if action not in valid_names:
                    call_opt = next((a for a in valid if a.action == "call"), None)
                    if call_opt:
                        action, amount = "call", call_opt.call_amount
                    elif any(a.action == "check" for a in valid):
                        action, amount = "check", 0
                    else:
                        action, amount = "fold", 0

                recorder.record_decision(
                    _current_hand_id, current_p.name,
                    state.street, action, amount,
                )

                sio.emit("ai_action", {
                    "player_name": current_p.name,
                    "action": action,
                    "amount": amount,
                    "reasoning": decision.reasoning,
                })

                result = _engine.apply_action(action, amount)

            # Handle run-it-twice outside the lock (it blocks waiting for human)
            if result.get("run_it_twice_prompt"):
                _handle_run_it_twice_bg(sio)
                break

            with _lock:
                if result["hand_over"]:
                    _finalize_hand(result)
                    break

                sio.emit("state_update", {
                    "state": _engine.get_state_snapshot(),
                    "valid_actions": _engine.get_valid_actions_dict(),
                    "street_changed": result.get("street_changed", False),
                })

    return app, socketio
