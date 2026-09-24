import unittest

from paper_engine import RiskState, net_expected_edge, simulate_market_buy, taker_fee_per_share


class PaperEngineTests(unittest.TestCase):
    def test_fee_formula(self):
        self.assertAlmostEqual(taker_fee_per_share(0.50), 0.0175, places=8)
        self.assertAlmostEqual(taker_fee_per_share(0.20), 0.0112, places=8)

    def test_depth_walk(self):
        fill = simulate_market_buy([(0.50, 1.0), (0.60, 2.0)], 1.20)
        self.assertAlmostEqual(fill.gross_cost, 1.20, places=8)
        self.assertAlmostEqual(fill.shares, 1.0 + (0.70 / 0.60), places=8)
        self.assertEqual(fill.levels_used, 2)
        self.assertAlmostEqual(fill.worst_price, 0.60, places=8)

    def test_edge_uses_all_in_cost(self):
        fill = simulate_market_buy([(0.50, 2.0)], 1.00)
        edge = net_expected_edge(0.60, fill)
        self.assertGreater(edge, 0.0)

    def test_risk_limits(self):
        state = RiskState.create(10.0)
        ok, reason = state.can_enter(1.0, 0.20, 0.10)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")
        ok, reason = state.can_enter(3.0, 0.20, 0.10)
        self.assertFalse(ok)
        self.assertEqual(reason, "position_limit")
        state.realized_pnl_today = -1.0
        ok, reason = state.can_enter(1.0, 0.20, 0.10)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_limit")


if __name__ == "__main__":
    unittest.main()
