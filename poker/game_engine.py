"""
Texas Hold'em game engine — state machine implementation.

Street flow: preflop → flop → turn → river → showdown
Supports: antes, side pots, all-in players, run-it-twice.
"""
from __future__ import annotations

import random

from poker.card import Card, Deck
from poker.game_state import GameState, PlayerState
from poker.hand_evaluator import evaluate, compare
from poker.player import ActionOption


STYLES = [
    "tight_aggressive",
    "loose_aggressive",
    "tight_passive",
    "loose_passive",
    "balanced",
]

STREET_ORDER = ["preflop", "flop", "turn", "river", "showdown"]


class GameEngine:
    def __init__(self, config: dict) -> None:
        table_cfg = config.get("table", {})
        blinds_cfg = config.get("blinds", {})
        ai_cfg = config.get("training", {})
        features_cfg = config.get("features", {})

        self.num_opponents: int = table_cfg.get("num_opponents", 3)
        self.starting_chips: int = table_cfg.get("starting_chips", 1000)
        self.max_chips: int = table_cfg.get("max_chips") or (2 * self.starting_chips)
        self.small_blind: int = blinds_cfg.get("small_blind", 10)
        self.big_blind: int = blinds_cfg.get("big_blind", 20)
        self.ante: int = blinds_cfg.get("ante", 0)
        self.opponent_styles: list[str] = ai_cfg.get("opponent_styles", ["random"])
        self.run_it_twice_enabled: bool = features_cfg.get("run_it_twice", False)

        self.state: GameState = GameState(
            small_blind=self.small_blind,
            big_blind=self.big_blind,
            ante=self.ante,
        )
        self._deck: Deck = Deck()
        self._init_players()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_players(self) -> None:
        styles = self._resolve_styles()
        players = []

        # Human player at idx 0
        players.append(PlayerState(
            idx=0,
            name="你",
            chips=self.starting_chips,
            is_human=True,
        ))

        ai_names = ["Alice", "Bob", "Charlie", "Diana", "Eve"]
        for i in range(self.num_opponents):
            players.append(PlayerState(
                idx=i + 1,
                name=ai_names[i % len(ai_names)],
                chips=self.starting_chips,
                is_human=False,
                style=styles[i],
            ))

        self.state.players = players
        self.state.dealer_idx = 0  # first hand: player is dealer

    def _resolve_styles(self) -> list[str]:
        styles_cfg = self.opponent_styles
        if styles_cfg == ["random"] or styles_cfg == "random":
            return [random.choice(STYLES) for _ in range(self.num_opponents)]
        if len(styles_cfg) >= self.num_opponents:
            return [s if s != "random" else random.choice(STYLES)
                    for s in styles_cfg[: self.num_opponents]]
        # pad with random if not enough
        resolved = [s if s != "random" else random.choice(STYLES) for s in styles_cfg]
        while len(resolved) < self.num_opponents:
            resolved.append(random.choice(STYLES))
        return resolved

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_hand(self) -> GameState:
        """Reset and deal a new hand. Returns updated GameState."""
        self.apply_rebuy_cashout()

        s = self.state
        s.hand_number += 1
        s.street = "preflop"
        s.community_cards = []
        s.pot = 0
        s.hand_log = []
        s.last_raiser_idx = -1

        # Rotate dealer (simple seat rotation, independent of fold/all-in state)
        s.dealer_idx = (s.dealer_idx + 1) % len(s.players)

        # Reset per-player state
        for p in s.players:
            p.hole_cards = []
            p.current_bet = 0
            p.total_bet = 0
            p.is_folded = False
            p.is_all_in = False
            p.last_action = ""
            p.last_action_amount = 0
            p.acted_this_round = False

        for p in s.players:
            p.chips_before_hand = p.chips

        # Shuffle and deal
        self._deck = Deck()
        for p in s.players:
            p.hole_cards = self._deck.deal(2)

        # Collect ante
        if s.ante > 0:
            for p in s.players:
                amount = min(p.chips, s.ante)
                p.chips -= amount
                p.total_bet += amount
                s.pot += amount
                s.log_action(p.name, "ante", amount)

        # Post blinds
        n = len(s.players)
        if n == 2:
            # heads-up: dealer posts SB
            sb_idx = s.dealer_idx
            bb_idx = self._next_active_idx(sb_idx)
        else:
            sb_idx = self._next_active_idx(s.dealer_idx)
            bb_idx = self._next_active_idx(sb_idx)

        self._post_blind(s.players[sb_idx], s.small_blind)
        s.log_action(s.players[sb_idx].name, "small_blind", s.small_blind)

        self._post_blind(s.players[bb_idx], s.big_blind)
        s.log_action(s.players[bb_idx].name, "big_blind", s.big_blind)

        s.current_max_bet = s.big_blind
        s.min_raise = s.big_blind

        # First to act preflop: UTG (after BB)
        s.current_player_idx = self._next_active_idx(bb_idx)
        # BB acts last preflop if no raise; track via last_raiser_idx (not used in completion check)
        s.last_raiser_idx = bb_idx

        return s

    def get_valid_actions(self) -> list[ActionOption]:
        s = self.state
        p = s.players[s.current_player_idx]
        options = []
        call_amount = min(p.chips, s.current_max_bet - p.current_bet)

        # Fold always available
        options.append(ActionOption(action="fold"))

        # Check if no debt
        if call_amount == 0:
            options.append(ActionOption(action="check"))
        else:
            options.append(ActionOption(action="call", call_amount=call_amount))

        # Raise / All-in
        min_raise_to = s.current_max_bet + s.min_raise
        if p.chips > call_amount:
            # can raise at least to min_raise_to, capped at all-in
            all_in_total = p.current_bet + p.chips
            if all_in_total >= min_raise_to:
                options.append(ActionOption(
                    action="raise",
                    min_amount=min_raise_to,
                    max_amount=all_in_total,
                    call_amount=call_amount,
                ))
            options.append(ActionOption(
                action="all_in",
                min_amount=all_in_total,
                max_amount=all_in_total,
            ))

        return options

    def apply_action(self, action: str, amount: int = 0) -> dict:
        """
        Apply a player action. Returns result dict with keys:
        - hand_over (bool)
        - next_player_idx (int, -1 if hand over)
        - street_changed (bool)
        - run_it_twice_prompt (bool, optional) — all-in runout, needs decision
        """
        s = self.state
        p = s.players[s.current_player_idx]

        # Guard: replace any invalid action with the best legal alternative.
        # This prevents GPT from bypassing bet obligations (e.g. checking preflop).
        valid = self.get_valid_actions()
        valid_names = {a.action for a in valid}
        if action not in valid_names:
            call_opt = next((a for a in valid if a.action == "call"), None)
            if call_opt:
                action, amount = "call", call_opt.call_amount
            elif any(a.action == "check" for a in valid):
                action, amount = "check", 0
            else:
                action, amount = "fold", 0

        # Coerce raise-to-all-in into all_in
        if action in ("raise", "bet") and amount >= p.current_bet + p.chips:
            action = "all_in"
            amount = 0

        if action == "fold":
            p.is_folded = True
            p.last_action = "fold"
            p.acted_this_round = True
            s.log_action(p.name, "fold")

        elif action == "check":
            p.last_action = "check"
            p.acted_this_round = True
            s.log_action(p.name, "check")

        elif action == "call":
            call_amount = min(p.chips, s.current_max_bet - p.current_bet)
            p.chips -= call_amount
            p.current_bet += call_amount
            p.total_bet += call_amount
            s.pot += call_amount
            if p.chips == 0:
                p.is_all_in = True
            p.last_action = "call"
            p.last_action_amount = call_amount
            p.acted_this_round = True
            s.log_action(p.name, "call", call_amount)

        elif action == "raise":
            # amount = total bet amount (raise_to)
            raise_to = max(amount, s.current_max_bet + s.min_raise)
            raise_to = min(raise_to, p.current_bet + p.chips)
            added = raise_to - p.current_bet
            s.min_raise = raise_to - s.current_max_bet
            s.current_max_bet = raise_to
            p.chips -= added
            p.current_bet = raise_to
            p.total_bet += added
            s.pot += added
            if p.chips == 0:
                p.is_all_in = True
            s.last_raiser_idx = s.current_player_idx
            p.last_action = "raise"
            p.last_action_amount = raise_to
            p.acted_this_round = True
            # Reset acted_this_round for others who must re-act after a raise
            for other in s.players:
                if other.idx != p.idx and not other.is_folded and not other.is_all_in:
                    other.acted_this_round = False
            s.log_action(p.name, "raise", raise_to)

        elif action == "all_in":
            added = p.chips
            all_in_total = p.current_bet + added
            if all_in_total > s.current_max_bet:
                s.min_raise = all_in_total - s.current_max_bet
                s.current_max_bet = all_in_total
                s.last_raiser_idx = s.current_player_idx
                # Reset acted_this_round for others who must re-act
                for other in s.players:
                    if other.idx != p.idx and not other.is_folded and not other.is_all_in:
                        other.acted_this_round = False
            p.current_bet = all_in_total
            p.total_bet += added
            s.pot += added
            p.chips = 0
            p.is_all_in = True
            p.last_action = "all_in"
            p.last_action_amount = all_in_total
            p.acted_this_round = True
            s.log_action(p.name, "all_in", all_in_total)

        # Normalize: any action that empties chips is recorded as all_in
        if p.is_all_in and p.last_action != "all_in":
            p.last_action = "all_in"
            p.last_action_amount = p.total_bet
            if s.hand_log:
                s.hand_log[-1]["action"] = "all_in"
                s.hand_log[-1]["amount"] = p.total_bet

        # Only one non-folded player → uncontested win
        active_not_folded = [pl for pl in s.players if not pl.is_folded]
        if len(active_not_folded) == 1:
            return self._end_hand_uncontested(active_not_folded[0])

        # Betting round complete?
        if self._betting_round_complete():
            active_betting = [pl for pl in s.players if not pl.is_folded and not pl.is_all_in]
            if len(active_betting) <= 1 and len(active_not_folded) >= 2:
                self._return_uncallable_bets()
                # All community cards dealt → resolve showdown
                if len(s.community_cards) >= 5:
                    return self._resolve_showdown()
                # RIT: only when exactly 2 players all-in (no active bettors)
                if not active_betting and len(active_not_folded) == 2:
                    if self.run_it_twice_enabled:
                        human_in_hand = any(p.is_human and not p.is_folded for p in s.players)
                        if human_in_hand:
                            return {
                                "hand_over": False,
                                "run_it_twice_prompt": True,
                                "next_player_idx": -1,
                                "street_changed": False,
                            }
                # Step-by-step runout handled by server
                return {"hand_over": False, "all_in_runout": True, "next_player_idx": -1, "street_changed": False}

            return self._advance_street()

        # Move to next player (skip folded and all-in players)
        s.current_player_idx = self._next_active_idx(s.current_player_idx)
        return {"hand_over": False, "next_player_idx": s.current_player_idx, "street_changed": False}

    def runout(self, run_twice: bool) -> dict:
        """Called after run-it-twice decision is made. Deals remaining cards."""
        if run_twice:
            return self._runout_twice()
        return self._runout_once()

    def step_runout(self) -> dict:
        """Deal one more street during all-in runout. Returns {done, street}."""
        s = self.state
        street_idx = STREET_ORDER.index(s.street)
        next_street = STREET_ORDER[street_idx + 1]
        if next_street == "flop":
            s.community_cards.extend(self._deck.deal(3))
        elif next_street in ("turn", "river"):
            s.community_cards.extend(self._deck.deal(1))
        elif next_street == "showdown":
            return {"done": True, "street": "showdown"}
        s.street = next_street
        return {"done": next_street == "river", "street": next_street}

    def settle_runout(self) -> dict:
        """Resolve showdown after all-in runout streets have been dealt."""
        return self._resolve_showdown()

    def _return_uncallable_bets(self) -> None:
        """Return uncallable excess to players whose total_bet exceeds opponents' maximum."""
        non_folded = [p for p in self.state.players if not p.is_folded]
        if len(non_folded) < 2:
            return
        for p in non_folded:
            other_max = max(q.total_bet for q in non_folded if q.idx != p.idx)
            excess = p.total_bet - min(p.total_bet, other_max)
            if excess > 0:
                p.chips += excess
                p.total_bet -= excess
                p.current_bet = max(0, p.current_bet - excess)
                self.state.pot -= excess

    def prepare_run_twice(self) -> None:
        """Save base state before step-by-step RIT animation."""
        s = self.state
        self._rit_base_community = list(s.community_cards)
        self._rit_base_street = s.street
        self._rit_side_pots = self._build_side_pots()
        self._rit_run1_community: list = []
        self._rit_run1_results: list = []

    def reset_for_run2(self) -> None:
        """After run 1 complete: evaluate, save results, reset community for run 2."""
        s = self.state
        self._rit_run1_community = [c.to_dict() for c in s.community_cards]
        self._rit_run1_results = self._evaluate_pots_only(self._rit_side_pots)
        s.community_cards = list(self._rit_base_community)
        s.street = self._rit_base_street

    def settle_run_twice(self) -> dict:
        """Evaluate run 2, distribute chips across both runs, return hand result."""
        s = self.state
        run_2_community = [c.to_dict() for c in s.community_cards]
        run_2_results = self._evaluate_pots_only(self._rit_side_pots)
        combined = self._distribute_run_twice(self._rit_side_pots, self._rit_run1_results, run_2_results)
        s.street = "showdown"
        reveal = {p.name: [c.to_dict() for c in p.hole_cards] for p in s.players if not p.is_folded}
        return {
            "hand_over": True,
            "next_player_idx": -1,
            "street_changed": True,
            "run_twice": True,
            "run_1_community": self._rit_run1_community,
            "run_2_community": run_2_community,
            "run_1_results": self._rit_run1_results,
            "run_2_results": run_2_results,
            "side_pot_results": combined,
            "reveal": reveal,
            "hand_log": list(s.hand_log),
        }

    def apply_rebuy_cashout(self) -> None:
        """Apply rebuy/cashout after a hand ends."""
        for p in self.state.players:
            if p.chips == 0:
                p.chips = self.starting_chips
                p.chip_adjustment -= 1
            else:
                while p.chips > self.max_chips:
                    p.chips -= self.starting_chips
                    p.chip_adjustment += 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_blind(self, player: PlayerState, amount: int) -> None:
        actual = min(player.chips, amount)
        player.chips -= actual
        player.current_bet = actual
        player.total_bet = actual
        self.state.pot += actual
        if player.chips == 0:
            player.is_all_in = True

    def _next_active_idx(self, from_idx: int) -> int:
        """Return next player index that is not folded and not all-in."""
        n = len(self.state.players)
        for i in range(1, n + 1):
            idx = (from_idx + i) % n
            p = self.state.players[idx]
            if not p.is_folded and not p.is_all_in:
                return idx
        # All remaining players are all-in — return first non-folded
        for i in range(1, n + 1):
            idx = (from_idx + i) % n
            if not self.state.players[idx].is_folded:
                return idx
        return from_idx  # fallback

    def _betting_round_complete(self) -> bool:
        """Return True when all active (non-folded, non-all-in) players have acted
        this round AND all their bets match the current max bet."""
        s = self.state
        active = [p for p in s.players if not p.is_folded and not p.is_all_in]
        if not active:
            return True
        return all(p.acted_this_round and p.current_bet >= s.current_max_bet for p in active)

    def _advance_street(self) -> dict:
        s = self.state
        # Reset bets and acted_this_round for new round
        for p in s.players:
            p.current_bet = 0
            p.last_action = ""
            p.last_action_amount = 0
            p.acted_this_round = False
        s.current_max_bet = 0
        s.min_raise = s.big_blind
        s.last_raiser_idx = -1

        street_idx = STREET_ORDER.index(s.street)
        next_street = STREET_ORDER[street_idx + 1]

        if next_street == "flop":
            s.community_cards.extend(self._deck.deal(3))
        elif next_street in ("turn", "river"):
            s.community_cards.extend(self._deck.deal(1))
        elif next_street == "showdown":
            return self._resolve_showdown()

        s.street = next_street

        # First to act post-flop: first active player after dealer
        s.current_player_idx = self._next_active_idx(s.dealer_idx)

        return {"hand_over": False, "next_player_idx": s.current_player_idx, "street_changed": True}

    def _deal_remaining_community_inplace(self) -> None:
        """Fill community cards to 5 using the current deck."""
        s = self.state
        while len(s.community_cards) < 5:
            if len(s.community_cards) == 0:
                s.community_cards.extend(self._deck.deal(3))
            else:
                s.community_cards.extend(self._deck.deal(1))

    def _runout_once(self) -> dict:
        """Deal all remaining community cards once and resolve showdown."""
        self._deal_remaining_community_inplace()
        return self._resolve_showdown()

    def _runout_twice(self) -> dict:
        """Deal remaining community cards twice, split each side pot between runs."""
        s = self.state
        base_community = list(s.community_cards)

        # Build side pots now (before dealing) — based on total_bet
        side_pots = self._build_side_pots()

        # --- Run 1 ---
        s.community_cards = list(base_community)
        self._deal_remaining_community_inplace()
        community_1 = [c.to_dict() for c in s.community_cards]
        run_1_results = self._evaluate_pots_only(side_pots)

        # --- Run 2 (continue from remaining deck — next N cards) ---
        s.community_cards = list(base_community)
        self._deal_remaining_community_inplace()
        community_2 = [c.to_dict() for c in s.community_cards]
        run_2_results = self._evaluate_pots_only(side_pots)

        # Distribute chips: each run gets half pot per side pot
        combined_results = self._distribute_run_twice(side_pots, run_1_results, run_2_results)

        s.street = "showdown"

        reveal = {p.name: [c.to_dict() for c in p.hole_cards]
                  for p in s.players if not p.is_folded}

        result = {
            "hand_over": True,
            "next_player_idx": -1,
            "street_changed": True,
            "run_twice": True,
            "run_1_community": community_1,
            "run_2_community": community_2,
            "run_1_results": run_1_results,
            "run_2_results": run_2_results,
            "side_pot_results": combined_results,
            "reveal": reveal,
            "hand_log": list(s.hand_log),
        }

        return result

    def _evaluate_pots_only(self, side_pots: list) -> list[dict]:
        """Evaluate winners for each side pot using current community cards, without awarding chips."""
        s = self.state
        results = []
        for pot_amount, eligible_players in side_pots:
            best_result = None
            winners = []
            for p in eligible_players:
                if p.is_folded:
                    continue
                ev = evaluate(p.hole_cards, s.community_cards)
                if best_result is None or compare(ev, best_result) > 0:
                    best_result = ev
                    winners = [p]
                elif compare(ev, best_result) == 0:
                    winners.append(p)
            results.append({
                "pot_amount": pot_amount,
                "winners": [w.name for w in winners],
                "hand_name": best_result["class_name"] if best_result else "",
            })
        return results

    def _distribute_run_twice(
        self,
        side_pots: list,
        run_1_results: list[dict],
        run_2_results: list[dict],
    ) -> list[dict]:
        """Distribute chips for run-twice; return per-player total chips won."""
        player_totals: dict[str, int] = {}
        for i, (pot_amount, eligible_players) in enumerate(side_pots):
            half_1 = (pot_amount + 1) // 2
            half_2 = pot_amount // 2

            r1_winners = [p for p in eligible_players if p.name in run_1_results[i]["winners"]]
            if r1_winners:
                share = half_1 // len(r1_winners)
                rem = half_1 - share * len(r1_winners)
                for j, w in enumerate(r1_winners):
                    won = share + (rem if j == 0 else 0)
                    w.chips += won
                    player_totals[w.name] = player_totals.get(w.name, 0) + won

            r2_winners = [p for p in eligible_players if p.name in run_2_results[i]["winners"]]
            if r2_winners:
                share = half_2 // len(r2_winners)
                rem = half_2 - share * len(r2_winners)
                for j, w in enumerate(r2_winners):
                    won = share + (rem if j == 0 else 0)
                    w.chips += won
                    player_totals[w.name] = player_totals.get(w.name, 0) + won

        return [
            {"pot_amount": total, "winners": [name], "hand_name": ""}
            for name, total in sorted(player_totals.items(), key=lambda x: -x[1])
        ]

    def _resolve_showdown(self) -> dict:
        s = self.state
        s.street = "showdown"

        # Build side pots
        side_pots = self._build_side_pots()
        results = []

        for pot_amount, eligible_players in side_pots:
            best_result = None
            winners = []
            for p in eligible_players:
                if p.is_folded:
                    continue
                ev = evaluate(p.hole_cards, s.community_cards)
                if best_result is None or compare(ev, best_result) > 0:
                    best_result = ev
                    winners = [p]
                elif compare(ev, best_result) == 0:
                    winners.append(p)

            share = pot_amount // len(winners)
            remainder = pot_amount - share * len(winners)
            for i, w in enumerate(winners):
                w.chips += share + (remainder if i == 0 else 0)

            results.append({
                "pot_amount": pot_amount,
                "winners": [w.name for w in winners],
                "hand_name": best_result["class_name"] if best_result else "",
            })

        # Build reveal payload
        reveal = {p.name: [c.to_dict() for c in p.hole_cards]
                  for p in s.players if not p.is_folded}

        result = {
            "hand_over": True,
            "next_player_idx": -1,
            "street_changed": True,
            "side_pot_results": results,
            "reveal": reveal,
            "hand_log": list(s.hand_log),
        }

        return result

    def _end_hand_uncontested(self, winner: PlayerState) -> dict:
        s = self.state
        winner.chips += s.pot
        result = {
            "hand_over": True,
            "next_player_idx": -1,
            "street_changed": False,
            "side_pot_results": [{"pot_amount": s.pot, "winners": [winner.name], "hand_name": ""}],
            "reveal": {},
            "hand_log": list(s.hand_log),
        }
        return result

    def _build_side_pots(self) -> list[tuple[int, list[PlayerState]]]:
        """Build side pots based on total_bet amounts."""
        players = [p for p in self.state.players if not p.is_folded or p.total_bet > 0]
        all_bets = sorted({p.total_bet for p in players if p.total_bet > 0})
        side_pots: list[tuple[int, list[PlayerState]]] = []
        prev = 0
        for level in all_bets:
            pot_amount = 0
            eligible = []
            for p in self.state.players:
                contribution = min(p.total_bet, level) - min(p.total_bet, prev)
                if contribution > 0:
                    pot_amount += contribution
                if p.total_bet >= level and not p.is_folded:
                    eligible.append(p)
            if pot_amount > 0:
                side_pots.append((pot_amount, eligible))
            prev = level
        return side_pots

    def get_state_snapshot(self, reveal_all: bool = False) -> dict:
        return self.state.to_dict(reveal_all=reveal_all)

    def get_valid_actions_dict(self) -> list[dict]:
        return [a.to_dict() for a in self.get_valid_actions()]
