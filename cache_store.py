"""Merge de snapshots públicos com preservação de último valor confiável.

Este módulo é deliberadamente conservador: uma consulta vazia, inválida ou
conflitante atualiza o estado da tentativa, mas nunca apaga o último dado válido.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
from typing import Any

QUOTE_FIELDS = (
    "p", "ch", "vol", "data_cotacao", "desempenho_1a", "desempenho_3a",
    "cotacao_em", "preco_fonte",
)
FUNDAMENTAL_FIELDS = ("pl", "pvp", "dy", "roe", "ml", "liq", "dbp", "cr", "ffoy")
DIVIDEND_FIELDS = ("dpa", "ap", "div", "dados_proventos")
CATALOG_FIELDS = (
    "nome", "cat", "setor", "assetType", "subType", "fonte_catalogo",
    "catalogActive", "logoUrl", "exchange", "currency", "subsector",
    "catalogo_consultado_em", "dados_completos",
)
MAX_PRICE_HISTORY = 30


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _finite(value) and value > 0


def _nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _source(row: dict[str, Any], group: str) -> str:
    if group == "quote":
        return str(row.get("preco_fonte") or row.get("fonte_cotacao") or "")
    if group == "fundamentals":
        return str(row.get("fonte_fundamentos") or "Fundamentus")
    if group == "dividends":
        return str(row.get("fonte_proventos") or "Yahoo Finance")
    return str(row.get("fonte_catalogo") or "")


def _group_status(row: dict[str, Any], group: str) -> str:
    if group == "quote":
        return str(row.get("dados_cotacao") or "indisponível")
    if group == "fundamentals":
        return str(row.get("dados_fundamentos") or "indisponível")
    if group == "dividends":
        return str(row.get("dados_proventos") or "indisponível")
    return "ok"


def _group_ok(row: dict[str, Any], group: str) -> bool:
    status = _group_status(row, group).lower()
    if group == "quote":
        return (status in {"verificada", "verificado", "ok"}
                and _positive(row.get("p")) and bool(_source(row, "quote")))
    if group == "fundamentals":
        return status == "ok" and any(_finite(row.get(k)) for k in FUNDAMENTAL_FIELDS)
    if group == "dividends":
        return status == "ok" and isinstance(row.get("div"), list) and bool(row["div"])
    return False


def _set_group_meta(cache: dict[str, Any], row: dict[str, Any], group: str,
                    generated: str, previous: dict[str, Any] | None) -> None:
    prior = deepcopy((previous or {}).get("cache_meta", {}).get(group, {}))
    if not prior.get("last_success") and previous:
        legacy_time = {
            "quote": previous.get("cotacao_em") or previous.get("data_cotacao"),
            "fundamentals": previous.get("fundamentos_em"),
            "dividends": previous.get("proventos_em"),
        }.get(group)
        if legacy_time:
            prior["last_success"] = legacy_time
    status = _group_status(row, group)
    prior["last_attempt"] = generated
    prior["last_status"] = status
    prior["source"] = _source(row, group) or prior.get("source", "unknown")
    if _group_ok(row, group):
        prior["last_success"] = row.get("cotacao_em" if group == "quote" else "fundamentos_em" if group == "fundamentals" else "proventos_em") or generated
        prior["last_status"] = "ok"
    cache[group] = prior


def _price_observation(row: dict[str, Any], generated: str) -> dict[str, Any] | None:
    if not _positive(row.get("p")):
        return None
    day = row.get("data_cotacao")
    if not isinstance(day, str) or not day:
        day = generated[:10]
    return {
        "data": day,
        "p": row["p"],
        "fonte": _source(row, "quote") or "fonte não informada",
    }


def merge_snapshot(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Combina uma coleta nova com o último snapshot conhecido.

    `current['catalog_complete']` só deve ser true se a enumeração de todas as
    páginas/tipos tiver sido verificada. Dados sem validação ficam no estado
    de tentativa, não substituem um valor publicado anteriormente.
    """
    generated = str(current.get("gerado") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    old_doc = previous or {}
    old_rows = {str(r.get("t", "")).upper(): r for r in old_doc.get("ativos", []) if r.get("t")}
    new_rows = {str(r.get("t", "")).upper(): r for r in current.get("ativos", []) if r.get("t")}
    complete = current.get("catalog_complete") is True
    merged_rows: list[dict[str, Any]] = []

    for ticker in sorted(set(old_rows) | set(new_rows)):
        old = deepcopy(old_rows.get(ticker, {}))
        fresh = deepcopy(new_rows.get(ticker, {}))
        row = old
        row["t"] = ticker

        if ticker in new_rows:
            for key in CATALOG_FIELDS:
                value = fresh.get(key)
                if _nonempty(value):
                    row[key] = value
            if complete:
                row["catalogActive"] = fresh.get("catalogActive") is not False
                if row["catalogActive"]:
                    row.pop("catalogRemovedAt", None)
                else:
                    row["catalogRemovedAt"] = row.get("catalogRemovedAt") or generated
            elif "catalogActive" not in row:
                row["catalogActive"] = True

            for key in ("precos_fontes", "preco_conflito", "preco_candidato", "erro_cotacao"):
                if key in fresh:
                    row[key] = fresh[key]

            groups = {
                "quote": (QUOTE_FIELDS, "dados_cotacao"),
                "fundamentals": (FUNDAMENTAL_FIELDS, "dados_fundamentos"),
                "dividends": (DIVIDEND_FIELDS, "dados_proventos"),
            }
            for group, (fields, status_key) in groups.items():
                success = _group_ok(fresh, group)
                if success:
                    for key in fields:
                        value = fresh.get(key)
                        if _nonempty(value):
                            row[key] = deepcopy(value)
                    if group == "quote":
                        for key in ("preco_conflito", "preco_candidato", "erro_cotacao"):
                            row.pop(key, None)
                        obs = _price_observation(fresh, generated)
                        if obs:
                            history = list(row.get("historico_precos") or [])
                            history = [x for x in history if x.get("data") != obs["data"]]
                            history.append(obs)
                            row["historico_precos"] = history[-MAX_PRICE_HISTORY:]
                else:
                    # Não publique o candidato inválido/conflitante como se fosse preço.
                    if group == "quote" and not old:
                        row.pop("p", None)
                    elif group == "fundamentals" and not old:
                        for key in fields:
                            row.pop(key, None)
                    elif group == "dividends" and not old:
                        for key in fields:
                            row.pop(key, None)
                row[status_key] = fresh.get(status_key, "indisponível")

            row["cache_meta"] = deepcopy(row.get("cache_meta") or {})
            for group in ("quote", "fundamentals", "dividends"):
                _set_group_meta(row["cache_meta"], fresh, group, generated, old)
            row["ultimo_check"] = generated
        elif complete:
            row["catalogActive"] = False
            row["catalogRemovedAt"] = row.get("catalogRemovedAt") or generated
            row["catalog_status"] = "ausente em catálogo completo; mantido no histórico"

        if row:
            merged_rows.append(row)

    active = [r for r in merged_rows if r.get("catalogActive") is not False]
    categories = sorted({str(r.get("cat") or "outro") for r in active})
    doc = {k: deepcopy(v) for k, v in current.items() if k not in {"ativos", "acoes"}}
    doc.update({
        "cache_version": 1,
        "gerado": generated,
        "total": len(active),
        "total_historico": len(merged_rows),
        "catalog_complete": complete,
        "resumo_categorias": {c: sum(1 for r in active if r.get("cat") == c) for c in categories},
        "ativos": merged_rows,
        "acoes": [deepcopy(r) for r in active if r.get("cat") == "acao"],
    })
    old_sources = old_doc.get("fonte", [])
    new_sources = current.get("fonte", [])
    if isinstance(old_sources, str):
        old_sources = [old_sources]
    if isinstance(new_sources, str):
        new_sources = [new_sources]
    doc["fonte"] = list(dict.fromkeys([*old_sources, *new_sources]))
    return doc
