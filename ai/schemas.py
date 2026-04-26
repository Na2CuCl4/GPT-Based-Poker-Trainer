"""Pydantic schemas for all GPT structured I/O."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# AI opponent decision
# ---------------------------------------------------------------------------

class OpponentDecision(BaseModel):
    action: Literal["fold", "check", "call", "raise", "all_in"]
    raise_to: Optional[int] = None   # total bet amount when action is "raise"
    reasoning: str                   # brief internal reasoning (Chinese)


# ---------------------------------------------------------------------------
# Real-time advisor hint
# ---------------------------------------------------------------------------

class HintRecommendation(BaseModel):
    action: Literal["fold", "check", "call", "raise", "all_in"]
    raise_to: Optional[int] = None
    confidence: Literal["高", "中", "低"]
    explanation: str          # ≤80 Chinese characters
    hand_strength_desc: str   # description of current hand strength
    pot_odds_note: str        # pot odds / EV comment


# ---------------------------------------------------------------------------
# Post-hand analysis
# ---------------------------------------------------------------------------

class DecisionEval(BaseModel):
    street: str
    player_action: str
    suggested_action: str
    is_optimal: bool
    reason: str               # Chinese explanation


class HandAnalysis(BaseModel):
    overall_score: int                       # 0-100
    summary: str                             # 1-2 sentences
    key_decision_evals: list[DecisionEval]
    main_lesson: str
    tips: list[str]                          # 2-3 improvement tips


# ---------------------------------------------------------------------------
# Run-it-twice decision
# ---------------------------------------------------------------------------

class RunItTwiceDecision(BaseModel):
    run_twice: bool    # True = agree to run twice
    reasoning: str     # brief Chinese reasoning (≤50 chars)
