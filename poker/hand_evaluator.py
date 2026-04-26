from __future__ import annotations
from typing import TYPE_CHECKING

from treys import Card as TreysCard, Evaluator

if TYPE_CHECKING:
    from poker.card import Card

_evaluator = Evaluator()

# treys get_rank_class() returns 1-9 (not 1-10)
# 1=Straight Flush, 2=Quads, 3=Full House, 4=Flush, 5=Straight,
# 6=Trips, 7=Two Pair, 8=Pair, 9=High Card
RANK_CN = {
    1: "同花顺",
    2: "四条",
    3: "葫芦",
    4: "同花",
    5: "顺子",
    6: "三条",
    7: "两对",
    8: "一对",
    9: "高牌",
}


def _to_treys(card: "Card") -> int:
    return TreysCard.new(card.to_treys())


def evaluate(hole_cards: list["Card"], community_cards: list["Card"]) -> dict:
    """
    Evaluate a 5-7 card hand.
    Requires at least 3 community cards (flop+). Returns placeholder for preflop.
    Returns: {rank_int, class_int (1-10), class_name_cn, percentile}
    """
    if len(community_cards) < 3:
        return {
            "rank_int": 9999,
            "class_int": 10,
            "class_name": "未知（翻牌前）",
            "percentile": 0.0,
        }
    board = [_to_treys(c) for c in community_cards]
    hand = [_to_treys(c) for c in hole_cards]
    rank_int = _evaluator.evaluate(board, hand)
    class_int = _evaluator.get_rank_class(rank_int)
    percentile = round(1.0 - _evaluator.get_five_card_rank_percentage(rank_int), 4)
    # rank_int == 1 is the single best hand (Royal Flush A-K-Q-J-T suited)
    class_name = "皇家同花顺" if rank_int == 1 else RANK_CN.get(class_int, "未知")
    return {
        "rank_int": rank_int,     # lower is better (1 = best possible)
        "class_int": class_int,   # 1=SF … 9=high card
        "class_name": class_name,
        "percentile": percentile, # 0-1, higher = stronger
    }


def compare(result_a: dict, result_b: dict) -> int:
    """Return -1 if a < b (worse), 0 if tie, 1 if a > b (better)."""
    if result_a["rank_int"] < result_b["rank_int"]:
        return 1
    if result_a["rank_int"] > result_b["rank_int"]:
        return -1
    return 0
