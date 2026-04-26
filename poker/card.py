from __future__ import annotations
import random
from dataclasses import dataclass

SUITS = ["s", "h", "d", "c"]  # spades, hearts, diamonds, clubs
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]

SUIT_UNICODE = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
SUIT_COLOR = {"s": "black", "h": "red", "d": "blue", "c": "green"}


@dataclass(frozen=True)
class Card:
    rank: str  # "2"-"9", "T", "J", "Q", "K", "A"
    suit: str  # "s", "h", "d", "c"

    def __str__(self) -> str:
        return f"{self.rank}{SUIT_UNICODE[self.suit]}"

    def __repr__(self) -> str:
        return self.__str__()

    @property
    def color(self) -> str:
        return SUIT_COLOR[self.suit]

    def to_treys(self) -> str:
        """Convert to treys string format, e.g. 'As', 'Td'."""
        return f"{self.rank}{self.suit}"

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "suit": self.suit,
            "display": str(self),
            "color": self.color,
        }

    @classmethod
    def from_str(cls, s: str) -> "Card":
        """Parse from '2s', 'Ah', 'Td', etc."""
        return cls(rank=s[0], suit=s[1])


class Deck:
    def __init__(self) -> None:
        self._cards: list[Card] = [Card(r, s) for s in SUITS for r in RANKS]
        self.shuffle()

    def shuffle(self) -> None:
        random.shuffle(self._cards)

    def deal(self, n: int = 1) -> list[Card]:
        if n > len(self._cards):
            raise ValueError("Not enough cards in deck")
        dealt = self._cards[:n]
        self._cards = self._cards[n:]
        return dealt

    def deal_one(self) -> Card:
        return self.deal(1)[0]

    def __len__(self) -> int:
        return len(self._cards)
