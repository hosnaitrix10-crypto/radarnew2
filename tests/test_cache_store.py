import unittest

from cache_store import merge_snapshot


class MergeSnapshotTests(unittest.TestCase):
    def test_failed_or_conflicting_quote_keeps_last_good_value(self):
        previous = {
            "gerado": "2026-11-17T21:00:00Z",
            "fonte": ["brapi.dev", "Yahoo Finance"],
            "ativos": [{
                "t": "ABCD3", "cat": "acao", "nome": "Empresa ABCD",
                "catalogActive": True, "p": 12.5, "data_cotacao": "2026-11-17",
                "dados_cotacao": "verificada", "preco_fonte": "brapi.dev + Yahoo Finance",
                "historico_precos": [{"data": "2026-11-17", "p": 12.5, "fonte": "brapi.dev + Yahoo Finance"}],
                "roe": 18.0, "dados_fundamentos": "ok",
            }],
        }
        current = {
            "gerado": "2026-11-18T21:00:00Z", "catalog_complete": True,
            "fonte": ["brapi.dev", "Yahoo Finance"],
            "ativos": [{
                "t": "ABCD3", "cat": "acao", "nome": "Empresa ABCD",
                "catalogActive": True, "p": None, "dados_cotacao": "conflito",
                "precos_fontes": {"brapi.dev": 12.5, "Yahoo Finance": 16.0},
                "roe": None, "dados_fundamentos": "indisponível",
                "div": [], "dados_proventos": "indisponível",
            }],
        }
        result = merge_snapshot(current, previous)
        row = result["ativos"][0]
        self.assertEqual(row["p"], 12.5)
        self.assertEqual(row["data_cotacao"], "2026-11-17")
        self.assertEqual(row["roe"], 18.0)
        self.assertEqual(len(row["historico_precos"]), 1)
        self.assertEqual(row["cache_meta"]["quote"]["last_status"], "conflito")
        self.assertEqual(row["cache_meta"]["quote"]["last_success"], "2026-11-17")

    def test_verified_quote_updates_and_same_day_observation_is_replaced(self):
        previous = {
            "gerado": "2026-11-18T16:00:00Z", "fonte": [],
            "ativos": [{
                "t": "ABCD3", "cat": "acao", "catalogActive": True,
                "p": 12.5, "data_cotacao": "2026-11-18", "dados_cotacao": "verificada",
                "preco_fonte": "brapi.dev + Yahoo Finance",
                "historico_precos": [
                    {"data": "2026-11-17", "p": 12.0, "fonte": "dupla"},
                    {"data": "2026-11-18", "p": 12.5, "fonte": "dupla"},
                ],
            }],
        }
        current = {
            "gerado": "2026-11-18T21:00:00Z", "catalog_complete": True,
            "fonte": ["brapi.dev", "Yahoo Finance"],
            "ativos": [{
                "t": "ABCD3", "cat": "acao", "nome": "Empresa ABCD", "catalogActive": True,
                "p": 12.8, "ch": 1.2, "vol": 123456, "data_cotacao": "2026-11-18",
                "dados_cotacao": "verificada", "preco_fonte": "brapi.dev + Yahoo Finance",
            }],
        }
        row = merge_snapshot(current, previous)["ativos"][0]
        self.assertEqual(row["p"], 12.8)
        self.assertEqual(len(row["historico_precos"]), 2)
        self.assertEqual(row["historico_precos"][-1]["p"], 12.8)

    def test_complete_catalog_keeps_removed_assets_as_inactive_history(self):
        previous = {
            "gerado": "2026-11-17T21:00:00Z", "fonte": [],
            "ativos": [
                {"t": "ABCD3", "cat": "acao", "catalogActive": True, "p": 12.5},
                {"t": "WXYZ11", "cat": "fii", "catalogActive": True, "p": 100.0},
            ],
        }
        current = {
            "gerado": "2026-11-18T21:00:00Z", "catalog_complete": True,
            "fonte": ["brapi.dev"],
            "ativos": [{"t": "ABCD3", "cat": "acao", "catalogActive": True,
                        "p": 12.5, "dados_cotacao": "verificada",
                        "preco_fonte": "brapi.dev + Yahoo Finance"}],
        }
        result = merge_snapshot(current, previous)
        by_ticker = {r["t"]: r for r in result["ativos"]}
        self.assertFalse(by_ticker["WXYZ11"]["catalogActive"])
        self.assertIn("catalogRemovedAt", by_ticker["WXYZ11"])
        self.assertEqual(by_ticker["WXYZ11"]["p"], 100.0)
        self.assertEqual([r["t"] for r in result["acoes"]], ["ABCD3"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["total_historico"], 2)

    def test_incomplete_catalog_does_not_mark_absent_assets_removed(self):
        previous = {"ativos": [{"t": "ABCD3", "cat": "acao", "catalogActive": True}]}
        current = {"gerado": "2026-11-18T21:00:00Z", "catalog_complete": False,
                   "ativos": [{"t": "WXYZ11", "cat": "fii", "catalogActive": True}]}
        result = merge_snapshot(current, previous)
        by_ticker = {r["t"]: r for r in result["ativos"]}
        self.assertTrue(by_ticker["ABCD3"]["catalogActive"])
        self.assertNotIn("catalogRemovedAt", by_ticker["ABCD3"])

    def test_invalid_numbers_are_not_accepted_as_quotes(self):
        previous = {"ativos": [{"t": "ABCD3", "cat": "acao", "p": 12.5,
                                "dados_cotacao": "verificada"}]}
        current = {"gerado": "2026-11-18T21:00:00Z", "catalog_complete": True,
                   "ativos": [{"t": "ABCD3", "cat": "acao", "p": 0,
                               "dados_cotacao": "verificada"}]}
        row = merge_snapshot(current, previous)["ativos"][0]
        self.assertEqual(row["p"], 12.5)

    def test_single_source_candidate_is_not_published_as_a_quote(self):
        current = {
            "gerado": "2026-11-18T21:00:00Z", "catalog_complete": True,
            "ativos": [{"t": "ABCD3", "cat": "acao", "p": 12.5,
                        "dados_cotacao": "uma_fonte",
                        "preco_candidato": {"Yahoo Finance": 12.5}}],
        }
        row = merge_snapshot(current, None)["ativos"][0]
        self.assertNotIn("p", row)
        self.assertEqual(row["dados_cotacao"], "uma_fonte")
        self.assertIn("preco_candidato", row)


if __name__ == "__main__":
    unittest.main()
