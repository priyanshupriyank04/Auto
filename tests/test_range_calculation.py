import unittest
import os
import sys

# Add project root to sys.path
sys.path.append(os.getcwd())

from src.breakout_strategy import BreakoutStrategyEngine, RANGE_DEFINED, SCANNING_FOR_PAIR

class TestRangeCalculation(unittest.TestCase):
    def setUp(self):
        # Initialize strategy with a dummy symbol
        self.strategy = BreakoutStrategyEngine(symbol="BTC-USDC", db_path=":memory:")

    def test_range_identification_logic(self):
        """
        Verify that the range is correctly identified when two consecutive 
        opposite-colored 5m candles are processed after 08:00 IST.
        """
        # 1. First Candle: Green (Open 100, Close 110)
        candle1 = {
            "symbol": "BTC-USDC",
            "interval": "5m",
            "open_time": 1773196200000, # 08:00 AM IST (example timestamp)
            "open": 100.0,
            "high": 115.0,
            "low": 95.0,
            "close": 110.0
        }
        
        # 2. Second Candle: Red (Open 110, Close 105) - Opposite color!
        candle2 = {
            "symbol": "BTC-USDC",
            "interval": "5m",
            "open_time": 1773196500000, # 08:05 AM IST
            "open": 110.0,
            "high": 112.0,
            "low": 102.0,
            "close": 105.0
        }

        # Process first candle
        res1 = self.strategy.process_closed_5m_candle(candle1)
        self.assertEqual(self.strategy._state.current_state, SCANNING_FOR_PAIR)
        self.assertFalse(res1.get("pair_found"))

        # Process second candle
        res2 = self.strategy.process_closed_5m_candle(candle2)
        
        # Assertions
        self.assertTrue(res2.get("pair_found"))
        self.assertEqual(self.strategy._state.current_state, RANGE_DEFINED)
        
        # Range should be based on the high/low of these two candles
        # Highs: 115.0, 112.0 -> Range High: 115.0
        # Lows: 95.0, 102.0 -> Range Low: 95.0
        expected_high = 115.0
        expected_low = 95.0
        
        self.assertEqual(res2.get("range_high"), expected_high)
        self.assertEqual(res2.get("range_low"), expected_low)
        self.assertEqual(res2.get("range_size"), expected_high - expected_low)
        
        print(f"\n[SUCCESS] Range correctly identified: High={res2.get('range_high')}, Low={res2.get('range_low')}")

    def test_same_color_no_range(self):
        """Verify that the range is NOT identified if candles have the same color."""
        # Candle 1: Green
        candle1 = {"open": 100, "close": 110, "high": 115, "low": 95, "open_time": 1773196200000}
        # Candle 2: Green
        candle2 = {"open": 110, "close": 120, "high": 125, "low": 105, "open_time": 1773196500000}

        self.strategy.process_closed_5m_candle(candle1)
        res = self.strategy.process_closed_5m_candle(candle2)
        
        self.assertEqual(self.strategy._state.current_state, SCANNING_FOR_PAIR)
        self.assertIsNone(res.get("pair_found"))
        print("[SUCCESS] Same color candles correctly ignored.")

    def test_red_to_green_identification(self):
        """Verify that a Red candle followed by a Green candle also defines the range."""
        # Candle 1: Red
        candle1 = {"open": 110, "close": 100, "high": 115, "low": 95, "open_time": 1773196200000}
        # Candle 2: Green
        candle2 = {"open": 100, "close": 105, "high": 108, "low": 98, "open_time": 1773196500000}

        self.strategy.process_closed_5m_candle(candle1)
        res = self.strategy.process_closed_5m_candle(candle2)
        
        self.assertTrue(res.get("pair_found"))
        self.assertEqual(self.strategy._state.current_state, RANGE_DEFINED)
        self.assertEqual(res.get("range_high"), 115.0)
        self.assertEqual(res.get("range_low"), 95.0)
        print("[SUCCESS] Red-to-Green range correctly identified.")

if __name__ == "__main__":
    unittest.main()
