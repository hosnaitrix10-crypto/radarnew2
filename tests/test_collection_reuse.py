import datetime as dt
import unittest
from unittest.mock import patch

import coleta


class SameDayReuseTests(unittest.TestCase):
    def setUp(self):
        self.previous_rows = coleta.PREVIOUS_ROWS
        coleta.PREVIOUS_ROWS = {}
        self.today = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def tearDown(self):
        coleta.PREVIOUS_ROWS = self.previous_rows

    def test_successful_quote_is_reused_without_downloading_again(self):
        coleta.PREVIOUS_ROWS = {
            "ABCD3": {
                "t": "ABCD3", "p": 12.5, "ch": 1.0, "vol": 1000,
                "data_cotacao": self.today[:10], "cotacao_em": self.today,
                "preco_fonte": "brapi.dev validada contra Yahoo Finance",
                "dados_cotacao": "verificada",
                "cache_meta": {"quote": {"last_attempt": self.today, "last_success": self.today, "last_status": "ok"}},
            }
        }
        with patch.object(coleta.yf, "download", side_effect=AssertionError("não deveria consultar Yahoo hoje")) as download:
            rows = coleta.batch_precos([{"t": "ABCD3", "quote_candidate_brapi": 12.5}])
        self.assertEqual(rows[0]["p"], 12.5)
        self.assertEqual(rows[0]["dados_cotacao"], "verificada")
        download.assert_not_called()

    def test_failed_quote_attempt_is_not_repeated_the_same_day(self):
        coleta.PREVIOUS_ROWS = {
            "ABCD3": {
                "t": "ABCD3", "p": 11.0, "data_cotacao": "2026-11-17",
                "dados_cotacao": "conflito", "preco_conflito": True,
                "preco_candidato": {"brapi.dev": 12.5, "Yahoo Finance": 15.0},
                "cache_meta": {"quote": {"last_attempt": self.today, "last_status": "conflito"}},
            }
        }
        with patch.object(coleta.yf, "download", side_effect=AssertionError("não deveria repetir tentativa hoje")) as download:
            rows = coleta.batch_precos([{"t": "ABCD3", "quote_candidate_brapi": 12.5}])
        self.assertEqual(rows[0]["p"], 11.0)
        self.assertEqual(rows[0]["dados_cotacao"], "conflito")
        self.assertTrue(rows[0]["preco_conflito"])
        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
