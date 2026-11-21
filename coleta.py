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
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://statusinvest.com.br/",
    "Connection": "keep-alive",
}


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

    # Blocos principais de cotação / DY / P/VP / indicadores do Status Invest
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
            if not chave or not valor_bruto:
                continue

            if chave not in indicadores:
                indicadores[chave] = {
                    "raw": valor_bruto,
                    "num": parse_numero_br(valor_bruto),
                }

    # Fallback para pares título/valor estruturados com atributo title
    for el in soup.select("[title]"):
        titulo = limpar_chave(el.get("title", ""))
        if not titulo or len(titulo) > 80:
            continue
        prox = el.find_next_sibling(["strong", "b", "span"])
        if prox:
            val_txt = prox.get_text(" ", strip=True)
            if val_txt and titulo not in indicadores:
                indicadores[titulo] = {
                    "raw": val_txt,
                    "num": parse_numero_br(val_txt),
                }

    return indicadores


def requisitar_com_retry(session: requests.Session, url: str, tentativas: int = 3):
    for tentativa in range(1, tentativas + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 429:
                if tentativa < tentativas:
                    time.sleep(2.5 * tentativa)
                    continue
                return "limit", None, "HTTP 429"
            if resp.status_code == 404:
                return "nodata", None, "HTTP 404"
            if resp.status_code >= 500:
                if tentativa < tentativas:
                    time.sleep(1.5 * tentativa)
                    continue
                return "neterr", None, f"HTTP {resp.status_code}"
            if resp.status_code != 200:
                return "neterr", None, f"HTTP {resp.status_code}"

            html = resp.text or ""
            if len(html) < 1200 or "status invest" not in html.lower():
                return "nodata", None, "Conteudo vazio ou invalido"

            return "ok", html, None
        except requests.RequestException as exc:
            if tentativa < tentativas:
                time.sleep(1.5 * tentativa)
                continue
            return "neterr", None, str(exc)

    return "neterr", None, "Falha desconhecida"


def carregar_catalogo() -> list[tuple[str, str]]:
    if not CATALOGO_PATH.exists():
        return []
    raw = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))
    lista = raw if isinstance(raw, list) else raw.get("ativos") or raw.get("items") or []

    saida = []
    vistos = set()
    for item in lista:
        if isinstance(item, str):
            ticker, tipo = item.strip().upper(), "stock"
        elif isinstance(item, dict):
            ticker = str(item.get("ticker") or item.get("code") or item.get("symbol") or "").strip().upper()
            tipo = str(item.get("type") or item.get("tipo") or item.get("category") or "stock").strip()
        else:
            continue

        if not ticker or ticker in vistos:
            continue
        vistos.add(ticker)
        saida.append((ticker, normalizar_tipo(tipo, ticker)))
    return saida


def carregar_dados_atuais() -> dict:
    if not DADOS_PATH.exists():
        return {
            "source": "statusinvest",
            "updatedAt": None,
            "stats": {},
            "items": {},
        }
    try:
        data = json.loads(DADOS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON raiz invalido")
        data["source"] = "statusinvest"
        if not isinstance(data.get("items"), dict):
            data["items"] = {}
        return data
    except Exception:
        return {
            "source": "statusinvest",
            "updatedAt": None,
            "stats": {},
            "items": {},
        }


def calcular_stats(items: dict) -> dict:
    contagem = {"total": len(items), "ok": 0, "nodata": 0, "neterr": 0, "limit": 0}
    for v in items.values():
        st = (v or {}).get("status")
        if st in contagem:
            contagem[st] += 1
    return contagem


def main():
    batch_size = int(os.getenv("BATCH_SIZE", "120"))
    catalogo = carregar_catalogo()
    base = carregar_dados_atuais()
    items = base["items"]

    # Prioriza ativos ainda não coletados ou com erro de rede anterior; depois os mais antigos
    def prioridade(par):
        ticker, _ = par
        atual = items.get(ticker)
        if not atual:
            return (0, "")
        if atual.get("status") in {"neterr", "limit"}:
            return (1, atual.get("updatedAt") or "")
        return (2, atual.get("updatedAt") or "")

    fila = sorted(catalogo, key=prioridade)[:batch_size]

    session = requests.Session()
    try:
        for ticker, tipo_norm in fila:
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
                    "error": None if status_final == "ok" else "Sem indicadores encontrados no HTML",
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

            time.sleep(0.9)
    finally:
        session.close()

    base["source"] = "statusinvest"
    base["updatedAt"] = agora_iso()
    base["stats"] = calcular_stats(items)

    DADOS_PATH.write_text(
        json.dumps(base, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
