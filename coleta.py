#!/usr/bin/env python3
"""Coleta catálogo multiclasse e dados de mercado para o Radar.

Catálogo de descoberta: diretório público CotaLab (ações, FIIs, ETFs e BDRs).
Fundamentos: Fundamentus (ações e FIIs) + enriquecimento opcional Status Invest.
Histórico de preços/proventos: Yahoo Finance via yfinance.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
from io import StringIO
import json
import math
from pathlib import Path
import re
import time

import pandas as pd
import requests
import yfinance as yf

OUT = Path("dados.json")
CATALOGO = Path("catalogo_ativos.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8",
}

TIMEOUT = 35
HISTORICO_ANOS = 3
BATCH_COTACOES = 50
MAX_PAGINAS = 60
MAX_SI_ENRICH = 120  # Limite por execução para não estourar tempo do GitHub Actions

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

TICKER_ACAO_RE = re.compile(r"^[A-Z]{4}(3|4|5|6|11)$")


def numero(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not pd.isna(v):
        n = float(v)
        return n if math.isfinite(n) else None
    s = str(v).strip().replace("%", "").replace("R$", "").replace(" ", "")
    if not s or s.lower() in {"nan", "none", "-", "--", "n/d"}:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        n = float(s)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def ticker_from(text):
    s = str(text).strip().upper()
    m = re.match(r"^([A-Z0-9]{2,4}\d{1,2})", s)
    if m:
        ticker = m.group(1)
        return ticker if any(ch.isdigit() for ch in ticker) else None
    return None


def parse_catalog_table(table):
    df = table.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    out = []
    for _, row in df.iterrows():
        vals = [
            str(x).strip()
            for x in row.tolist()
            if x is not None and str(x).strip() not in {"", "nan", "None"}
        ]
        if not vals:
            continue
        ticker = None
        ticker_cell = ""
        for v in vals:
            t = ticker_from(v)
            if t:
                ticker = t
                ticker_cell = v
                break
        if not ticker:
            continue
        joined = " | ".join(vals).lower()
        cat = None
        for needle, canon in [
            ("acoes", "acao"),
            ("ações", "acao"),
            ("fiis", "fii"),
            ("etfs", "etf"),
            ("bdrs", "bdr"),
        ]:
            if needle in joined:
                cat = canon
                break
        if not cat:
            continue
        nome = ""
        if ticker_cell.upper() != ticker:
            nome = re.sub(r"^" + re.escape(ticker) + r"\s*", "", ticker_cell, flags=re.I).strip(" -–|")
        if not nome:
            for v in vals:
                if v != ticker_cell and not any(
                    x in v.lower() for x in ["acoes", "ações", "fiis", "etfs", "bdrs"]
                ):
                    if not re.fullmatch(r"[A-Z]{2,4}", v.strip()):
                        nome = v
                        break
        setor = vals[-1] if len(vals) > 2 else ""
        if setor.lower() in {"acoes", "ações", "fiis", "etfs", "bdrs"}:
            setor = ""
        out.append({
            "t": ticker,
            "nome": nome or ticker,
            "cat": cat,
            "setor": setor,
            "fonte_catalogo": "CotaLab",
        })
    return out


def carregar_catalogo():
    catalogo = []
    vistos_paginas = set()
    total_pag = None
    for pagina in range(1, MAX_PAGINAS + 1):
        urls = (
            ["https://www.cotalab.com.br/ativos"]
            if pagina == 1
            else [
                f"https://www.cotalab.com.br/ativos/pagina/{pagina}",
                f"https://www.cotalab.com.br/ativos?page={pagina}",
                f"https://www.cotalab.com.br/ativos?pagina={pagina}",
            ]
        )
        r = None
        ultimo_erro = None
        for url in urls:
            try:
                tentativa = SESSION.get(url, timeout=TIMEOUT, allow_redirects=True)
                if tentativa.status_code == 404:
                    continue
                tentativa.raise_for_status()
                r = tentativa
                if (
                    pagina == 1
                    or url == r.url
                    or ("pagina" in r.url.lower())
                    or ("page=" in r.url.lower())
                ):
                    break
            except Exception as e:
                ultimo_erro = e
        if r is None:
            if pagina > 1:
                break
            raise RuntimeError(f"Não foi possível acessar o catálogo CotaLab: {ultimo_erro}")
        texto = r.text
        m = re.search(r"(?:Página|Pagina)\s*\d+\s*de\s*(\d+)", texto, re.I)
        if m:
            total_pag = min(MAX_PAGINAS, int(m.group(1)))
        numeros = []
        for padrao in [r"/ativos/pagina/(\d+)", r"[?&]page=(\d+)", r"[?&]pagina=(\d+)"]:
            numeros.extend(int(x) for x in re.findall(padrao, texto, re.I))
        if numeros:
            total_pag = min(MAX_PAGINAS, max([total_pag or 1] + numeros))
        try:
            tabelas = pd.read_html(StringIO(texto))
        except ValueError:
            tabelas = []
        candidatos = []
        for tab in tabelas:
            cols = " ".join(str(c).lower() for c in tab.columns)
            if any(k in cols for k in ["ticker", "categoria", "nome"]) or len(tab) >= 10:
                achados = parse_catalog_table(tab)
                if len(achados) >= len(candidatos):
                    candidatos = achados
        chaves = frozenset(a["t"] for a in candidatos)
        print(
            f"[CATÁLOGO] página {pagina}/{total_pag or '?'}: {len(candidatos)} ativos "
            f"(HTTP {r.status_code}; {r.url})",
            flush=True,
        )
        if not candidatos or not chaves:
            if pagina == 1:
                raise RuntimeError("Não foi possível interpretar a tabela de ativos do catálogo.")
            break
        if chaves in vistos_paginas:
            print("[CATÁLOGO] Página repetida; encerrando varredura de paginação.", flush=True)
            break
        vistos_paginas.add(chaves)
        catalogo.extend(candidatos)
        if total_pag is not None and pagina >= total_pag:
            break
        time.sleep(0.25)

    unicos = {}
    for a in catalogo:
        unicos[a["t"]] = a
    resultado = sorted(unicos.values(), key=lambda a: a["t"])
    if len(resultado) < 100:
        if CATALOGO.exists():
            antigo = json.loads(CATALOGO.read_text(encoding="utf-8"))
            if len(antigo.get("ativos", [])) > len(resultado):
                print("[AVISO] Catálogo atual incompleto; preservando catálogo anterior.", flush=True)
                return antigo["ativos"]
        raise RuntimeError(f"Catálogo incompleto ({len(resultado)} ativos); abortando.")

    CATALOGO.write_text(
        json.dumps(
            {
                "gerado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "fonte": "CotaLab — diretório público; validar contra cadastro oficial B3",
                "total": len(resultado),
                "ativos": resultado,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    return resultado


def col_by(df, needles):
    for c in df.columns:
        s = str(c).lower()
        if any(n in s for n in needles):
            return c
    return None


def carregar_fundamentos(url, cat):
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        tabs = pd.read_html(StringIO(r.text), decimal=",", thousands=".")
        if not tabs:
            return {}
        df = max(tabs, key=lambda t: len(t))
        ct = col_by(df, ["papel", "ticker"])
        if ct is None:
            return {}
        mapping = {
            "p": ["cota", "cotação", "cotacao"],
            "pl": ["p/l"],
            "pvp": ["p/vp"],
            "dy": ["div. yield", "div yield", "dividend yield", "div."],
            "ml": ["mrg. l", "margem líquida", "margem liquida"],
            "roe": ["roe"],
            "liq": ["liq.2", "liquidez"],
            "dbp": ["dív/bruta", "div/bruta", "dívida/bruta", "divida/bruta"],
            "cr": ["cres. 5a", "crescimento"],
            "ffoy": ["ffo yield"],
        }
        out = {}
        for _, row in df.iterrows():
            t = str(row[ct]).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{4,6}", t):
                continue
            a = {"t": t, "cat": cat}
            for k, needles in mapping.items():
                c = col_by(df, needles)
                if c is not None:
                    a[k] = numero(row[c])
            if a.get("p") is not None and a["p"] <= 0:
                a["p"] = None
            out[t] = a
        return out
    except Exception as e:
        print(f"[AVISO] Fundamentos {cat} indisponíveis: {e}", flush=True)
        return {}


def raspar_status_invest_acao(ticker: str) -> dict | None:
    """Extrai indicadores básicos de /acoes/<ticker> no Status Invest sem derrubar a coleta."""
    url = f"https://statusinvest.com.br/acoes/{ticker.lower()}"
    try:
        r = SESSION.get(url, timeout=15, headers={"Referer": "https://statusinvest.com.br/"})
        if r.status_code != 200:
            return None
        html = r.text
        texto = re.sub(r"<[^>]+>", " ", html)
        texto = re.sub(r"\s+", " ", texto)

        def pick(padroes):
            for rx in padroes:
                m = re.search(rx, texto, flags=re.I)
                if m:
                    val = numero(m.group(1))
                    if val is not None:
                        return val
            return None

        dy = pick([r"D\.?Y\.?\s*\(12m\)\s*(-?\d[\d.,]*)\s*%?", r"Dividend\s*Yield\s*(-?\d[\d.,]*)\s*%?"])
        pl = pick([r"\bP/L\b\s*(-?\d[\d.,]*)"])
        pvp = pick([r"\bP/VP\b\s*(-?\d[\d.,]*)"])
        roe = pick([r"\bROE\b\s*(-?\d[\d.,]*)\s*%?"])
        roic = pick([r"\bROIC\b\s*(-?\d[\d.,]*)\s*%?"])
        ml = pick([r"Margem\s+L[íi]quida\s*(-?\d[\d.,]*)\s*%?"])

        if all(x is None for x in (dy, pl, pvp, roe, roic, ml)):
            return None
        return {
            "dy12": dy,
            "pl": pl,
            "pvp": pvp,
            "roe": roe,
            "roic": roic,
            "ml": ml,
        }
    except Exception:
        return None


def enriquecer_status_invest(ativos: list[dict]):
    """Completa lacunas de fundamentos de ações com Status Invest quando necessário."""
    alvos = [
        a
        for a in ativos
        if a.get("cat") == "acao"
        and TICKER_ACAO_RE.match(a.get("t", ""))
        and (a.get("pl") is None or a.get("pvp") is None or a.get("roe") is None or a.get("dy") is None)
    ][:MAX_SI_ENRICH]

    if not alvos:
        return

    ok = 0
    for a in alvos:
        dados = raspar_status_invest_acao(a["t"])
        if dados:
            if a.get("pl") is None and dados.get("pl") is not None:
                a["pl"] = dados["pl"]
            if a.get("pvp") is None and dados.get("pvp") is not None:
                a["pvp"] = dados["pvp"]
            if a.get("roe") is None and dados.get("roe") is not None:
                a["roe"] = dados["roe"]
            if a.get("dy") is None and dados.get("dy12") is not None:
                a["dy"] = dados["dy12"]
            a["dy12"] = dados.get("dy12") if dados.get("dy12") is not None else a.get("dy")
            a["fu"] = {
                "roic": dados.get("roic"),
                "ml": dados.get("ml") if dados.get("ml") is not None else a.get("ml"),
            }
            a["si_ok"] = True
            ok += 1
        time.sleep(0.5)
    print(f"[STATUS INVEST] Enriquecidos {ok}/{len(alvos)} ativos com lacunas.", flush=True)


def batch_precos(ativos):
    tickers = [a["t"] + ".SA" for a in ativos]
    for ini in range(0, len(tickers), BATCH_COTACOES):
        grupo = tickers[ini : ini + BATCH_COTACOES]
        try:
            hist = yf.download(
                tickers=grupo,
                period=f"{HISTORICO_ANOS}y",
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                threads=True,
                progress=False,
                timeout=TIMEOUT,
            )
            if hist is None or hist.empty:
                continue
            for a, ticker in zip(ativos[ini : ini + BATCH_COTACOES], grupo):
                try:
                    if isinstance(hist.columns, pd.MultiIndex):
                        if ticker in hist.columns.get_level_values(0):
                            one = hist[ticker]
                        elif ticker in hist.columns.get_level_values(1):
                            one = hist.xs(ticker, axis=1, level=1)
                        else:
                            continue
                    else:
                        one = hist
                    close = one["Close"].dropna() if "Close" in one else pd.Series(dtype=float)
                    if close.empty:
                        continue
                    first = float(close.iloc[0])
                    last = float(close.iloc[-1])
                    prev = float(close.iloc[-2]) if len(close) > 1 else last
                    if not math.isfinite(last) or last <= 0:
                        continue
                    a["p"] = round(last, 6)
                    a["ch"] = round((last / prev - 1) * 100, 2) if prev > 0 else 0.0
                    a["desempenho_3a"] = round((last / first - 1) * 100, 2) if first > 0 else None
                    one_year = close[close.index >= pd.Timestamp.now(tz=None) - pd.DateOffset(years=1)]
                    if len(one_year) > 0:
                        a["lo"] = round(float(one_year.min()), 4)
                        a["hi"] = round(float(one_year.max()), 4)
                        if len(one_year) > 1 and float(one_year.iloc[0]) > 0:
                            a["desempenho_1a"] = round((last / float(one_year.iloc[0]) - 1) * 100, 2)
                    a["data_cotacao"] = str(close.index[-1].date())
                except Exception:
                    continue
        except Exception as e:
            print(f"[AVISO] Lote de cotações {ini+1}-{min(ini+len(grupo),len(tickers))}: {e}", flush=True)
        if (ini // BATCH_COTACOES + 1) % 5 == 0:
            print(f"[COTAÇÕES] {min(ini+len(grupo),len(tickers))}/{len(tickers)} tickers processados", flush=True)


def dividendos_ticker(a, agora):
    if a["cat"] not in {"acao", "fii"}:
        return a
    try:
        s = yf.Ticker(a["t"] + ".SA").dividends
        if s is None or s.empty:
            a.setdefault("dpa", None)
            a.setdefault("ap", 0)
            a.setdefault("div", [])
            a["dados_proventos"] = "sem histórico"
            return a
        idx = pd.DatetimeIndex(s.index)
        idx = idx.tz_localize(None) if idx.tz is not None else idx
        s.index = idx
        inicio = agora - pd.DateOffset(years=HISTORICO_ANOS)
        rec = s[s.index >= inicio]
        anos = len(set(int(y) for y in rec.index.year))
        a["ap"] = min(HISTORICO_ANOS, anos)
        a["dpa"] = round(float(rec.sum()) / anos, 4) if anos else None
        a["div"] = [
            {"v": float(v), "d": d.strftime("%Y-%m-%d")}
            for d, v in s.items()
            if math.isfinite(float(v)) and d >= agora - pd.DateOffset(months=18)
        ]
        a["dados_proventos"] = "ok" if anos else "sem histórico"
    except Exception as e:
        a.setdefault("dpa", None)
        a.setdefault("ap", 0)
        a.setdefault("div", [])
        a["dados_proventos"] = "indisponível"
        a["erro_proventos"] = str(e)[:160]
    return a


def main():
    agora = pd.Timestamp.now().tz_localize(None)
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)

    print("[1/5] Descobrindo catálogo multiclasse...", flush=True)
    catalogo = carregar_catalogo()
    print(f"[CATÁLOGO] {len(catalogo)} instrumentos únicos.", flush=True)

    print("[2/5] Carregando fundamentos de ações e FIIs...", flush=True)
    fund = {}
    fund.update(carregar_fundamentos("https://www.fundamentus.com.br/resultado.php", "acao"))
    fund.update(carregar_fundamentos("https://www.fundamentus.com.br/fii_resultado.php", "fii"))

    for a in catalogo:
        f = fund.get(a["t"], {})
        a.update({k: v for k, v in f.items() if k not in {"t", "cat"}})
        a.setdefault("p", None)
        a.setdefault("ch", None)
        a.setdefault("lo", None)
        a.setdefault("hi", None)
        a.setdefault("pl", None)
        a.setdefault("pvp", None)
        a.setdefault("dy", None)
        a.setdefault("dy12", a.get("dy"))
        a.setdefault("roe", None)
        a.setdefault("ml", None)
        a.setdefault("liq", None)
        a.setdefault("dpa", None)
        a.setdefault("ap", 0)
        a.setdefault("div", [])
        a.setdefault("fu", {})
        a["dados_fundamentos"] = "ok" if f else "indisponível"

    enriquecer_status_invest(catalogo)

    print("[3/5] Obtendo histórico de preços em lotes de 50 tickers...", flush=True)
    batch_precos(catalogo)

    print("[4/5] Buscando proventos para ações e FIIs...", flush=True)
    alvo = [a for a in catalogo if a["cat"] in {"acao", "fii"}]
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(dividendos_ticker, a, agora) for a in alvo]
        for i, f in enumerate(cf.as_completed(futs), 1):
            try:
                f.result()
            except Exception as e:
                print(f"[AVISO] Proventos: {e}", flush=True)
            if i % 100 == 0:
                print(f"[PROVENTOS] {i}/{len(alvo)} ativos", flush=True)

    for a in catalogo:
        if a.get("p") is not None:
            a["detAt"] = now_ms
            a["qAt"] = now_ms
            if a.get("div"):
                a["dAt"] = now_ms
        a["dados_completos"] = bool(
            a.get("p") is not None and (a.get("dados_fundamentos") == "ok" or a.get("si_ok") or a["cat"] in {"etf", "bdr"})
        )

    gerado = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    doc = {
        "gerado": gerado,
        "gerado_ms": now_ms,
        "fonte": [
            "CotaLab (catálogo secundário)",
            "Fundamentus (fundamentos)",
            "Status Invest (complemento de fundamentos)",
            "Yahoo Finance/yfinance (cotações e proventos)",
        ],
        "historico_anos": HISTORICO_ANOS,
        "total": len(catalogo),
        "resumo_categorias": {
            c: sum(1 for a in catalogo if a["cat"] == c) for c in ["acao", "fii", "etf", "bdr"]
        },
        "ativos": catalogo,
        "acoes": [a for a in catalogo if a["cat"] == "acao"],
    }

    temp = OUT.with_suffix(".json.tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    json.loads(temp.read_text(encoding="utf-8"))
    temp.replace(OUT)
    print(f"[5/5] Concluído: {len(catalogo)} ativos; resumo {doc['resumo_categorias']}.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERRO] Coleta interrompida; dados.json anterior preservado: {e}", flush=True)
        raise
