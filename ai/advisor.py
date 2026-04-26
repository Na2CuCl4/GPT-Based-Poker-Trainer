"""GPT-powered advisor for real-time hints and post-hand analysis."""
from __future__ import annotations

import json

from ai import gpt_client
from ai.schemas import HandAnalysis, HintRecommendation, RunItTwiceDecision
from poker.game_state import GameState, PlayerState
from poker.player import ActionOption

STYLE_CN = {
    "tight_aggressive": "紧凶（TAG）",
    "loose_aggressive": "松凶（LAG）",
    "tight_passive":    "紧弱（TP）",
    "loose_passive":    "松弱（LP）",
    "balanced":         "均衡（GTO）",
}

STREET_CN = {
    "preflop": "翻牌前（共5张公共牌，目前0张已发）",
    "flop":    "翻牌（共5张公共牌，目前3张已发）",
    "turn":    "转牌（共5张公共牌，目前4张已发）",
    "river":   "河牌（共5张公共牌，目前5张已发，不会再有新牌）",
}

_HINT_SYSTEM = """
你是一名专业的德州扑克教练，正在帮助玩家学习和提高。
请注意：
1. 当前处于哪条街（翻牌前/翻牌/转牌/河牌）非常关键——board 中的公共牌数量是固定的，不会再增加本条街的牌。
2. 如果当前是翻牌（3张公共牌），绝对不可以建议"听花色"这种需要第4或第5张公共牌的手牌策略，因为后续还有转牌和河牌，但此刻只有3张公共牌。
3. 如有对手的位置和风格信息，请结合位置和风格给出建议。
4. 请给出：推荐动作（fold/check/call/raise/all_in）、若加注则给出合理加注额、置信度（高/中/低）、简短中文说明（60字以内）、手牌强度描述、底池赔率或EV分析（一句话）。
"""

_ANALYSIS_SYSTEM = """
你是一名专业的德州扑克教练，正在帮助玩家分析刚结束的一手牌。
请结合以下信息给出深入分析：
- 玩家的手牌、行动记录（若有风格信息，也请结合风格分析）
- 公共牌和最终结果

【重要】：只评价 is_human=true 的玩家（即"你"）的决策，不要评价或提及 AI 对手的决策。
AI 对手由 GPT 模拟，其决策可能不符合最优策略，评价 AI 对手的决策会误导玩家。
key_decision_evals 中只包含玩家（is_human=true）自己的关键决策点。

请给出总体评分（0-100）、关键决策点评估、本局最重要的教训和2-3条改进建议。
请使用中文，语言简洁具体，避免泛泛而谈。
"""


def _get_positions(state: GameState) -> dict[int, str]:
    """Compute position label for each player based on dealer_idx."""
    n = len(state.players)
    d = state.dealer_idx
    if n == 2:
        labels = ["BTN/SB", "BB"]
    elif n == 3:
        labels = ["BTN", "SB", "BB"]
    elif n == 4:
        labels = ["BTN", "SB", "BB", "UTG"]
    elif n == 5:
        labels = ["BTN", "SB", "BB", "UTG", "CO"]
    elif n == 6:
        labels = ["BTN", "SB", "BB", "UTG", "HJ", "CO"]
    elif n == 7:
        labels = ["BTN", "SB", "BB", "UTG", "UTG+1", "HJ", "CO"]
    else:
        mid = [f"UTG+{i}" for i in range(n - 6)]
        labels = ["BTN", "SB", "BB", "UTG"] + mid + ["HJ", "CO"]
    return {(d + i) % n: labels[i] for i in range(n)}


def _build_hint_prompt(
    state: GameState,
    player: PlayerState,
    valid_actions: list[ActionOption],
    show_styles: bool = True,
) -> str:
    board = [str(c) for c in state.community_cards]
    hole = [str(c) for c in player.hole_cards]
    positions = _get_positions(state)
    my_position = positions.get(player.idx, "?")

    opponents = []
    for p in state.players:
        if p.idx == player.idx:
            continue
        entry: dict = {
            "name": p.name,
            "position": positions.get(p.idx, "?"),
            "chips": p.chips,
            "current_bet": p.current_bet,
            "is_folded": p.is_folded,
            "last_action": p.last_action,
        }
        if show_styles:
            entry["style"] = p.style
            entry["style_cn"] = STYLE_CN.get(p.style, p.style)
        opponents.append(entry)

    data = {
        "current_street": state.street,
        "street_description": STREET_CN.get(state.street, state.street),
        "board_cards": board,
        "board_cards_count": len(board),
        "pot": state.pot,
        "my_hand": hole,
        "my_position": my_position,
        "my_chips": player.chips,
        "my_current_bet": player.current_bet,
        "current_max_bet": state.current_max_bet,
        "opponents": opponents,
        "valid_actions": [a.to_dict() for a in valid_actions],
        "recent_log": state.hand_log[-10:],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _build_analysis_prompt(hand_result: dict) -> str:
    return json.dumps(hand_result, ensure_ascii=False, indent=2)


class GPTAdvisor:
    def __init__(self, show_styles: bool = True) -> None:
        self.show_styles = show_styles

    def get_hint(
        self,
        state: GameState,
        player: PlayerState,
        valid_actions: list[ActionOption],
    ) -> HintRecommendation:
        user_prompt = _build_hint_prompt(state, player, valid_actions, self.show_styles)
        try:
            return gpt_client.parse_response(
                system_prompt=_HINT_SYSTEM,
                user_prompt=user_prompt,
                schema=HintRecommendation,
            )
        except Exception as e:
            return HintRecommendation(
                action="check",
                confidence="低",
                explanation=f"无法获取建议: {e}",
                hand_strength_desc="未知",
                pot_odds_note="",
            )

    def analyze_hand(self, hand_result: dict) -> HandAnalysis:
        user_prompt = _build_analysis_prompt(hand_result)
        try:
            return gpt_client.parse_response(
                system_prompt=_ANALYSIS_SYSTEM,
                user_prompt=user_prompt,
                schema=HandAnalysis,
            )
        except Exception as e:
            return HandAnalysis(
                overall_score=0,
                summary=f"分析失败: {e}",
                key_decision_evals=[],
                main_lesson="",
                tips=[],
            )

    def advise_run_it_twice(
        self,
        state: GameState,
        player: PlayerState,
    ) -> RunItTwiceDecision:
        board = [str(c) for c in state.community_cards]
        hole = [str(c) for c in player.hole_cards]
        data = {
            "situation": "双方均已全押，询问是否应发两次",
            "my_hand": hole,
            "board_so_far": board,
            "pot": state.pot,
            "my_chips": player.chips,
        }
        system = (
            "你是德州扑克教练。玩家询问是否应选择发两次（Run It Twice）。"
            "请根据手牌强度和方差管理给出建议（发两次可降低方差）。"
            "reasoning 用中文说明（50字以内）。"
        )
        try:
            return gpt_client.parse_response(
                system_prompt=system,
                user_prompt=json.dumps(data, ensure_ascii=False),
                schema=RunItTwiceDecision,
            )
        except Exception as e:
            return RunItTwiceDecision(run_twice=False, reasoning=f"获取建议失败: {e}")
