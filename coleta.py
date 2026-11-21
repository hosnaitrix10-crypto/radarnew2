import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
import requests

CATALOGO_PATH = Path("catalogo_ativos.json")
DADOS_PATH = Path("dados.json")

TAMANHO_BLOCO = 10

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
]

BASE_B3_TICKERS = [
    "PETR4", "PETR3", "VALE3", "ITUB4", "ITUB3", "BBDC4", "BBDC3", "BBAS3",
    "WEGE3", "RENT3", "ABEV3", "SUZB3", "PRIO3", "BBSE3", "ELET3", "ELET6",
    "EQTL3", "RADL3", "SBSP3", "VIVT3", "TIMS3", "CMIG4", "CPLE6", "TAEE11",
    "B3SA3", "GGBR4", "GOAU4", "CSNA3", "USIM5", "CMIN3", "KLBN11", "UGPA3",
    "VBBR3", "RAIL3", "EMBR3", "HYPE3", "RDOR3", "HAPV3", "LREN3", "ASAI3",
    "MXRF11", "HGLG11", "XPML11", "KNCR11", "KNRI11", "XPLG11", "VISC11", "IRDM11",
    "CPTS11", "TGAR11", "VGHF11", "VGIR11", "BTLG11", "KNSC11", "TRXF11", "ALZR11",
    "NVDC34", "AAPL34", "MSFT34", "AMZO34", "GOOGL34", "META34", "TSLA34", "MELI34",
]


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def construir_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://statusinvest.com.br/",
        "Upgrade-Insecure-Requests": "1",
    }


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

    for seletor in [".top-info .info", ".info-area .info", ".indicators .item", ".card .info"]:
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

    return indicadores


def achar_num(data_obj: dict, termos: list[str]):
    for k, v in (data_obj or {}).items():
        kl = k.lower()
        if any(t in kl for t in termos):
            num = v.get("num") if isinstance(v, dict) else None
            if isinstance(num, (int, float)):
                return float(num)
    return None


def analisar_matematica(data_obj: dict, tipo_norm: str) -> dict:
    if not data_obj:
        return {
            "sinal": "Sem dados",
            "score": None,
            "pos_52s_pct": None,
            "preco_teto": None,
            "margem_teto_pct": None,
            "motivo": "Sem indicadores numéricos disponíveis.",
        }

    cot = achar_num(data_obj, ["valor atual", "cotação", "cotacao", "preço", "preco"])
    min52 = achar_num(data_obj, ["min. 52", "mín. 52", "min 52"])
    max52 = achar_num(data_obj, ["máx. 52", "max. 52", "max 52"])
    dy = achar_num(data_obj, ["dividend yield", "dy"])
    var12 = achar_num(data_obj, ["12m"])

    score = 0
    motivos = []

    pos_52s_pct = None
    if cot is not None and min52 is not None and max52 is not None and max52 > min52:
        pos = max(0.0, min(1.0, (cot - min52) / (max52 - min52)))
        pos_52s_pct = round(pos * 100.0, 1)
        if pos <= 0.30:
            score += 2
            motivos.append(f"próximo da mínima 52s ({pos_52s_pct}%)")
        elif pos <= 0.50:
            score += 1
            motivos.append(f"metade inferior da faixa 52s ({pos_52s_pct}%)")
        elif pos >= 0.85:
            score -= 2
            motivos.append(f"próximo da máxima 52s ({pos_52s_pct}%)")
        elif pos >= 0.70:
            score -= 1
            motivos.append(f"parte superior da faixa 52s ({pos_52s_pct}%)")

    piso_dy = 7.5 if tipo_norm == "fund" else 6.0
    preco_teto = None
    margem_teto_pct = None
    if cot is not None and cot > 0 and dy is not None and dy > 0:
        preco_teto = round(cot * (dy / piso_dy), 2)
        margem_teto_pct = round(((preco_teto - cot) / preco_teto) * 100.0, 1)
        if margem_teto_pct >= 15.0:
            score += 2
            motivos.append(f"abaixo do teto (+{margem_teto_pct:.1f}% até R$ {preco_teto:.2f})")
        elif margem_teto_pct >= 0.0:
            score += 1
            motivos.append(f"abaixo do teto (+{margem_teto_pct:.1f}% até R$ {preco_teto:.2f})")
        elif margem_teto_pct <= -25.0:
            score -= 2
            motivos.append(f"acima do teto ({margem_teto_pct:.1f}% vs R$ {preco_teto:.2f})")
        else:
            score -= 1
            motivos.append(f"acima do teto ({margem_teto_pct:.1f}% vs R$ {preco_teto:.2f})")
    elif dy is not None and dy == 0:
        score -= 1
        motivos.append("DY zerado")

    if var12 is not None:
        if var12 <= -25.0:
            score += 1
            motivos.append(f"queda 12m ({var12:+.1f}%)")
        elif var12 >= 50.0:
            score -= 1
            motivos.append(f"alta forte 12m ({var12:+.1f}%)")

    if score >= 2:
        sinal = "Comprar"
    elif score <= -3:
        sinal = "Vender"
    elif score <= -1:
        sinal = "Caro"
    else:
        sinal = "Aguardar"

    return {
        "sinal": sinal,
        "score": score,
        "pos_52s_pct": pos_52s_pct,
        "preco_teto": preco_teto,
        "margem_teto_pct": margem_teto_pct,
        "motivo": "; ".join(motivos) if motivos else "Indicadores neutros ou parciais.",
    }


def requisitar_com_backoff(session: requests.Session, url: str, tentativas: int = 2):
    for tentativa in range(1, tentativas + 1):
        try:
            resp = session.get(
                url,
                headers=construir_headers(),
                timeout=20,
                allow_redirects=True,
            )
            if resp.status_code == 429:
                time.sleep(random.uniform(12.0, 18.0) * tentativa)
                if tentativa < tentativas:
                    continue
                return "limit", None, "HTTP 429"
            if resp.status_code == 404:
                return "nodata", None, "HTTP 404"
            if resp.status_code in {403, 503} or resp.status_code >= 500:
                time.sleep(random.uniform(7.0, 11.0) * tentativa)
                if tentativa < tentativas:
                    continue
                return "neterr", None, f"HTTP {resp.status_code}"
            if resp.status_code != 200:
                return "neterr", None, f"HTTP {resp.status_code}"

            html = resp.text or ""
            if len(html) < 1000:
                return "nodata", None, f"HTML curto ({len(html)} bytes)"
            return "ok", html, None
        except requests.RequestException as exc:
            if tentativa < tentativas:
                time.sleep(random.uniform(5.0, 9.0) * tentativa)
                continue
            return "neterr", None, f"{type(exc).__name__}: {str(exc)[:120]}"

    return "neterr", None, "Limite de tentativas excedido"


def extrair_lista_catalogo(raw) -> list[tuple[str, str]]:
    saida = []
    vistos = set()

    def add(tk_raw, tipo_raw="stock"):
        tk = str(tk_raw or "").strip().upper()
        if not tk or tk in vistos or not re.match(r"^[A-Z0-9]{4,8}$", tk):
            return
        vistos.add(tk)
        saida.append((tk, normalizar_tipo(str(tipo_raw or "stock"), tk)))

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                add(item, "stock")
            elif isinstance(item, dict):
                add(
                    item.get("ticker") or item.get("code") or item.get("symbol") or item.get("ativo"),
                    item.get("type") or item.get("tipo") or item.get("category") or "stock",
                )
    elif isinstance(raw, dict):
        for chave in ["ativos", "items", "tickers", "catalog", "data", "stocks", "funds", "bdrs"]:
            sub = raw.get(chave)
            if isinstance(sub, list) and sub:
                return extrair_lista_catalogo(sub)
        for k, v in raw.items():
            if str(k).upper() in {"META", "STATS", "UPDATEDAT", "SOURCE"}:
                continue
            add(k, v.get("type") or v.get("tipo") if isinstance(v, dict) else "stock")

    return saida


def montar_catalogo() -> list[tuple[str, str]]:
    combinados = []
    vistos = set()

    def push_all(pares):
        for tk, tp in pares:
            t_up = str(tk or "").strip().upper()
            if not t_up or t_up in vistos or not re.match(r"^[A-Z0-9]{4,8}$", t_up):
                continue
            vistos.add(t_up)
            combinados.append((t_up, normalizar_tipo(tp, t_up)))

    if CATALOGO_PATH.exists():
        try:
            raw = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))
            push_all(extrair_lista_catalogo(raw))
        except Exception as exc:
            print(f"Aviso ao ler catalogo_ativos.json: {exc}")

    if not combinados:
        push_all([(tk, normalizar_tipo("", tk)) for tk in BASE_B3_TICKERS])

    return combinados


def carregar_dados() -> dict:
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
    raw_batch = (os.getenv("BATCH_SIZE") or "").strip().lower()
    if not raw_batch:
        batch_size = TAMANHO_BLOCO
    elif raw_batch in {"0", "all", "todos", "full"}:
        batch_size = 0
    else:
        batch_size = max(1, int(raw_batch))

    catalogo = montar_catalogo()
    base = carregar_dados()
    items = base["items"]

    def prioridade(par):
        ticker, _ = par
        atual = items.get(ticker)
        if not atual:
            return (0, "")
        st = atual.get("status")
        dt = atual.get("updatedAt") or ""
        if st == "ok":
            return (1, dt)
        if st == "nodata":
            return (2, dt)
        return (3, dt)

    ordenada = sorted(catalogo, key=prioridade)
    fila = ordenada if batch_size == 0 else ordenada[:batch_size]

    blocos = [fila[i : i + TAMANHO_BLOCO] for i in range(0, len(fila), TAMANHO_BLOCO)]
    print(f"Catalogo: {len(catalogo)} | Total na fila: {len(fila)} | Blocos de 10: {len(blocos)}")

    for num_bloco, bloco in enumerate(blocos, start=1):
        print(f"--- Iniciando bloco {num_bloco}/{len(blocos)} ({len(bloco)} ativos) ---")
        session = requests.Session()
        try:
            for ticker, tipo_norm in bloco:
                url = montar_url(ticker, tipo_norm)
                st_req, html, err = requisitar_com_backoff(session, url)

                if st_req == "ok" and html:
                    indicadores = extrair_dados_status_invest(html)
                    status_final = "ok" if indicadores else "nodata"
                    analise = (
                        analisar_matematica(indicadores, tipo_norm)
                        if status_final == "ok"
                        else analisar_matematica({}, tipo_norm)
                    )
                    items[ticker] = {
                        "ticker": ticker,
                        "type": tipo_norm,
                        "source": "statusinvest",
                        "status": status_final,
                        "sinal": analise["sinal"],
                        "analise": analise,
                        "url": url,
                        "updatedAt": agora_iso(),
                        "error": None if status_final == "ok" else "Sem indicadores no HTML",
                        "data": indicadores,
                    }
                else:
                    anterior = items.get(ticker) or {}
                    items[ticker] = {
                        "ticker": ticker,
                        "type": tipo_norm,
                        "source": "statusinvest",
                        "status": st_req,
                        "sinal": anterior.get("sinal") or "Sem dados",
                        "analise": anterior.get("analise") or analisar_matematica({}, tipo_norm),
                        "url": url,
                        "updatedAt": agora_iso(),
                        "error": err,
                        "data": anterior.get("data", {}),
                    }

                print(f"  {ticker}: {items[ticker]['status']} | {items[ticker]['sinal']}")
                time.sleep(random.uniform(2.5, 4.5))
        finally:
            session.close()

        salvar_dados(base, items)
        print(f"--- Fim do bloco {num_bloco}/{len(blocos)} | Resumo: {base['stats']} ---")

        if num_bloco < len(blocos):
            parada = random.uniform(20.0, 35.0)
            print(f"  [Parada entre blocos de 10] Aguardando {parada:.1f}s...")
            time.sleep(parada)


if __name__ == "__main__":
    main()
