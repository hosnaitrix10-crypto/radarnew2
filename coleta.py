import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
import requests

CATALOGO_PATH = Path("catalogo_ativos.json")
DADOS_PATH = Path("dados.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://statusinvest.com.br/",
    "Connection": "keep-alive",
}

# Lista ampla inicial da B3 (ações, units, FIIs/FIAGROs e BDRs) + descoberta dinâmica
BASE_B3_TICKERS = [
    # Ações e Units
    "RRRP3", "ALOS3", "ALPA4", "ABEV3", "ARZZ3", "ASAI3", "AURE3", "AZUL4", "AZZA3",
    "B3SA3", "BBSE3", "BBDC3", "BBDC4", "BRAP4", "BBAS3", "BRKM5", "BRFS3", "BPAC11",
    "CRFB3", "CCRO3", "CMPY3", "CMIG4", "CIEL3", "COGN3", "CPFE3", "CMIN3", "CSAN3",
    "CPLE6", "CSNA3", "CURY3", "CYRE3", "DXCO3", "DIRR3", "ECOR3", "ELET3", "ELET6",
    "EMBR3", "ENGI11", "ENEV3", "EGIE3", "EQTL3", "EZTC3", "FLRY3", "GGBR4", "GOAU4",
    "GOLL4", "GMAT3", "NTCO3", "SOMA3", "HAPV3", "HYPE3", "IGTI11", "INTELBRAS", "IRBR3",
    "ITSA4", "ITUB3", "ITUB4", "JBSS3", "JHSF3", "KLBN11", "RENT3", "LWSA3", "LREN3",
    "MGLU3", "POMO4", "MRFG3", "MRVE3", "MULT3", "PCAR3", "PETR3", "PETR4", "PRIO3",
    "PETZ3", "PSSA3", "RADL3", "RAIZ4", "RDOR3", "RECV3", "SANB11", "STBP3", "SMTO3",
    "CSMG3", "SLCE3", "SMFT3", "SUZB3", "TAEE11", "VIVT3", "TIMS3", "TOTS3", "TRPL4",
    "UGPA3", "USIM5", "VALE3", "VAMO3", "VBBR3", "VIVA3", "WEGE3", "YDUQ3", "ABCB4",
    "AGRO3", "ALLD3", "ALPK3", "AMAR3", "ANIM3", "ARML3", "AUAU3", "AVLL3", "BMGB4",
    "BMOB3", "BOAS3", "BPAN4", "BRSR6", "CAML3", "CBAV3", "CEAB3", "CGRA4", "CLSC4",
    "CSED3", "DESK3", "DEXP3", "DOTZ3", "ENJU3", "EVEN3", "FESA4", "FIQE3", "FRAS3",
    "GRND3", "GUAR3", "HBOR3", "HBRE3", "HBSA3", "HAGA4", "INTB3", "ISAE4", "JALL3",
    "JSLG3", "KEPL3", "LAND3", "LAVV3", "LEVE3", "LIGT3", "LOGG3", "LOGN3", "MATD3",
    "MDIA3", "MELK3", "MILS3", "MLAS3", "MOTV3", "MOVI3", "MTRE3", "MYPK3", "ODPV3",
    "ONCO3", "OPCT3", "ORVR3", "PASS3", "PGMN3", "PINE4", "PLPL3", "PMAM3", "PNVL3",
    "POSI3", "PRNR3", "PTBL3", "QUAL3", "RANI3", "RAPT4", "RCSL4", "REDE3", "RIAA3",
    "ROMI3", "SANB3", "SANB4", "SAPR11", "SAPR3", "SAPR4", "SAUD3", "SBFG3", "SCAR3",
    "SEER3", "SHUL4", "SIMH3", "SOJA3", "SYNE3", "TASA4", "TECN3", "TFCO4", "TGMA3",
    "TPIS3", "TRIS3", "TTEN3", "TUPY3", "UNIP6", "USIM3", "VITT3", "VLID3", "VTRU3",
    "VULC3", "WHRL4", "WIZC3", "ZAMP3",
    # FIIs / Fundos
    "MXRF11", "HGLG11", "XPML11", "KNCR11", "KNRI11", "XPLG11", "VISC11", "IRDM11",
    "BCFF11", "HGBS11", "CPTS11", "TGAR11", "VGHF11", "VGIR11", "HCTR11", "DEVA11",
    "RECR11", "MCCI11", "RBRF11", "RBRR11", "RBRY11", "BTLG11", "VILG11", "HSML11",
    "GGRC11", "LVBI11", "PVBI11", "BRCO11", "RECT11", "HFOF11", "JSRE11", "KNSC11",
    "KNHF11", "VRTA11", "CVBI11", "HGCR11", "XPCI11", "AFHI11", "GAME11", "HABT11",
    "HSLG11", "MALL11", "TRXF11", "ALZR11", "RZTR11", "KNIP11", "ICRI11", "BTAL11",
    # BDRs
    "NVDC34", "AAPL34", "MSFT34", "AMZO34", "GOOGL34", "META34", "TSLA34", "MELI34",
    "NFLX34", "DISB34", "KOEC34", "PEPB34", "JPMB34", "BOAC34", "WMTB34", "ROXO34",
    "BERK34", "PGCO34", "JNJN34", "XOMM34", "CVSH34", "ABTT34", "HDDR34", "MCDZ34",
]


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalizar_tipo(tipo_raw: str, ticker: str) -> str:
    t = (tipo_raw or "").strip().lower()
    tk = (ticker or "").strip().upper()

    if t in {"fund", "fii", "fiis", "fundo", "fundos"} or tk.endswith("11"):
        return "fund"
    if t in {"bdr", "bdrs"} or re.match(r"^[A-Z]{4}(31|32|33|34|35|39)$", tk):
        return "bdr"
    return "stock"


def montar_url(ticker: str, tipo_norm: str) -> str:
    slug = ticker.strip().lower()
    if tipo_norm == "fund":
        return f"https://statusinvest.com.br/fundos-imobiliarios/{slug}"
    if tipo_norm == "bdr":
        return f"https://statusinvest.com.br/bdrs/{slug}"
    return f"https://statusinvest.com.br/acoes/{slug}"


def parse_numero_br(texto: str):
    if not texto:
        return None
    limpo = re.sub(r"[R$%\s\xa0]", "", texto).strip()
    if not limpo or limpo in {"-", "--", "N/A", "nan", "None"}:
        return None
    limpo = limpo.replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return texto.strip()


def limpar_chave(texto: str) -> str:
    chave = re.sub(r"\s+", " ", (texto or "")).strip()
    chave = re.sub(r"\s*help_outline$", "", chave, flags=re.IGNORECASE).strip()
    return chave


def extrair_dados_status_invest(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    indicadores = {}

    seletores_bloco = [
        ".top-info .info",
        ".info-area .info",
        ".indicators .item",
        ".card .info",
    ]

    for seletor in seletores_bloco:
        for bloco in soup.select(seletor):
            titulo_el = bloco.select_one(".title, .name, h3, h2, span.d-block")
            valor_el = bloco.select_one(".value, strong, .number")
            if not titulo_el or not valor_el:
                continue

            chave = limpar_chave(titulo_el.get_text(" ", strip=True))
            valor_bruto = valor_el.get_text(" ", strip=True)
            if chave and valor_bruto and chave not in indicadores:
                indicadores[chave] = {
                    "raw": valor_bruto,
                    "num": parse_numero_br(valor_bruto),
                }

    if not indicadores:
        h1 = soup.select_one("h1")
        if h1:
            txt_h1 = limpar_chave(h1.get_text(" ", strip=True))
            if txt_h1:
                indicadores["Ativo"] = {"raw": txt_h1, "num": None}

    return indicadores


def requisitar_com_retry(session: requests.Session, url: str, tentativas: int = 2):
    for tentativa in range(1, tentativas + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
            if resp.status_code == 429:
                if tentativa < tentativas:
                    time.sleep(2.0 * tentativa)
                    continue
                return "limit", None, "HTTP 429"
            if resp.status_code == 404:
                return "nodata", None, "HTTP 404"
            if resp.status_code != 200:
                return "neterr", None, f"HTTP {resp.status_code}"

            html = resp.text or ""
            if len(html) < 800:
                return "nodata", None, f"HTML curto ({len(html)} bytes)"

            return "ok", html, None
        except requests.RequestException as exc:
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            return "neterr", None, f"{type(exc).__name__}: {str(exc)[:140]}"

    return "neterr", None, "Falha desconhecida"


def descobrir_tickers_b3(session: requests.Session) -> list[tuple[str, str]]:
    """Tenta descobrir tickers adicionais da B3 via endpoints/páginas públicas do Status Invest."""
    descobertos = []
    endpoints = [
        ("https://statusinvest.com.br/category/advancedsearchresult?CategoryType=1&Search=%7B%7D", "stock"),
        ("https://statusinvest.com.br/category/advancedsearchresult?CategoryType=2&Search=%7B%7D", "fund"),
        ("https://statusinvest.com.br/category/advancedsearchresult?CategoryType=4&Search=%7B%7D", "bdr"),
    ]

    for url, tipo_padrao in endpoints:
        try:
            resp = session.get(
                url,
                headers={**HEADERS, "Accept": "application/json, text/plain, */*"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            lista = data.get("list") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in lista:
                if not isinstance(item, dict):
                    continue
                tk = str(item.get("ticker") or item.get("code") or item.get("symbol") or "").strip().upper()
                if re.match(r"^[A-Z]{4}\d{1,2}$", tk):
                    descobertos.append((tk, normalizar_tipo(tipo_padrao, tk)))
        except Exception:
            continue

    return descobertos


def extrair_lista_catalogo(raw) -> list[tuple[str, str]]:
    saida = []
    vistos = set()

    def adicionar(tk_raw, tipo_raw="stock"):
        tk = str(tk_raw or "").strip().upper()
        if not tk or tk in vistos or not re.match(r"^[A-Z0-9]{4,8}$", tk):
            return
        vistos.add(tk)
        saida.append((tk, normalizar_tipo(str(tipo_raw or "stock"), tk)))

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                adicionar(item, "stock")
            elif isinstance(item, dict):
                tk = item.get("ticker") or item.get("code") or item.get("symbol") or item.get("ativo")
                tp = item.get("type") or item.get("tipo") or item.get("category") or "stock"
                adicionar(tk, tp)
    elif isinstance(raw, dict):
        for chave_lista in ["ativos", "items", "tickers", "catalog", "data", "stocks", "funds", "bdrs"]:
            sub = raw.get(chave_lista)
            if isinstance(sub, list) and sub:
                return extrair_lista_catalogo(sub)

        for k, v in raw.items():
            if str(k).upper() in {"META", "STATS", "UPDATEDAT", "SOURCE"}:
                continue
            if isinstance(v, dict):
                adicionar(k, v.get("type") or v.get("tipo") or "stock")
            else:
                adicionar(k, "stock")

    return saida


def montar_catalogo_completo(session: requests.Session) -> list[tuple[str, str]]:
    combinados = []
    vistos = set()

    def add_lista(pares):
        for tk, tp in pares:
            t_up = str(tk or "").strip().upper()
            if not t_up or t_up in vistos or not re.match(r"^[A-Z0-9]{4,8}$", t_up):
                continue
            vistos.add(t_up)
            combinados.append((t_up, normalizar_tipo(tp, t_up)))

    if CATALOGO_PATH.exists():
        try:
            raw = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))
            add_lista(extrair_lista_catalogo(raw))
        except Exception as exc:
            print(f"Aviso ao ler catalogo_ativos.json: {exc}")

    # Acrescenta lista base B3 e descoberta dinâmica para garantir cobertura ampla
    add_lista([(tk, normalizar_tipo("", tk)) for tk in BASE_B3_TICKERS])
    add_lista(descobrir_tickers_b3(session))

    return combinados


def carregar_dados_atuais() -> dict:
    padrao = {"source": "statusinvest", "updatedAt": None, "stats": {}, "items": {}}
    if not DADOS_PATH.exists():
        return padrao
    try:
        data = json.loads(DADOS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["source"] = "statusinvest"
            if not isinstance(data.get("items"), dict):
                data["items"] = {}
            return data
    except Exception:
        pass
    return padrao


def calcular_stats(items: dict) -> dict:
    contagem = {"total": len(items), "ok": 0, "nodata": 0, "neterr": 0, "limit": 0}
    for v in items.values():
        st = (v or {}).get("status")
        if st in contagem:
            contagem[st] += 1
    return contagem


def salvar_dados(base: dict, items: dict):
    base["source"] = "statusinvest"
    base["updatedAt"] = agora_iso()
    base["stats"] = calcular_stats(items)
    DADOS_PATH.write_text(
        json.dumps(base, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main():
    batch_env = (os.getenv("BATCH_SIZE") or "400").strip().lower()
    batch_size = 0 if batch_env in {"0", "all", "todos", "full"} else max(1, int(batch_env))

    base = carregar_dados_atuais()
    items = base["items"]

    session = requests.Session()
    try:
        catalogo = montar_catalogo_completo(session)

        # Prioriza não coletados; depois erros de rede/limite; depois os mais antigos
        def prioridade(par):
            ticker, _ = par
            atual = items.get(ticker)
            if not atual:
                return (0, "")
            if atual.get("status") in {"neterr", "limit"}:
                return (1, atual.get("updatedAt") or "")
            return (2, atual.get("updatedAt") or "")

        ordenada = sorted(catalogo, key=prioridade)
        fila = ordenada if batch_size == 0 else ordenada[:batch_size]

        print(f"Catalogo B3 total: {len(catalogo)} | Processando neste lote: {len(fila)}")

        for idx, (ticker, tipo_norm) in enumerate(fila, start=1):
            url = montar_url(ticker, tipo_norm)
            st_req, html, err = requisitar_com_retry(session, url)

            if st_req == "ok" and html:
                indicadores = extrair_dados_status_invest(html)
                status_final = "ok" if indicadores else "nodata"
                items[ticker] = {
                    "ticker": ticker,
                    "type": tipo_norm,
                    "source": "statusinvest",
                    "status": status_final,
                    "url": url,
                    "updatedAt": agora_iso(),
                    "error": None if status_final == "ok" else "Sem indicadores no HTML",
                    "data": indicadores,
                }
            else:
                items[ticker] = {
                    "ticker": ticker,
                    "type": tipo_norm,
                    "source": "statusinvest",
                    "status": st_req,
                    "url": url,
                    "updatedAt": agora_iso(),
                    "error": err,
                    "data": (items.get(ticker) or {}).get("data", {}),
                }

            if idx % 25 == 0 or idx == len(fila):
                salvar_dados(base, items)
                print(f"  Progresso: {idx}/{len(fila)} -> {ticker} ({items[ticker]['status']})")

            time.sleep(0.55)
    finally:
        session.close()

    salvar_dados(base, items)
    print("Resumo final:", base["stats"])


if __name__ == "__main__":
    main()
