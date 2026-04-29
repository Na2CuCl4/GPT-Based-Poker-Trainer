from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ActionOption:
    action: str        # fold | check | call | raise | all_in
    min_amount: int = 0
    max_amount: int = 0
    call_amount: int = 0

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "call_amount": self.call_amount,
        }
