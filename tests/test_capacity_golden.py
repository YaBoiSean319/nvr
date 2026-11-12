"""capacity.estimate gives exactly the figures it gave before it was restructured (#286): twelve plans, every number and every note."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app import capacity
from tests.capacity_plans import PLANS

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "capacity_golden.json").read_text())


class GoldenEstimateTests(unittest.TestCase):
    def test_every_plan_gives_the_recorded_estimate(self):
        self.assertEqual(set(PLANS), set(GOLDEN))
        for name, plan in PLANS.items():
            with self.subTest(plan=name):
                got = json.loads(json.dumps(capacity.estimate(plan["rows"], plan["viewers"], plan["tiles"], plan["overrides"])))
                self.assertEqual(got, GOLDEN[name])


if __name__ == "__main__":
    unittest.main()
