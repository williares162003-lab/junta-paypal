"""Tests for the fair-swap algorithm, including an exact check of the Hungarian algorithm by brute force."""
import itertools
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from junta.fairswap import INF, hungarian, propose  # noqa: E402


def m(mid, turn, preference="NEUTRAL", urgency=0):
    return {"id": mid, "name": f"M{mid}", "turn": turn, "preference": preference, "urgency": urgency}


class HungarianTest(unittest.TestCase):
    def test_matches_brute_force_on_random_matrices(self):
        rng = random.Random(2026)
        for _ in range(300):
            n = rng.randint(1, 7)
            cost = [[rng.randint(0, 50) for _ in range(n)] for _ in range(n)]
            got = hungarian(cost)
            self.assertEqual(sorted(got), list(range(n)))           # a valid permutation
            best = min(sum(cost[i][p[i]] for i in range(n)) for p in itertools.permutations(range(n)))
            self.assertEqual(sum(cost[i][got[i]] for i in range(n)), best)

    def test_handles_forbidden_cells(self):
        cost = [[0, INF, INF], [INF, 0, INF], [INF, INF, 0]]
        self.assertEqual(hungarian(cost), [0, 1, 2])


class FairSwapTest(unittest.TestCase):
    def test_nobody_asks_nothing_changes(self):
        self.assertEqual(propose([m(1, 1), m(2, 2), m(3, 3)])["changes"], {})

    def test_urgent_member_swaps_with_one_who_prefers_later(self):
        # Luis (turn 3) has an emergency; Ana (turn 1) prefers to receive later; Rosa asked nothing.
        result = propose([m(1, 1, "LATER"), m(2, 2), m(3, 3, "EARLY", 3)])
        self.assertEqual(result["changes"], {1: (1, 3), 3: (3, 1)})
        self.assertNotIn(2, result["changes"])                       # Rosa is never moved
        self.assertLess(result["cost_after"], result["cost_before"])

    def test_neutral_members_are_never_moved_even_if_it_would_help(self):
        # Only Luis asks; nobody else agreed to move, so there is no fair swap.
        self.assertEqual(propose([m(1, 1), m(2, 2), m(3, 3, "EARLY", 3)])["changes"], {})

    def test_most_urgent_member_gets_the_earliest_turn(self):
        result = propose([m(1, 1, "LATER"), m(2, 2, "LATER"), m(3, 3, "EARLY", 1), m(4, 4, "EARLY", 3)])
        new_turn = {mid: new for mid, (_, new) in result["changes"].items()}
        self.assertEqual(new_turn[4], 1)
        self.assertEqual(new_turn[3], 2)

    def test_member_already_first_is_not_moved_for_nothing(self):
        self.assertEqual(propose([m(1, 1, "EARLY", 3), m(2, 2, "LATER")])["changes"], {})

    def test_only_open_turns_are_used(self):
        # Rounds 1-2 already paid out: only turns 3-5 can be reassigned.
        result = propose([m(3, 3, "LATER"), m(4, 4), m(5, 5, "EARLY", 2)])
        self.assertEqual(result["changes"], {3: (3, 5), 5: (5, 3)})


if __name__ == "__main__":
    unittest.main(verbosity=2)
