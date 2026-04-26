from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional

from poker.card import Card

Street = Literal["preflop", "flop", "turn", "river", "showdown"]


@dataclass
class PlayerState:
    idx: int
    name: str
    chips: int
    is_human: bool
    style: str = "balanced"           # AI style
    hole_cards: list[Card] = field(default_factory=list)
    current_bet: int = 0              # amount bet in current betting round
    total_bet: int = 0                # total bet in current hand (for side-pot calc)
    is_folded: bool = False
    is_all_in: bool = False
    last_action: str = ""             # fold/check/call/raise/all_in
    last_action_amount: int = 0
    acted_this_round: bool = False    # True once player voluntarily acts in current betting round
    chip_adjustment: int = 0         # cumulative: -N = N rebuys, +N = N cashouts
    chips_before_hand: int = 0       # chips at start of current hand (after adjustment)

    @property
    def is_active(self) -> bool:
        return not self.is_folded and not self.is_all_in

    def to_dict(self, reveal_cards: bool = False) -> dict:
        return {
            "idx": self.idx,
            "name": self.name,
            "chips": self.chips,
            "is_human": self.is_human,
            "style": self.style,
            "hole_cards": [c.to_dict() for c in self.hole_cards] if reveal_cards else (
                [c.to_dict() for c in self.hole_cards] if self.is_human else [None] * len(self.hole_cards)
            ),
            "current_bet": self.current_bet,
            "total_bet": self.total_bet,
            "is_folded": self.is_folded,
            "is_all_in": self.is_all_in,
            "last_action": self.last_action,
            "last_action_amount": self.last_action_amount,
            "chip_adjustment": self.chip_adjustment,
            "chips_before_hand": self.chips_before_hand,
        }


@dataclass
class GameState:
    street: Street = "preflop"
    pot: int = 0
    community_cards: list[Card] = field(default_factory=list)
    players: list[PlayerState] = field(default_factory=list)
    current_player_idx: int = 0
    dealer_idx: int = 0
    hand_number: int = 0
    small_blind: int = 10
    big_blind: int = 20
    ante: int = 0
    min_raise: int = 20              # minimum raise amount (= big blind initially)
    current_max_bet: int = 0         # highest bet in current round
    last_raiser_idx: int = -1        # to detect when action comes back around
    hand_log: list[dict] = field(default_factory=list)  # log of all actions this hand

    def log_action(self, player_name: str, action: str, amount: int = 0) -> None:
        self.hand_log.append({
            "street": self.street,
            "player": player_name,
            "action": action,
            "amount": amount,
        })

    def to_dict(self, reveal_all: bool = False) -> dict:
        return {
            "street": self.street,
            "pot": self.pot,
            "community_cards": [c.to_dict() for c in self.community_cards],
            "players": [p.to_dict(reveal_cards=reveal_all) for p in self.players],
            "current_player_idx": self.current_player_idx,
            "dealer_idx": self.dealer_idx,
            "hand_number": self.hand_number,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "ante": self.ante,
            "min_raise": self.min_raise,
            "current_max_bet": self.current_max_bet,
        }
