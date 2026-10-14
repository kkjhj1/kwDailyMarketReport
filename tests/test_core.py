import tempfile
import unittest
from datetime import date
from pathlib import Path
from market_brief.alert_store import AlertStore
from market_brief.core import is_alert_threshold_met

class AlertTests(unittest.TestCase):
    def test_threshold(self):
        self.assertFalse(is_alert_threshold_met(2.99))
        self.assertTrue(is_alert_threshold_met(-3.0))
        self.assertTrue(is_alert_threshold_met(3.1))

    def test_market_once_per_day(self):
        with tempfile.TemporaryDirectory() as d:
            store = AlertStore(str(Path(d) / "test.sqlite3"))
            today = date(2026, 10, 13)
            self.assertFalse(store.reserve_if_threshold("KR", today, 2.8))
            self.assertTrue(store.reserve_if_threshold("KR", today, -3.1))
            self.assertFalse(store.reserve_if_threshold("KR", today, -4.5))
            self.assertTrue(store.reserve_if_threshold("US", today, 3.2))
            self.assertTrue(store.reserve_if_threshold("KR", date(2026, 10, 14), 3.3))

if __name__ == "__main__":
    unittest.main()
