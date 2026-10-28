"""Тесты логики на синтетических данных в формате документированных API.

Это не реальные котировки — только проверка разбора, сопоставления и математики.
Запуск: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from predicts import alerts  # noqa: E402
from predicts.matching import Match, find_matches, key_numbers, tokens  # noqa: E402
from predicts.models import Market, parse_dt  # noqa: E402
from predicts.sources import kalshi, polymarket  # noqa: E402
from predicts.storage import Store  # noqa: E402

POLY_EVENT = {
    "id": "1", "slug": "bitcoin-above-on-december-31", "title": "Bitcoin above ___ on December 31?",
    "endDate": "2026-12-31T17:00:00Z", "tags": [{"label": "Crypto"}],
    "markets": [
        {"conditionId": "0xaaa", "question": "Will Bitcoin be above $120,000 on December 31?",
         "groupItemTitle": "120,000", "outcomes": '["Yes", "No"]', "outcomePrices": '["0.42", "0.58"]',
         "bestBid": 0.41, "bestAsk": 0.43, "volumeNum": 150000, "volume24hr": 12000,
         "liquidityNum": 30000, "endDate": "2026-12-31T17:00:00Z", "active": True, "closed": False},
        {"conditionId": "0xbbb", "question": "Will Bitcoin be above $150,000 on December 31?",
         "outcomes": '["Yes", "No"]', "outcomePrices": '["0.1", "0.9"]', "bestBid": 0, "bestAsk": 1,
         "active": True, "closed": True},
    ],
}

KALSHI_EVENT = {
    "event_ticker": "KXBTCY-26DEC31", "series_ticker": "KXBTCY", "category": "Crypto",
    "title": "Bitcoin price on Dec 31, 2026",
    "markets": [
        {"ticker": "KXBTCY-26DEC31-T120000", "status": "active", "yes_sub_title": "Above $120,000",
         "yes_bid": 47, "yes_ask": 49, "no_bid": 51, "no_ask": 53, "last_price": 48,
         "volume": 8000, "volume_24h": 900, "liquidity": 500000, "close_time": "2026-12-31T22:00:00Z"},
        {"ticker": "KXBTCY-26DEC31-T130000", "status": "active", "yes_sub_title": "Above $130,000",
         "yes_bid_dollars": "0.3000", "yes_ask_dollars": "0.3200", "last_price_dollars": "0.3100",
         "close_time": "2026-12-31T22:00:00Z"},
        {"ticker": "KXMVESPORTS-X", "status": "active", "yes_sub_title": "combo"},
    ],
}


class ParseTests(unittest.TestCase):
    def test_polymarket(self):
        ms = polymarket.parse_event(POLY_EVENT)
        self.assertEqual(len(ms), 1)  # закрытый рынок отброшен
        m = ms[0]
        self.assertEqual((m.yes_price, m.yes_bid, m.yes_ask), (0.42, 0.41, 0.43))
        self.assertAlmostEqual(m.no_ask, 0.59)
        self.assertEqual(m.category, "Crypto")
        self.assertEqual(m.url, "https://polymarket.com/event/bitcoin-above-on-december-31")

    def test_kalshi_cents_and_dollars(self):
        ms = kalshi.parse_event(KALSHI_EVENT)
        self.assertEqual([m.id for m in ms], ["KXBTCY-26DEC31-T120000", "KXBTCY-26DEC31-T130000"])
        a, b = ms
        self.assertEqual(a.question, "Bitcoin price on Dec 31, 2026: Above $120,000")
        self.assertEqual((a.yes_bid, a.yes_ask, a.no_ask, a.yes_price), (0.47, 0.49, 0.53, 0.48))
        self.assertEqual(a.liquidity, 5000)
        self.assertEqual((b.yes_bid, b.yes_ask, b.yes_price), (0.3, 0.32, 0.31))
        self.assertAlmostEqual(b.no_ask, 0.7)  # достроено из yes_bid

    def test_parse_dt(self):
        self.assertEqual(parse_dt("2026-12-31T17:00:00.5Z"),
                         datetime(2026, 12, 31, 17, 0, 0, 500000, tzinfo=timezone.utc))
        self.assertEqual(parse_dt(1798736400000), parse_dt(1798736400))
        self.assertIsNone(parse_dt("garbage"))


class MatchingTests(unittest.TestCase):
    def test_numbers_normalized(self):
        for text in ("BTC above $120K", "Bitcoin over 120,000", "bitcoin > 0.12 million"):
            self.assertEqual(key_numbers(tokens(text)), {"120000"}, text)
        self.assertEqual(key_numbers(tokens("by Dec 31st 2026 at 5pm ET")), {"31"})

    def test_cross_platform_match_and_arb(self):
        ms = polymarket.parse_event(POLY_EVENT) + kalshi.parse_event(KALSHI_EVENT)
        found = find_matches(ms)
        self.assertEqual(len(found), 1)
        mt = found[0]
        self.assertEqual({mt.a.id, mt.b.id}, {"0xaaa", "KXBTCY-26DEC31-T120000"})
        arb = mt.arbitrage()
        # YES на Polymarket за 0.43 + NO на Kalshi за 0.53 = 0.96 + комиссия Kalshi
        self.assertEqual((arb["buy_a"], arb["buy_b"]), ("NO", "YES")
                         if mt.a.source == "kalshi" else ("YES", "NO"))
        self.assertAlmostEqual(arb["fees"], 0.07 * 0.53 * 0.47, places=6)
        self.assertAlmostEqual(arb["edge"], 1 - 0.96 - arb["fees"], places=6)

    def test_strike_mismatch_not_matched(self):
        a = Market("polymarket", "1", "Will Bitcoin be above $120,000 on December 31?", yes_price=.4)
        b = Market("kalshi", "2", "Will Bitcoin be above $130,000 on December 31?", yes_price=.3)
        self.assertEqual(find_matches([a, b]), [])

    def test_inverted_outcomes(self):
        a = Market("polymarket", "1", "Lakers vs. Celtics", yes_label="Lakers", no_label="Celtics",
                   yes_price=0.6, yes_bid=0.59, yes_ask=0.61)
        b = Market("kalshi", "2", "Lakers vs Celtics: Celtics", outcome="Celtics", yes_label="Celtics",
                   yes_price=0.45, yes_bid=0.44, yes_ask=0.46)
        mt = Match(a, b, 1.0, inverted=True)
        self.assertAlmostEqual(mt.spread, 0.05)    # 0.60 против 1-0.45
        arb = mt.arbitrage({"kalshi": 0})
        # Lakers на A (YES, 0.61) + Celtics на B (YES, 0.46) = 1.07
        # Celtics на A (NO, 0.41) + Lakers на B (NO, 0.56) = 0.97 — дешевле
        self.assertEqual(arb["buy_a"], "NO")
        self.assertEqual(arb["buy_b"], "NO")
        self.assertAlmostEqual(arb["cost"], 0.41 + 0.56)


class StoreAlertTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        os.remove(self.path)

    def test_new_and_move(self):
        t0 = time.time() - 3600
        m = Market("polymarket", "x", "Q?", yes_price=0.30)
        self.assertEqual(self.store.record([m], ts=t0), [])          # первый запуск — база
        m2 = Market("polymarket", "y", "Brand new?", yes_price=0.5)
        m.yes_price = 0.42
        new = self.store.record([m, m2])
        self.assertEqual([n.id for n in new], ["y"])
        moves = alerts.move_alerts([m], self.store, threshold=0.05, window_min=30,
                                   min_volume=0, keywords=[])
        self.assertEqual(len(moves), 1)
        self.assertIn("30.0% → <b>42.0%</b>", moves[0].text)
        self.store.mark_sent([moves[0].key])
        self.assertTrue(self.store.was_sent(moves[0].key, 3600))


if __name__ == "__main__":
    unittest.main()
