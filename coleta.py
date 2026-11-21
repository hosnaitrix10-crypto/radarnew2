#!/usr/bin/env python3
"""Coleta catálogo multiclasse e dados de mercado para o Radar.

Catálogo de descoberta: diretório público CotaLab (ações, FIIs, ETFs e BDRs).
Fundamentos: Fundamentus (ações e FIIs) + enriquecimento Status Invest.
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

from lxml import html as lxml_html
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
    "Referer": "https://statusinvest.com.br/",
}

TIMEOUT = 35
HISTORICO_ANOS = 3
BATCH_COTACOES = 50
MAX_PAGINAS = 60
MAX_SI_ENRICH = 600
MAX_SI_WORKERS = 6

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

TICKER_ACAO_RE = re.compile(r"^[A-Z]{4}(3|4|5|6|11)$")
TICKER_FII_RE = re.compile(r"^[A-Z]{4}11$")

SETOR_MAP = {
    "finance": "Finance",
    "financeiro": "Finance",
    "bancos": "Finance",
    "seguros": "Finance",
    "energy minerals": "Energy Minerals",
    "petroleo": "Energy Minerals",
    "petróleo": "Energy Minerals",
    "non-energy minerals": "Non-Energy Minerals",
    "mineracao": "Non-Energy Minerals",
    "mineração": "Non-Energy Minerals",
    "siderurgia": "Non-Energy Minerals",
    "utilities": "Utilities",
    "utilidade publica": "Utilities",
    "utilidade pública": "Utilities",
    "energia eletrica": "Utilities",
    "energia elétrica": "Utilities",
    "saneamento": "Utilities",
    "consumer non-durables": "Consumer Non-Durables",
    "consumo nao duravel": "Consumer Non-Durables",
    "consumo não durável": "Consumer Non-Durables",
    "alimentos": "Consumer Non-Durables",
    "bebidas": "Consumer Non-Durables",
    "technology services": "Technology Services",
    "servicos de tecnologia": "Technology Services",
    "serviços de tecnologia": "Technology Services",
}


def normalizar_setor(raw: str | None) -> str:
    if not raw:
        return ""
    limpo = re.sub(r"\s+", " ", str(raw).strip())
    if not limpo or limpo.lower() in {"-", "--", "n/d", "none", "nan", "outros", "sem classificação"}:
        return ""
    chave = limpo.lower()
    if chave in SETOR_MAP:
        return SETOR_MAP[chave]
    for k, v in SETOR_MAP.items():
        if k in chave:
            return v
    return limpo


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
        setor = ""
        for v in reversed(vals):
            if v == ticker_cell or v == nome:
                continue
            if v.lower() in {"acoes", "ações", "fiis", "etfs", "bdrs"}:
                continue
            if numero(v) is not None:
                continue
            setor = normalizar_setor(v)
            if setor:
                break
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


def _extrair_blocos_lxml(html_str: str) -> dict[str, float]:
    encontrados: dict[str, float] = {}
    try:
        tree = lxml_html.fromstring(html_str)
    except Exception:
        return encontrados

    for node in tree.xpath("//*[self::div or self::li or self::tr]"):
        txt_parts = [re.sub(r"\s+", " ", x).strip() for x in node.xpath(".//text()") if x and x.strip()]
        if len(txt_parts) < 2:
            continue
        joined = " ".join(txt_parts)
        if len(joined) > 160:
            continue
        rotulo = " ".join(txt_parts[:-1]).lower()
        valor = numero(txt_parts[-1])
        if valor is None and len(txt_parts) >= 3:
            valor = numero(txt_parts[-2])
        if valor is not None and rotulo:
            encontrados.setdefault(rotulo, valor)
    return encontrados


def raspar_status_invest(ticker: str, cat: str = "acao") -> dict | None:
    rota = "fiis" if cat == "fii" else "acoes"
    url = f"https://statusinvest.com.br/{rota}/{ticker.lower()}"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        if r.status_code != 200 or not r.text:
            return None
        html_str = r.text
        blocos = _extrair_blocos_lxml(html_str)

        texto = re.sub(r"<[^>]+>", " ", html_str)
        texto = re.sub(r"\s+", " ", texto)

        def pick_blocos(chaves):
            for chave in chaves:
                for k, v in blocos.items():
                    if chave in k:
                        return v
            return None

        def pick_rx(padroes):
            for rx in padroes:
                m = re.search(rx, texto, flags=re.I)
                if m:
                    val = numero(m.group(1))
                    if val is not None:
                        return val
            return None

        dy = pick_blocos(["d.y (12m)", "dy (12m)", "dividend yield", "dividendo / cota"])
        if dy is None:
            dy = pick_rx([r"D\.?Y\.?\s*\(12m\)\s*(-?\d[\d.,]*)\s*%?", r"Dividend\s*Yield\s*(-?\d[\d.,]*)\s*%?"])

        pl = pick_blocos(["p/l"])
        if pl is None:
            pl = pick_rx([r"\bP/L\b\s*(-?\d[\d.,]*)"])

        pvp = pick_blocos(["p/vp"])
        if pvp is None:
            pvp = pick_rx([r"\bP/VP\b\s*(-?\d[\d.,]*)"])

        roe = pick_blocos(["roe"])
        if roe is None:
            roe = pick_rx([r"\bROE\b\s*(-?\d[\d.,]*)\s*%?"])

        roic = pick_blocos(["roic"])
        if roic is None:
            roic = pick_rx([r"\bROIC\b\s*(-?\d[\d.,]*)\s*%?"])

        ml = pick_blocos(["m. líquida", "margem líquida", "margem liquida"])
        if ml is None:
            ml = pick_rx([r"Margem\s+L[íi]quida\s*(-?\d[\d.,]*)\s*%?"])

        dle = pick_blocos(["d.líq./ebitda", "dív. líq./ebitda", "div. liq./ebitda"])
        if dle is None:
            dle = pick_rx([r"D[íi]v\.?\s*L[íi]q\.?\s*/\s*EBITDA\s*(-?\d[\d.,]*)"])

        lpa = pick_blocos(["lpa"])
        if lpa is None:
            lpa = pick_rx([r"\bLPA\b\s*R?\$?\s*(-?\d[\d.,]*)"])

        vpa = pick_blocos(["vpa"])
        if vpa is None:
            vpa = pick_rx([r"\bVPA\b\s*R?\$?\s*(-?\d[\d.,]*)"])

        if all(x is None for x in (dy, pl, pvp, roe, roic, ml, dle, lpa, vpa)):
            return None

        return {
            "dy12": dy,
            "pl": pl,
            "pvp": pvp,
            "roe": roe,
            "roic": roic,
            "ml": ml,
            "dle": dle,
            "lpa": lpa,
            "vpa": vpa,
            "fonte": "Status Invest",
        }
    except Exception:
        return None


def _aplicar_si_no_ativo(a: dict, dados: dict) -> None:
    if dados.get("pl") is not None:
        a["pl"] = dados["pl"]
    if dados.get("pvp") is not None:
        a["pvp"] = dados["pvp"]
    if dados.get("roe") is not None:
        a["roe"] = dados["roe"]
    if dados.get("dy12") is not None:
        a["dy12"] = dados["dy12"]
        if a.get("dy") is None:
            a["dy"] = dados["dy12"]
    if dados.get("ml") is not None:
        a["ml"] = dados["ml"]
    fu = dict(a.get("fu") or {})
    for k_src, k_dst in [("roic", "roic"), ("ml", "ml"), ("dle", "dle"), ("lpa", "lpa"), ("vpa", "vpa")]:
        if dados.get(k_src) is not None:
            fu[k_dst] = dados[k_src]
    a["fu"] = fu
    a["si"] = dados
    a["si_ok"] = True


def enriquecer_status_invest(ativos: list[dict]):
    alvos = [
        a
        for a in ativos
        if (
            (a.get("cat") == "acao" and TICKER_ACAO_RE.match(a.get("t", "")))
            or (a.get("cat") == "fii" and TICKER_FII_RE.match(a.get("t", "")))
        )
    ][:MAX_SI_ENRICH]

    if not alvos:
        return

    ok = 0

    def _worker(item):
        res = raspar_status_invest(item["t"], item.get("cat", "acao"))
        time.sleep(0.15)
        return item, res

    with cf.ThreadPoolExecutor(max_workers=MAX_SI_WORKERS) as pool:
        futs = [pool.submit(_worker, item) for item in alvos]
        for fut in cf.as_completed(futs):
            try:
                item, dados = fut.result()
                if dados:
                    _aplicar_si_no_ativo(item, dados)
                    ok += 1
            except Exception:
                continue

    print(f"[STATUS INVEST] Coletados {ok}/{len(alvos)} ativos (máx. {MAX_SI_ENRICH} chamadas).", flush=True)


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

    print("[2/5] Carregando fundamentos e Status Invest...", flush=True)
    fund = {}
    fund.update(carregar_fundamentos("https://www.fundamentus.com.br/resultado.php", "acao"))
    fund.update(carregar_fundamentos("https://www.fundamentus.com.br/fii_resultado.php", "fii"))

    for a in catalogo:
        a["setor"] = normalizar_setor(a.get("setor"))
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
            a.get("p") is not None
            and (a.get("dados_fundamentos") == "ok" or a.get("si_ok") or a["cat"] in {"etf", "bdr"})
        )

    gerado = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    doc = {
        "gerado": gerado,
        "gerado_ms": now_ms,
        "fonte": [
            "CotaLab (catálogo)",
            "Status Invest (fundamentos)",
            "Fundamentus (fundamentos)",
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
