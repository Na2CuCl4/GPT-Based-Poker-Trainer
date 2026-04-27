"""GPT-powered AI opponents with distinct playing styles."""
from __future__ import annotations

import json

from ai import gpt_client
from ai.schemas import OpponentDecision, RunItTwiceDecision
from poker.game_state import GameState, PlayerState
from poker.player import ActionOption

STYLE_PROMPTS: dict[str, str] = {
    "tight_aggressive": (
        "你是一名紧凶（Tight-Aggressive，TAG）风格的德州扑克玩家。"
        "你只在手牌较强时入池（翻牌前范围约15-20%），"
        "但一旦入池就会积极下注和加注，极少慢玩。"
        "面对弱牌或不利形势时坚决弃牌。"
    ),
    "loose_aggressive": (
        "你是一名松凶（Loose-Aggressive，LAG）风格的德州扑克玩家。"
        "你喜欢用较宽的范围入池（翻牌前范围约35-45%），"
        "并频繁下注、加注和诈唬，用主动性制造压力。"
        "善于在位置优势下打出攻势，对手难以读牌。"
    ),
    "tight_passive": (
        "你是一名紧弱（Tight-Passive，TP）风格的德州扑克玩家。"
        "只玩强牌（翻牌前范围约12-18%），但进入底池后倾向于跟注而非加注，"
        "很少主动下注，只有成了强牌才会跟注到底。"
    ),
    "loose_passive": (
        "你是一名松弱（Loose-Passive，LP）风格的德州扑克玩家。"
        "你喜欢玩很多手牌（翻牌前范围40%+），"
        "但进入底池后几乎只跟注，极少加注，容易被人读牌。"
    ),
    "balanced": (
        "你是一名均衡（Balanced/GTO）风格的德州扑克玩家。"
        "在不同情况下采取混合策略：既有紧凶时的坚定加注，"
        "也有慢玩和平衡范围的考量。目标是让对手难以剥削。"
    ),
}

SYSTEM_BASE = """
你是一名德州扑克 AI，需要根据当前游戏状态做出合理的决策。
你的决策必须符合以下约束：
1. 只能选择 valid_actions 中列出的动作。
2. 如果选择 raise，raise_to 必须在 [min_amount, max_amount] 范围内。
3. reasoning 请用中文简要说明（50字以内）。
"""


def _build_user_prompt(
    state: GameState,
    player: PlayerState,
    valid_actions: list[ActionOption],
) -> str:
    board = [str(c) for c in state.community_cards]
    hole = [str(c) for c in player.hole_cards]
    opponents = [
        {
            "name": p.name,
            "chips": p.chips,
            "current_bet": p.current_bet,
            "is_folded": p.is_folded,
            "is_all_in": p.is_all_in,
            "last_action": p.last_action,
        }
        for p in state.players
        if p.idx != player.idx
    ]
    actions_info = [a.to_dict() for a in valid_actions]
    recent_log = state.hand_log[-10:] if state.hand_log else []

    data = {
        "street": state.street,
        "pot": state.pot,
        "board": board,
        "my_hand": hole,
        "my_chips": player.chips,
        "my_current_bet": player.current_bet,
        "current_max_bet": state.current_max_bet,
        "min_raise": state.min_raise,
        "opponents": opponents,
        "valid_actions": actions_info,
        "recent_action_log": recent_log,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


class GPTOpponent:
    def __init__(self, style: str = "balanced") -> None:
        self.style = style
        self._system_prompt = SYSTEM_BASE + "\n" + STYLE_PROMPTS.get(style, STYLE_PROMPTS["balanced"])

    def decide(
        self,
        state: GameState,
        player: PlayerState,
        valid_actions: list[ActionOption],
        timeout: float = 30.0,
    ) -> OpponentDecision:
        user_prompt = _build_user_prompt(state, player, valid_actions)
        try:
            decision = gpt_client.parse_response(
                system_prompt=self._system_prompt,
                user_prompt=user_prompt,
                schema=OpponentDecision,
                timeout=timeout,
            )
        except TimeoutError:
            raise
        except Exception as e:
            # Fallback on non-timeout errors: call or check
            has_call = any(a.action == "call" for a in valid_actions)
            fallback = "call" if has_call else "check"
            decision = OpponentDecision(action=fallback, reasoning=f"GPT error: {e}")
        return decision

    def decide_run_it_twice(
        self,
        state: GameState,
        player: PlayerState,
        timeout: float = 30.0,
    ) -> RunItTwiceDecision:
        board = [str(c) for c in state.community_cards]
        hole = [str(c) for c in player.hole_cards]
        data = {
            "situation": "双方均已全押，是否同意发两次？",
            "my_hand": hole,
            "board_so_far": board,
            "pot": state.pot,
            "my_chips": player.chips,
        }
        system = (
            self._system_prompt
            + "\n你需要决定是否同意发两次（Run It Twice）。"
            "如果你胜率高则倾向发一次锁定胜局，如果形势不利则倾向发两次降低方差。"
            "reasoning 用中文说明（50字以内）。"
        )
        try:
            return gpt_client.parse_response(
                system_prompt=system,
                user_prompt=json.dumps(data, ensure_ascii=False),
                schema=RunItTwiceDecision,
                timeout=timeout,
            )
        except TimeoutError:
            raise
        except Exception as e:
            return RunItTwiceDecision(run_twice=False, reasoning=f"GPT error: {e}")
