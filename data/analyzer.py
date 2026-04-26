"""Statistical analysis of player performance."""
from __future__ import annotations

from data import recorder


def calc_win_rate(session_id: int, player_name: str = "你") -> dict:
    hands = recorder.get_session_hands(session_id)
    if not hands:
        return {"win_rate": 0.0, "hands_played": 0, "hands_won": 0}
    won = sum(1 for h in hands if h.get("winner") == player_name)
    return {
        "win_rate": round(won / len(hands), 4),
        "hands_played": len(hands),
        "hands_won": won,
    }


def calc_vpip(session_id: int, player_name: str = "你") -> dict:
    """VPIP: Voluntarily Put money In Pot (preflop call/raise rate)."""
    hands = recorder.get_session_hands(session_id)
    if not hands:
        return {"vpip": 0.0}
    voluntary = 0
    total = 0
    for h in hands:
        decisions = recorder.get_hand_decisions(h["id"])
        preflop_actions = [
            d for d in decisions
            if d["player"] == player_name and d["street"] == "preflop"
        ]
        if preflop_actions:
            total += 1
            for d in preflop_actions:
                if d["action"] in ("call", "raise", "all_in"):
                    voluntary += 1
                    break
    return {"vpip": round(voluntary / total, 4) if total else 0.0}


def calc_pfr(session_id: int, player_name: str = "你") -> dict:
    """PFR: Pre-Flop Raise rate."""
    hands = recorder.get_session_hands(session_id)
    if not hands:
        return {"pfr": 0.0}
    raised = 0
    total = 0
    for h in hands:
        decisions = recorder.get_hand_decisions(h["id"])
        preflop_actions = [
            d for d in decisions
            if d["player"] == player_name and d["street"] == "preflop"
        ]
        if preflop_actions:
            total += 1
            for d in preflop_actions:
                if d["action"] in ("raise", "all_in"):
                    raised += 1
                    break
    return {"pfr": round(raised / total, 4) if total else 0.0}


def full_stats(session_id: int, player_name: str = "你") -> dict:
    return {
        **calc_win_rate(session_id, player_name),
        **calc_vpip(session_id, player_name),
        **calc_pfr(session_id, player_name),
    }
