"""Тесты анализа разведчика (без браузера)."""
import gzip
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import discover  # noqa: E402


class DiscoverTests(unittest.TestCase):
    def test_finds_market_list_among_noise(self):
        data = {"code": 0, "data": {
            "langs": [{"code": "en", "name": "English"}, {"code": "ru", "name": "Русский"}],
            "list": [
                {"id": 1, "title": "Will Bitcoin be above $120,000 on December 31?",
                 "outcomes": [{"name": "Yes", "price": "0.42"}, {"name": "No", "price": "0.58"}]},
                {"id": 2, "title": "Will the Fed cut rates in December?",
                 "outcomes": [{"name": "Yes", "price": "0.71"}, {"name": "No", "price": "0.29"}]},
            ]}}
        best = max(discover.find_lists(data), key=lambda c: c.score)
        self.assertEqual(best.path, "$.data.list")
        self.assertIn("outcomes[].price", best.prob_keys)
        self.assertGreaterEqual(best.score, discover.MIN_SCORE)

    def test_redaction(self):
        h = discover.redact_headers({"Cookie": "a=b", "X-Auth-Token": "t", "User-Agent": "UA"})
        self.assertEqual(h, {"Cookie": "<redacted>", "X-Auth-Token": "<redacted>", "User-Agent": "UA"})
        url = discover.redact_url("https://x.io/a?page=1&token=s3cr3t")
        self.assertNotIn("s3cr3t", url)
        self.assertIn("page=1", url)

    def test_ws_frames(self):
        msg = {"channel": "ticker", "data": {"bid": 0.41}}
        self.assertEqual(discover.decode_frame(gzip.compress(json.dumps(msg).encode())), msg)
        self.assertEqual(discover.decode_frame(json.dumps(msg)), msg)
        self.assertIsNone(discover.decode_frame(b"\x08\x96\x01"))

    def test_antik_endpoints(self):
        f = discover.find_cdp_endpoint
        # Dolphin: порт и путь отдельно
        self.assertEqual(f({"success": True, "automation": {"port": 50568,
                                                            "wsEndpoint": "/devtools/browser/abc"}}),
                         "ws://127.0.0.1:50568/devtools/browser/abc")
        # AdsPower: готовый ws в data.ws.puppeteer
        self.assertEqual(f({"code": 0, "data": {"ws": {"selenium": "127.0.0.1:61001",
                                                        "puppeteer": "ws://127.0.0.1:61001/devtools/browser/x"},
                                                 "debug_port": "61001"}}),
                         "ws://127.0.0.1:61001/devtools/browser/x")
        # только порт
        self.assertEqual(f({"uuid": "u", "debug_port": "9333"}), "http://127.0.0.1:9333")
        self.assertIsNone(f({"success": False, "error": "already running"}))
        self.assertEqual(discover.cdp_url("9222"), "http://127.0.0.1:9222")
        self.assertEqual(discover.cdp_url("127.0.0.1:9222"), "http://127.0.0.1:9222")
        self.assertEqual(discover.cdp_url("ws://127.0.0.1:1/devtools/browser/z"),
                         "ws://127.0.0.1:1/devtools/browser/z")


if __name__ == "__main__":
    unittest.main()
