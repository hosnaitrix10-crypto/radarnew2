#!/usr/bin/env python3
"""Coleta e reconcilia um snapshot multiclasse do mercado brasileiro.

O catálogo primário é o endpoint público paginado da brapi já usado pelo app;
CotaLab é contingência e nunca é tratado como cadastro oficial da B3. Cotações
só são aceitas no snapshot quando brapi e Yahoo Finance concordam dentro do
limite documentado. Fundamentus e Yahoo Finance permanecem fontes secundárias.
"""
from __future__ import annotations
import concurrent.futures as cf
import datetime as dt
import json, math, re, time
from io import StringIO
from pathlib import Path
import pandas as pd
import requests
import yfinance as yf
from cache_store import merge_snapshot

OUT=Path('dados.json'); CATALOGO=Path('catalogo_ativos.json')
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; RadarDeDividendos/2.0)'}
TIMEOUT=45; HISTORICO_ANOS=3; BATCH_COTACOES=50; MAX_PAGINAS=60
MAX_PRICE_DIVERGENCE=0.08
SESSION=requests.Session(); SESSION.headers.update(HEADERS)
CATALOG_INFO={'complete':False,'source':'unknown'}
PREVIOUS_ROWS={}
PREVIOUS_GENERATED=None
BR_TZ=dt.timezone(dt.timedelta(hours=-3))


def data_brt(value=None):
    if value is None: return dt.datetime.now(dt.timezone.utc).astimezone(BR_TZ).date().isoformat()
    try:
        if isinstance(value,str) and len(value)>=10 and value[4]=='-' and value[7]=='-':
            parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        else: return None
        if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(BR_TZ).date().isoformat()
    except (ValueError,TypeError): return None


def numero(v):
    if v is None: return None
    if isinstance(v,(int,float)) and not pd.isna(v):
        n=float(v); return n if math.isfinite(n) else None
    s=str(v).strip().replace('%','').replace('R$','').replace(' ','')
    if not s or s.lower() in {'nan','none','-','--','n/d'}: return None
    if ',' in s: s=s.replace('.','').replace(',','.')
    try:
        n=float(s); return n if math.isfinite(n) else None
    except (TypeError,ValueError): return None


def ticker_from(text):
    s=str(text).strip().upper()
    # O CotaLab concatena ticker e nome em uma única célula (ex.: AAPL34Apple,
    # A1AP34Advance, 03BK11Galapagos, AALR3Alianca). Aceita formatos comuns
    # da B3/BDRs/ETFs sem exigir que o ticker tenha apenas letras antes dos números.
    m=re.match(r'^([A-Z0-9]{2,4}\d{1,2})',s)
    if m:
        ticker=m.group(1)
        # Evita capturar prefixos arbitrários que não terminam em dígito.
        return ticker if any(ch.isdigit() for ch in ticker) else None
    return None


def parse_catalog_table(table):
    df=table.copy(); df.columns=[str(c).strip().lower() for c in df.columns]
    out=[]
    for _,row in df.iterrows():
        vals=[str(x).strip() for x in row.tolist() if x is not None and str(x).strip() not in {'','nan','None'}]
        if not vals: continue
        ticker=None; ticker_cell=''
        for v in vals:
            t=ticker_from(v)
            if t: ticker=t; ticker_cell=v; break
        if not ticker: continue
        joined=' | '.join(vals).lower()
        cat=None
        for needle,canon in [('acoes','acao'),('ações','acao'),('fiis','fii'),('etfs','etf'),('bdrs','bdr')]:
            if needle in joined: cat=canon; break
        if not cat: continue
        nome=''
        if ticker_cell.upper()!=ticker:
            nome=re.sub(r'^'+re.escape(ticker)+r'\s*','',ticker_cell,flags=re.I).strip(' -–|')
        if not nome:
            for v in vals:
                if v!=ticker_cell and not any(x in v.lower() for x in ['acoes','ações','fiis','etfs','bdrs']):
                    if not re.fullmatch(r'[A-Z]{2,4}',v.strip()): nome=v; break
        setor=vals[-1] if len(vals)>2 else ''
        if setor.lower() in {'acoes','ações','fiis','etfs','bdrs'}: setor=''
        out.append({'t':ticker,'nome':nome or ticker,'cat':cat,'setor':setor,'fonte_catalogo':'CotaLab'})
    return out


def carregar_catalogo_cotalab():
    """Fallback: descobre páginas do diretório secundário CotaLab.

    Alguns diretórios não informam "Página X de Y" no HTML. Nesse caso,
    tenta páginas sequenciais e encerra quando encontra página vazia, erro 404
    ou uma página repetida. Nunca substitui um catálogo maior por um menor.
    """
    catalogo=[]; vistos_paginas=set(); total_pag=None
    for pagina in range(1,MAX_PAGINAS+1):
        # Suporta os dois formatos comuns de URL; a primeira página é a URL canônica.
        urls = ['https://www.cotalab.com.br/ativos'] if pagina == 1 else [
            f'https://www.cotalab.com.br/ativos/pagina/{pagina}',
            f'https://www.cotalab.com.br/ativos?page={pagina}',
            f'https://www.cotalab.com.br/ativos?pagina={pagina}',
        ]
        r=None; ultimo_erro=None
        for url in urls:
            try:
                tentativa=SESSION.get(url,timeout=TIMEOUT,allow_redirects=True)
                if tentativa.status_code == 404:
                    continue
                tentativa.raise_for_status()
                # Se o endereço alternativo redirecionou para a mesma URL canônica,
                # não o considere uma página nova.
                r=tentativa
                if pagina == 1 or url == r.url or ('pagina' in r.url.lower()) or ('page=' in r.url.lower()):
                    break
            except Exception as e:
                ultimo_erro=e
        if r is None:
            if pagina > 1: break
            raise RuntimeError(f'Não foi possível acessar o catálogo CotaLab: {ultimo_erro}')
        texto=r.text
        m=re.search(r'(?:Página|Pagina)\s*\d+\s*de\s*(\d+)',texto,re.I)
        if m: total_pag=min(MAX_PAGINAS,int(m.group(1)))
        # Descobre paginação pelo próprio HTML, mesmo quando não há "Página X de Y".
        numeros=[]
        for padrao in [r'/ativos/pagina/(\d+)', r'[?&]page=(\d+)', r'[?&]pagina=(\d+)']:
            numeros.extend(int(x) for x in re.findall(padrao,texto,re.I))
        if numeros:
            total_pag=max([total_pag or 1]+numeros)
            total_pag=min(MAX_PAGINAS,total_pag)
        try: tabelas=pd.read_html(StringIO(texto))
        except ValueError: tabelas=[]
        candidatos=[]
        for tab in tabelas:
            cols=' '.join(str(c).lower() for c in tab.columns)
            if any(k in cols for k in ['ticker','categoria','nome']) or len(tab)>=10:
                achados=parse_catalog_table(tab)
                if len(achados)>=len(candidatos): candidatos=achados
        chaves=frozenset(a['t'] for a in candidatos)
        print(f'[CATÁLOGO] página {pagina}/{total_pag or "?"}: {len(candidatos)} ativos (HTTP {r.status_code}; {r.url})',flush=True)
        if not candidatos or not chaves:
            if pagina == 1: raise RuntimeError('Não foi possível interpretar a tabela de ativos do catálogo.')
            break
        if chaves in vistos_paginas:
            print('[CATÁLOGO] Página repetida; encerrando varredura de paginação.',flush=True)
            break
        vistos_paginas.add(chaves); catalogo.extend(candidatos)
        if total_pag is not None and pagina >= total_pag: break
        time.sleep(0.25)
    unicos={}
    for a in catalogo: unicos[a['t']]=a
    resultado=sorted(unicos.values(),key=lambda a:a['t'])
    if len(resultado)<100:
        if CATALOGO.exists():
            antigo=json.loads(CATALOGO.read_text(encoding='utf-8'))
            if len(antigo.get('ativos',[]))>len(resultado):
                print('[AVISO] Catálogo atual incompleto; preservando catálogo anterior.',flush=True)
                return antigo['ativos']
        raise RuntimeError(f'Catálogo incompleto ({len(resultado)} ativos após verificar paginação); não substituir dados existentes.')
    CATALOGO.write_text(json.dumps({'gerado':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),'fonte':'CotaLab — diretório público; validar contra cadastro oficial B3','total':len(resultado),'ativos':resultado},ensure_ascii=False,allow_nan=False),encoding='utf-8')
    return resultado



def _categoria_brapi(asset_type, subtype):
    if asset_type == 'stock': return 'acao'
    if asset_type == 'bdr': return 'bdr'
    if asset_type == 'fund':
        return {'fii':'fii','etf':'etf','fi-infra':'fi-infra','fi-agro':'fi-agro',
                'fip':'fip','fidc':'fidc'}.get(subtype, 'fundo')
    return 'outro'


def carregar_catalogo_brapi():
    """Obtém todas as páginas dos tipos públicos usados pela própria interface."""
    global CATALOG_INFO
    tipos=('stock','fund','bdr'); itens=[]; expected=0; page_stats={}
    for tipo in tipos:
        pagina=1; total_paginas=None; tipo_esperado=None; contagem=0
        while pagina <= MAX_PAGINAS:
            r=SESSION.get('https://brapi.dev/api/v2/tickers',
                          params={'type':tipo,'limit':2000,'page':pagina},timeout=TIMEOUT)
            r.raise_for_status(); j=r.json()
            rows=j.get('results'); pag=j.get('pagination') or {}
            if not isinstance(rows,list) or not isinstance(pag,dict):
                raise RuntimeError(f'brapi catálogo {tipo}: resposta sem results/pagination')
            if pagina==1:
                tipo_esperado=int(pag.get('totalItems') or len(rows))
                total_paginas=int(pag.get('totalPages') or 1); expected+=tipo_esperado
            if len(rows)==0 and tipo_esperado:
                raise RuntimeError(f'brapi catálogo {tipo}: página {pagina} vazia antes do fim')
            requested=str(j.get('requestedAt') or '')
            for x in rows:
                ticker=str(x.get('symbol') or '').strip().upper()
                if not re.fullmatch(r'[A-Z0-9.^_-]{1,24}',ticker): continue
                asset=str(x.get('assetType') or tipo).lower()
                subtype=str(x.get('subType') or '').lower()
                quote=x.get('quote') if isinstance(x.get('quote'),dict) else {}
                price=numero(quote.get('lastPrice'))
                entry={'t':ticker,'nome':x.get('longName') or x.get('name') or ticker,
                       'cat':_categoria_brapi(asset,subtype),'setor':x.get('sector') or '',
                       'fonte_catalogo':'brapi.dev v2 tickers','assetType':asset,
                       'subType':subtype,'catalogActive':x.get('isActive') is not False,
                       'logoUrl':x.get('logoUrl'),'exchange':x.get('exchange'),
                       'currency':x.get('currency'),'subsector':x.get('subsector'),
                       'requestedAt':requested,'catalogo_consultado_em':requested,
                       'quote_candidate_brapi':price,
                       'quote_change_brapi':numero(quote.get('changePercent')),
                       'quote_volume_brapi':numero(quote.get('volume')),
                       'quote_marketcap_brapi':numero(quote.get('marketCap'))}
                if price is not None and price>0:
                    entry['precos_fontes']={'brapi.dev':{'p':price,'em':requested}}
                itens.append(entry); contagem+=1
            has_next=pag.get('hasNextPage') is True or pagina<total_paginas
            if not has_next: break
            pagina+=1; time.sleep(0.8)
        if tipo_esperado is not None and contagem < tipo_esperado:
            raise RuntimeError(f'brapi catálogo {tipo}: recebidos {contagem} de {tipo_esperado} registros')
        page_stats[tipo]={'items':contagem,'pages':pagina,'expected':tipo_esperado or contagem}
        time.sleep(0.5)
    unique={}
    for a in itens:
        old=unique.get(a['t'])
        if old is None or (old.get('assetType')!='stock' and a.get('assetType')=='stock'):
            unique[a['t']]=a
    result=sorted(unique.values(),key=lambda a:a['t'])
    if len(result)<100 or len(result)<expected*0.95:
        raise RuntimeError(f'brapi catálogo incompleto: {len(result)} únicos para {expected} declarados')
    CATALOG_INFO={'complete':True,'source':'brapi.dev v2 tickers','expected':expected,
                  'count':len(result),'types':page_stats}
    private_quote_keys={'requestedAt','quote_candidate_brapi','quote_change_brapi',
                        'quote_volume_brapi','quote_marketcap_brapi','precos_fontes'}
    safe_rows=[{k:v for k,v in item.items() if k not in private_quote_keys} for item in result]
    payload={'gerado':dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
             'fonte':'brapi.dev v2 tickers','catalog_complete':True,'total':len(result),
             'catalog_info':CATALOG_INFO,'ativos':safe_rows}
    CATALOGO.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    for tipo,info in page_stats.items():
        print(f'[CATÁLOGO] brapi {tipo}: {info["items"]} ativos em {info["pages"]} página(s).',flush=True)
    return result


def carregar_catalogo():
    """Prefere o cache do catálogo completo do dia; CotaLab é só contingência."""
    global CATALOG_INFO
    if CATALOGO.exists():
        try:
            cached=json.loads(CATALOGO.read_text(encoding='utf-8'))
            if (cached.get('catalog_complete') is True and data_brt(cached.get('gerado'))==data_brt()
                    and data_brt(PREVIOUS_GENERATED)==data_brt() and len(cached.get('ativos',[]))>=100):
                CATALOG_INFO={'complete':True,'source':'brapi.dev v2 tickers (cache do dia)',
                              'expected':cached.get('total',len(cached['ativos'])),'count':len(cached['ativos']),
                              'types':cached.get('catalog_info',{}).get('types',{})}
                print(f'[CATÁLOGO] Reutilizando catálogo completo salvo hoje: {len(cached["ativos"])} tickers; sem repetir chamadas.',flush=True)
                return cached['ativos']
        except Exception as e:
            print(f'[AVISO] Cache local do catálogo ilegível; fazendo consulta completa: {e}',flush=True)
    try:
        return carregar_catalogo_brapi()
    except Exception as e:
        print(f'[AVISO] Catálogo brapi indisponível ou incompleto; tentando CotaLab: {e}',flush=True)
    result=carregar_catalogo_cotalab()
    CATALOG_INFO={'complete':False,'source':'CotaLab (contingência; completude não confirmada)',
                  'count':len(result),'types':{}}
    return result

def col_by(df,needles):
    for c in df.columns:
        s=str(c).lower()
        if any(n in s for n in needles): return c
    return None


def carregar_fundamentos(url,cat):
    """Lê tabelas públicas de fundamentos sem filtrar empresas por qualidade."""
    try:
        r=SESSION.get(url,timeout=TIMEOUT); r.raise_for_status()
        tabs=pd.read_html(StringIO(r.text),decimal=',',thousands='.')
        if not tabs: return {}
        df=max(tabs,key=lambda t:len(t))
        ct=col_by(df,['papel','ticker'])
        if ct is None:return {}
        mapping={
          'p': ['cota','cotação','cotacao'], 'pl':['p/l'], 'pvp':['p/vp'],
          'dy':['div. yield','div yield','dividend yield','div.'], 'ml':['mrg. l','margem líquida','margem liquida'],
          'roe':['roe'], 'liq':['liq.2','liquidez'], 'dbp':['dív/bruta','div/bruta','dívida/bruta','divida/bruta'],
          'cr':['cres. 5a','crescimento'], 'ffoy':['ffo yield'],
        }
        out={}
        for _,row in df.iterrows():
            t=str(row[ct]).strip().upper()
            if not re.fullmatch(r'[A-Z0-9]{4,6}',t): continue
            a={'t':t,'cat':cat}
            for k,needles in mapping.items():
                c=col_by(df,needles)
                if c is not None:a[k]=numero(row[c])
            if a.get('p') is not None and a['p']<=0:a['p']=None
            out[t]=a
        return out
    except Exception as e:
        print(f'[AVISO] Fundamentos {cat} indisponíveis: {e}',flush=True); return {}


def batch_precos(ativos):
    """Cruza o preço resumido brapi com o fechamento Yahoo quando ambos existem.

    Divergências acima de 8% são retidas como conflito; preço não validado não
    substitui o último valor confiável do cache.
    """
    pendentes=[]
    for a in ativos:
        a.setdefault('precos_fontes',{})
        old=PREVIOUS_ROWS.get(a['t'],{}); meta=(old.get('cache_meta') or {}).get('quote') or {}
        if old and data_brt(meta.get('last_attempt'))==data_brt():
            for key in ('p','ch','vol','data_cotacao','cotacao_em','preco_fonte','desempenho_1a','desempenho_3a','divergencia_preco_pct','precos_fontes'):
                if old.get(key) is not None: a[key]=old[key]
            if old.get('dados_cotacao')=='verificada' and meta.get('last_status')=='ok' and numero(old.get('p')) and numero(old.get('p'))>0:
                a['dados_cotacao']='verificada'
            else:
                a['p']=old.get('p') if numero(old.get('p')) and numero(old.get('p'))>0 else None
                a['dados_cotacao']=old.get('dados_cotacao') or meta.get('last_status') or 'indisponível'
                for key in ('preco_conflito','preco_candidato','erro_cotacao'):
                    if old.get(key) is not None: a[key]=old[key]
                a['erro_cotacao']=a.get('erro_cotacao') or 'já consultado hoje; nova tentativa no próximo ciclo'
            a['_skip_quote_refresh']=True
        else:
            bp=numero(a.get('quote_candidate_brapi'))
            if bp is not None and bp>0:
                a['precos_fontes']['brapi.dev']={'p':bp,'em':a.get('requestedAt')}
            pendentes.append(a)
    tickers=[a['t']+'.SA' for a in pendentes]
    for ini in range(0,len(tickers),BATCH_COTACOES):
        grupo=tickers[ini:ini+BATCH_COTACOES]
        try:
            hist=yf.download(tickers=grupo,period=f'{HISTORICO_ANOS}y',interval='1d',auto_adjust=False,group_by='ticker',threads=True,progress=False,timeout=TIMEOUT)
            if hist is None or hist.empty: hist=None
        except Exception as e:
            print(f'[AVISO] Lote de cotações {ini+1}-{min(ini+len(grupo),len(tickers))}: {e}',flush=True);hist=None
        for a,ticker in zip(pendentes[ini:ini+BATCH_COTACOES],grupo):
            one=None; close=pd.Series(dtype=float)
            try:
                if hist is not None:
                    if isinstance(hist.columns,pd.MultiIndex):
                        if ticker in hist.columns.get_level_values(0): one=hist[ticker]
                        elif ticker in hist.columns.get_level_values(1): one=hist.xs(ticker,axis=1,level=1)
                    else: one=hist
                    if one is not None: close=one['Close'].dropna() if 'Close' in one else pd.Series(dtype=float)
            except Exception: close=pd.Series(dtype=float)
            bp=numero(a.get('quote_candidate_brapi'))
            yp=float(close.iloc[-1]) if not close.empty and math.isfinite(float(close.iloc[-1])) and float(close.iloc[-1])>0 else None
            if yp is not None:
                yahoo_date=str(close.index[-1].date())
                a['precos_fontes']['Yahoo Finance']={'p':yp,'data':yahoo_date}
                first=float(close.iloc[0]) if len(close) else yp
                a['desempenho_3a']=round((yp/first-1)*100,2) if first>0 else None
                one_year=close[close.index>=pd.Timestamp.now(tz=None)-pd.DateOffset(years=1)]
                a['desempenho_1a']=round((yp/float(one_year.iloc[0])-1)*100,2) if len(one_year)>1 and float(one_year.iloc[0])>0 else None
            if bp is not None and bp>0 and yp is not None:
                gap=abs(bp/yp-1)
                if gap<=MAX_PRICE_DIVERGENCE:
                    a['p']=bp;a['ch']=numero(a.get('quote_change_brapi'));a['vol']=numero(a.get('quote_volume_brapi')) or 0
                    a['data_cotacao']=yahoo_date
                    a['cotacao_em']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
                    a['preco_fonte']='brapi.dev validada contra Yahoo Finance';a['dados_cotacao']='verificada'
                    a['divergencia_preco_pct']=round(gap*100,3)
                else:
                    a['p']=None;a['dados_cotacao']='conflito';a['preco_conflito']=True
                    a['preco_candidato']={'brapi.dev':bp,'Yahoo Finance':yp,'diferenca_pct':round(gap*100,3)}
                    a['erro_cotacao']='fontes divergem acima do limite; preço anterior preservado'
            elif bp is not None and bp>0:
                a['p']=None;a['dados_cotacao']='uma_fonte';a['preco_candidato']={'brapi.dev':bp}
                a['erro_cotacao']='sem cotação Yahoo para validação cruzada'
            elif yp is not None:
                a['p']=None;a['dados_cotacao']='uma_fonte';a['preco_candidato']={'Yahoo Finance':yp}
                a['erro_cotacao']='sem preço brapi para validação cruzada'
            else:
                a['p']=None;a['dados_cotacao']='indisponível';a['erro_cotacao']='sem preço válido nas fontes consultadas'
        if (ini//BATCH_COTACOES+1)%5==0: print(f'[COTAÇÕES] {min(ini+len(grupo),len(tickers))}/{len(tickers)} tickers processados',flush=True)
    for a in ativos: a.pop('_skip_quote_refresh',None)
    return ativos


def dividendos_ticker(a,agora):
    if a['cat'] not in {'acao','fii'}: return a
    old=PREVIOUS_ROWS.get(a['t'],{}); dm=(old.get('cache_meta') or {}).get('dividends') or {}
    if data_brt(dm.get('last_attempt'))==data_brt():
        for key in ('dpa','ap','div','dados_proventos','fonte_proventos','proventos_em'):
            if old.get(key) is not None: a[key]=old[key]
        a.pop('_skip_dividend_refresh',None)
        return a
    try:
        time.sleep(0.35)
        s=yf.Ticker(a['t']+'.SA').dividends
        if s is None or s.empty:
            a.setdefault('dpa',None);a.setdefault('ap',0);a.setdefault('div',[]);a['dados_proventos']='sem histórico';a['fonte_proventos']='Yahoo Finance';return a
        idx=pd.DatetimeIndex(s.index);idx=idx.tz_localize(None) if idx.tz is not None else idx;s.index=idx
        inicio=agora-pd.DateOffset(years=HISTORICO_ANOS); rec=s[s.index>=inicio]
        anos=len(set(int(y) for y in rec.index.year))
        a['ap']=min(HISTORICO_ANOS,anos)
        a['dpa']=round(float(rec.sum())/anos,4) if anos else None
        a['div']=[{'v':float(v),'d':d.strftime('%Y-%m-%d')} for d,v in s.items() if math.isfinite(float(v)) and d>=agora-pd.DateOffset(months=18)]
        a['dados_proventos']='ok' if anos and len(rec)>0 else 'sem histórico';a['fonte_proventos']='Yahoo Finance'
        a['proventos_em']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    except Exception as e:
        a.setdefault('dpa',None);a.setdefault('ap',0);a.setdefault('div',[]);a['dados_proventos']='indisponível';a['fonte_proventos']='Yahoo Finance';a['erro_proventos']=str(e)[:160]
    a.pop('_skip_dividend_refresh',None)
    return a


def main():
    global PREVIOUS_ROWS, PREVIOUS_GENERATED
    agora=pd.Timestamp.now().tz_localize(None)
    previous=None
    if OUT.exists():
        try: previous=json.loads(OUT.read_text(encoding='utf-8'))
        except Exception as e: raise RuntimeError(f'Cache anterior inválido; recusando sobrescrever dados.json: {e}')
    PREVIOUS_GENERATED=(previous or {}).get('gerado')
    PREVIOUS_ROWS={str(a.get('t','')).upper():a for a in (previous or {}).get('ativos',[]) if a.get('t')}
    print('[1/5] Descobrindo catálogo multiclasse...',flush=True)
    catalogo=carregar_catalogo()
    print(f'[CATÁLOGO] {len(catalogo)} instrumentos únicos.',flush=True)
    print('[2/5] Carregando fundamentos de ações e FIIs...',flush=True)
    fund={}
    fund.update(carregar_fundamentos('https://www.fundamentus.com.br/resultado.php','acao'))
    fund.update(carregar_fundamentos('https://www.fundamentus.com.br/fii_resultado.php','fii'))
    for a in catalogo:
        f=fund.get(a['t'],{})
        # Mantém a categoria do catálogo; fundamentos ausentes ficam explicitamente nulos.
        a.update({k:v for k,v in f.items() if k not in {'t','cat'}})
        a.setdefault('p',None);a.setdefault('pl',None);a.setdefault('pvp',None);a.setdefault('dy',None)
        a.setdefault('roe',None);a.setdefault('ml',None);a.setdefault('liq',None);a.setdefault('dpa',None);a.setdefault('ap',0);a.setdefault('div',[])
        a['dados_fundamentos']='ok' if f else 'indisponível'
        if f: a['fonte_fundamentos']='Fundamentus';a['fundamentos_em']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    print('[3/5] Obtendo histórico de preços em lotes de 50 tickers...',flush=True)
    batch_precos(catalogo)
    print('[4/5] Buscando proventos para ações e FIIs (pode demorar)...',flush=True)
    alvo=[a for a in catalogo if a['cat'] in {'acao','fii'}]
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        futs=[pool.submit(dividendos_ticker,a,agora) for a in alvo]
        for i,f in enumerate(cf.as_completed(futs),1):
            try:f.result()
            except Exception as e: print(f'[AVISO] Proventos: {e}',flush=True)
            if i%100==0:print(f'[PROVENTOS] {i}/{len(alvo)} ativos',flush=True)
    # Preserva campos e evita que a falta de fundamentos transforme um ativo em falso sinal de compra.
    for a in catalogo:
        a['dados_completos']=bool(a.get('p') is not None and (a.get('dados_fundamentos')=='ok' or a['cat'] in {'etf','bdr'}))
    gerado=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    doc={'gerado':gerado,'fonte':[CATALOG_INFO.get('source','catálogo desconhecido'),'Fundamentus (fundamentos quando disponíveis)','Yahoo Finance/yfinance (série histórica e proventos)'],'catalog_complete':CATALOG_INFO.get('complete') is True,'catalog_info':CATALOG_INFO,'historico_anos':HISTORICO_ANOS,'total':len(catalogo),'resumo_categorias':{c:sum(1 for a in catalogo if a['cat']==c) for c in sorted({a['cat'] for a in catalogo})},'ativos':catalogo}
    doc=merge_snapshot(doc,previous)
    temp=OUT.with_suffix('.json.tmp');temp.write_text(json.dumps(doc,ensure_ascii=False,allow_nan=False),encoding='utf-8');json.loads(temp.read_text(encoding='utf-8'));temp.replace(OUT)
    print(f'[5/5] Concluído: {doc["total"]} ativos atuais, {doc["total_historico"]} incluindo histórico; resumo {doc["resumo_categorias"]}.',flush=True)

if __name__=='__main__':
    try: main()
    except Exception as e: print(f'[ERRO] Coleta interrompida; dados.json anterior preservado: {e}',flush=True); raise
