# -*- coding: utf-8 -*-
"""Monitor de publicacoes juridicas - LexFlow.

FONTES (em ordem de prioridade):
  1. DataJud CNJ API (https://api-publica.datajud.cnj.jus.br) - publica, REST, sem login, sem Selenium
  2. Selenium headless no Comunica PJE 1G (https://comunica.pje.jus.br/consulta)
  3. Selenium no eProc/Projudi/e-SAJ (via OAuth ou consulta publica)

Cada scraper retorna um dict consistente:
  {"pubs": [pub...], "url": "...", "error": "...", "tribunal": "...", "case_info": {...}}

Cada pub tem:
  {"cnj": "...", "date": "DD/MM/YYYY", "type": "Intimacao|Citacao|...",
   "title": "...", "description": "...", "raw": "...",
   "classe": "...", "assunto": "...", "orgao": "...", "magistrado": "...",
   "valor_causa": "...", "partes": [...]}
"""

import os, re, json, time, sqlite3, threading
from datetime import datetime
import urllib.request, urllib.error, urllib.parse

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "lexflow.db"))

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_OK = True
except Exception:
    SELENIUM_OK = False


# ===================== HELPERS =====================

def normalize_cnj(raw):
    if not raw:
        return ("", "")
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) != 20:
        return (str(raw).strip(), digits)
    formatted = f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    return (formatted, digits)


def cnj_to_tribunal(cnj_digits):
    if not cnj_digits or len(cnj_digits) < 16:
        return ""
    uf_map = {"01":"AC","02":"AL","03":"AP","04":"AM","05":"BA","06":"CE","07":"DF","08":"ES",
              "09":"GO","10":"MA","11":"MT","12":"MS","13":"MG","14":"PA","15":"PB","16":"PR",
              "17":"PE","18":"PI","19":"RJ","20":"RN","21":"RS","22":"RO","23":"RR","24":"SC",
              "25":"SP","26":"SE","27":"TO"}
    seg = cnj_digits[13:14]
    if seg == "8":
        uf = uf_map.get(cnj_digits[14:16], "")
        return "TJ" + uf if uf else ""
    if seg == "4":
        n = cnj_digits[14:15]
        return f"TRF{n}" if n.isdigit() else ""
    if seg == "5":
        n = cnj_digits[14:15]
        return f"TRT{n}" if n.isdigit() else ""
    if seg == "1":
        return "STF"
    if seg == "2":
        return "STJ"
    if seg == "9":
        return "CSJT" if cnj_digits[14:15] == "0" else ""
    return ""


def uf_to_tribunal(uf):
    return "TJ" + (uf or "").upper() if uf else ""


def _parse_oab(raw):
    if not raw:
        return {"numero": "", "uf": ""}
    s = str(raw).strip()
    m = re.search(r"/\s*([A-Z]{2})\b", s) or re.search(r"\b([A-Z]{2})\s*(\d)", s)
    uf = m.group(1) if m else ""
    n = re.search(r"(\d{1,3}(?:\.\d{3}){1,2}|\d{4,6})", s)
    numero = n.group(1).replace(".", "") if n else ""
    return {"numero": numero, "uf": uf}


# ===================== DATAJUD CNJ (PRIMARIO) =====================

# API publica: https://api-publica.datajud.cnj.jus.br
# Endpoint: POST /api_publica_{tribunal}/_search  (com query DSL Elasticsearch)
# Headers:  Content-Type: application/json
#           Authorization: APIKey xxx (NAO OBRIGATORIO para uso publico limitado, mas recomendado)
# Body: {"query": {"match": {"numeroProcesso": "NNNNNNN-XX.YYYY.J.TR.OOOO"}}, "size": 50}
DATAJUD_BASE = "https://api-publica.datajud.cnj.jus.br"
# API key publica conhecida - compartilhada pela comunidade (uso livre para nao-PJe)
DATAJUD_API_KEY = os.environ.get("DATAJUD_API_KEY", "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==")

# Aliases de sigla -> codigo do DataJud
_TRIBUNAL_CODES = {
    "TJRJ": "tjrj", "TJSP": "tjsp", "TJMG": "tjmg", "TJRS": "tjrs", "TJPR": "tjpr",
    "TJBA": "tjba", "TJPE": "tjpe", "TJCE": "tjce", "TJGO": "tjgo", "TJDF": "tjdft",
    "TJSC": "tjsc", "TJMS": "tjms", "TJMT": "tjmt", "TJPA": "tjpa", "TJES": "tjes",
    "TJMA": "tjma", "TJPB": "tjpb", "TJRN": "tjrn", "TJAL": "tjal", "TJPI": "tjpi",
    "TJSE": "tjse", "TJAC": "tjac", "TJAP": "tjap", "TJAM": "tjam", "TJRO": "tjro",
    "TJRR": "tjrr", "TJTO": "tjto",
    "TRF1": "trf1", "TRF2": "trf2", "TRF3": "trf3", "TRF4": "trf4", "TRF5": "trf5", "TRF6": "trf6",
    "TRT1": "trt1", "TRT2": "trt2", "TRT3": "trt3", "TRT4": "trt4", "TRT5": "trt5",
    "TRT6": "trt6", "TRT7": "trt7", "TRT8": "trt8", "TRT9": "trt9", "TRT10": "trt10",
    "TRT11": "trt11", "TRT12": "trt12", "TRT13": "trt13", "TRT14": "trt14", "TRT15": "trt15",
    "TRT16": "trt16", "TRT17": "trt17", "TRT18": "trt18", "TRT19": "trt19", "TRT20": "trt20",
    "TRT21": "trt21", "TRT22": "trt22", "TRT23": "trt23", "TRT24": "trt24",
    "STJ": "stj", "STF": "stf", "TST": "tst", "CSJT": "csjt",
}


def _datajud_request(tribunal, body, timeout=20):
    """Faz POST na API DataJud. Retorna dict ou None."""
    code = _TRIBUNAL_CODES.get((tribunal or "").upper())
    if not code:
        return None
    url = f"{DATAJUD_BASE}/api_publica_{code}/_search"
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"APIKey {DATAJUD_API_KEY}",
            "User-Agent": "LexFlow/1.0 (https://github.com/zBasck/lexflow)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 404 = sem processos indexados, 401/403 = API key invalida, 400 = CNJ malformado
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            body = ""
        return {"_error": f"HTTP {e.code}", "_body": body, "_tribunal": tribunal}
    except Exception as e:
        return {"_error": str(e)[:200], "_tribunal": tribunal}


def _datajud_get_movs(cnj_digits, tribunal):
    """Busca movimentacoes de um CNJ na API DataJud. Retorna lista de pubs normalizadas."""
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    body = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"numeroProcesso": cnj_fmt}}
                ]
            }
        },
        "size": 1,
    }
    res = _datajud_request(tribunal, body, timeout=15)
    if not res or res.get("_error"):
        return [], res or {}
    hits = res.get("hits", {}).get("hits", [])
    if not hits:
        return [], res
    src = hits[0].get("_source", {})
    movs = src.get("movimentos", []) or []
    classe = (src.get("classe") or {}).get("nome", "") if isinstance(src.get("classe"), dict) else str(src.get("classe", ""))
    orgao = (src.get("orgaoJulgador") or {}).get("nome", "") if isinstance(src.get("orgaoJulgador"), dict) else str(src.get("orgaoJulgador", ""))
    assunto = ""
    if src.get("assuntos"):
        a = src["assuntos"][0]
        assunto = a.get("nome", "") if isinstance(a, dict) else str(a)
    valor = src.get("valorCausa", 0) or 0
    magistr = src.get("magistrado", "")

    pubs = []
    for m in movs:
        date_iso = m.get("dataHora", "")[:10] if m.get("dataHora") else ""
        if not date_iso:
            continue
        try:
            d, mo, y = date_iso.split("-")
            date_br = f"{d}/{mo}/{y}"
        except Exception:
            date_br = date_iso
        tipo = _guess_tipo(m)
        complemento = m.get("complementosTabelados", []) or []
        desc_parts = []
        for c in complemento:
            if isinstance(c, dict):
                desc_parts.append(c.get("nome", c.get("descricao", "")))
            else:
                desc_parts.append(str(c))
        desc = m.get("descricao", "") or " ".join(str(x) for x in desc_parts)
        desc = (desc or "").strip()[:1000]
        if not desc:
            continue
        pubs.append({
            "cnj": cnj_fmt,
            "date": date_br,
            "type": tipo,
            "title": f"{tipo} - {cnj_fmt}" + (f" ({date_br})" if date_br else ""),
            "description": desc,
            "raw": desc,
            "classe": classe,
            "assunto": assunto,
            "orgao": orgao,
            "magistrado": magistr,
            "valor_causa": str(valor) if valor else "",
            "partes": src.get("partes", []),
        })
    return pubs, res


def _datajud_search_oab(numero_oab, uf):
    """Busca processos em que uma OAB aparece. Retorna (pubs, tribunal) por tribunal."""
    # O DataJud nao indexa OAB diretamente nas partes em todos os tribunais, mas
    # as publicacoes sao acessiveis via DJe diario oficial (que NAO esta no DataJud)
    # O que conseguimos: lista de processos por OAB em sistemas que indexam.
    # Para o Comunica PJE, o Selenium ainda e o caminho.
    return [], {"_note": "DataJud nao indexa OAB diretamente; use Selenium Comunica PJE"}


def _guess_tipo(mov):
    """Identifica o tipo da movimentacao baseado em palavras-chave."""
    desc = (mov.get("descricao") or "").lower()
    codigo = str(mov.get("codigo", ""))
    # Codigos nacionais CNJ (tabela unica): 5=intimacao, 10=citacao, 11=sentenca, 51=audiencia
    if codigo in ("5", "105", "106", "112", "246", "247") or "intima" in desc:
        return "Intimacao"
    if codigo in ("10", "111", "236", "237") or "cita" in desc:
        return "Citacao"
    if codigo in ("11", "113", "114", "220", "221") or "senten" in desc:
        return "Sentenca"
    if "despacho" in desc or codigo in ("60", "60", "60"):
        return "Despacho"
    if "audien" in desc or "audiencia" in desc or codigo in ("51", "52"):
        return "Audiencia"
    if "edital" in desc or codigo in ("85", "86"):
        return "Edital"
    if "ato ordinat" in desc:
        return "Ato Ordinatorio"
    if "decis" in desc:
        return "Decisao"
    if "conclus" in desc:
        return "Conclusos"
    if "julg" in desc:
        return "Julgamento"
    if "distribu" in desc or "redistribu" in desc:
        return "Distribuicao"
    if "recurso" in desc or "agravo" in desc or "apel" in desc:
        return "Recurso"
    return "Movimentacao"


# ===================== SELENIUM (SECUNDARIO) =====================

_DRIVER = None
_DRIVER_LOCK = threading.Lock()


def get_driver():
    if not SELENIUM_OK:
        raise RuntimeError("selenium nao instalado")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _get_driver_singleton():
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is None:
            _DRIVER = get_driver()
        return _DRIVER


def _close_driver():
    global _DRIVER
    with _DRIVER_LOCK:
        if _DRIVER is not None:
            try: _DRIVER.quit()
            except Exception: pass
            _DRIVER = None


def _extract_pub_from_row(txt, source_label, cnj_hint=""):
    txt = (txt or "").strip()
    if not txt or len(txt) < 5:
        return None
    cnj_m = re.search(r"\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}", txt)
    cnj = cnj_m.group(0) if cnj_m else cnj_hint
    date_m = re.search(r"\d{2}/\d{2}/\d{4}", txt)
    date = date_m.group(0) if date_m else ""
    tipo = "Publicacao"
    for k in ("Intimacao","Citacao","Sentenca","Despacho","Audiencia","Ato Ordinatorio","Edital","Decisao","Julgamento","Conclusos"):
        if k.lower() in txt.lower():
            tipo = k
            break
    return {
        "cnj": cnj,
        "date": date,
        "type": tipo,
        "title": f"{tipo} - {cnj}" if cnj else f"{tipo} - {source_label}",
        "description": txt[:500],
        "raw": txt,
    }


def _scraper_selenium_pje(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return {"pubs": [], "url": "", "error": "", "tribunal": ""}
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://comunica.pje.jus.br/consulta?numeroProcesso={cnj_digits}"
    pubs, err = [], ""
    try:
        driver = _get_driver_singleton()
        # v4.3.0: timeout curto (12s) - se o Comunica PJE demorar mais, sai rapido
        # e o frontend mostra erro. Antes era 45s, pendurava o request HTTP.
        PAGE_TIMEOUT = 12
        driver.set_page_load_timeout(PAGE_TIMEOUT)
        try:
            driver.get(url)
        except Exception as e:
            return {"pubs": [], "url": url, "error": f"timeout carregando Comunica PJE ({PAGE_TIMEOUT}s)", "tribunal": cnj_to_tribunal(cnj_digits)}
        try:
            WebDriverWait(driver, PAGE_TIMEOUT).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "table tbody tr, [class*='item'], [class*='publicacao'], [class*='resultado'], [class*='movimentacao'], mat-card, app-publicacao, .ng-star-inserted"
                ))
            )
            time.sleep(2.0)
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr, [class*='item'], [class*='publicacao'], [class*='resultado'], [class*='movimentacao'], mat-card, app-publicacao, .publicacao-item, .ng-star-inserted")
            for row in rows:
                p = _extract_pub_from_row(row.text, "PJE", cnj_hint=cnj_fmt)
                if p: pubs.append(p)
            # Sem body scan: CNJ aleatorio no DOM nao e publicacao real
            if not pubs:
                err = "Selenium Comunica PJE: 0 publicacoes encontradas (sem body scan fallback)"
        except Exception as e:
            err = f"timeout Selenium: {str(e)[:120]}"
    except Exception as e:
        err = str(e)[:200]
    return {"pubs": pubs, "url": url, "error": err, "tribunal": cnj_to_tribunal(cnj_digits)}


def _scraper_selenium_eproc(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return {"pubs": [], "url": "", "error": "", "tribunal": ""}
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://eproc1g.tjrj.jus.br/eproc/externo_controlador.php?acao=consulta_publica&num_processo={cnj_fmt}"
    pubs, err = [], ""
    try:
        driver = _get_driver_singleton()
        driver.set_page_load_timeout(15)
        driver.get(url)
        time.sleep(2.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "table.tabelaMovimentacoes tbody tr, table tbody tr, .evento, [class*='movimentacao']")
        for row in rows:
            p = _extract_pub_from_row(row.text, "eProc", cnj_hint=cnj_fmt)
            if p: pubs.append(p)
    except Exception as e:
        err = str(e)[:200]
    return {"pubs": pubs, "url": url, "error": err, "tribunal": cnj_to_tribunal(cnj_digits)}


def _scraper_selenium_projudi(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return {"pubs": [], "url": "", "error": "", "tribunal": ""}
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://projudi.tjrj.jus.br/projudi/consultaPublica.do?actionType=consulta&numero={cnj_fmt}"
    pubs, err = [], ""
    try:
        driver = _get_driver_singleton()
        driver.set_page_load_timeout(15)
        driver.get(url)
        time.sleep(2.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "table.tabelaLinha tbody tr, table tbody tr, [class*='movimentacao']")
        for row in rows:
            p = _extract_pub_from_row(row.text, "Projudi", cnj_hint=cnj_fmt)
            if p: pubs.append(p)
    except Exception as e:
        err = str(e)[:200]
    return {"pubs": pubs, "url": url, "error": err, "tribunal": cnj_to_tribunal(cnj_digits)}


def _scraper_selenium_esaj(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return {"pubs": [], "url": "", "error": "", "tribunal": ""}
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://esaj.tjsp.jus.br/cpopg/search.do?cbPesquisa=NUMPROC&dadosConsulta.valorConsultaNuUnificado={cnj_fmt}"
    pubs, err = [], ""
    try:
        driver = _get_driver_singleton()
        driver.set_page_load_timeout(15)
        driver.get(url)
        time.sleep(2.0)
        rows = driver.find_elements(By.CSS_SELECTOR, "#tabelaUltimasMovimentacoes tr, .movimentacao, table tbody tr, [class*='movimentacaoItem']")
        for row in rows:
            p = _extract_pub_from_row(row.text, "e-SAJ", cnj_hint=cnj_fmt)
            if p: pubs.append(p)
    except Exception as e:
        err = str(e)[:200]
    return {"pubs": pubs, "url": url, "error": err, "tribunal": cnj_to_tribunal(cnj_digits)}


# ===================== INTERFACE =====================

SCRAPERS = {
    "pje":     _scraper_selenium_pje,
    "eproc":   _scraper_selenium_eproc,
    "projudi": _scraper_selenium_projudi,
    "esaj":    _scraper_selenium_esaj,
}


def scraper_pje_for_case(cnj, system="pje"):
    """Pipeline de busca por CNJ:
    1. Selenium no Comunica PJE / eProc / Projudi / e-SAJ (fonte primaria real)
    2. DataJud CNJ API (fallback se Selenium falhar ou vier vazio)
    Retorna dict com pubs, url, error, tribunal, case_info.
    """
    _, digits = normalize_cnj(cnj)
    if not digits or len(digits) != 20:
        return {"pubs": [], "url": "", "error": "CNJ invalido", "tribunal": ""}
    cnj_fmt = f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    tribunal = cnj_to_tribunal(digits)
    err_total = ""

    # 1) Selenium PRIMARIO - fonte real de publicacoes
    fn = SCRAPERS.get((system or "pje").lower(), _scraper_selenium_pje)
    try:
        res = fn(digits)
        if res.get("pubs"):
            res["tribunal"] = res.get("tribunal") or tribunal
            res["source"] = "selenium"
            # tenta extrair case_info do primeiro pub
            first = res["pubs"][0]
            res["case_info"] = {
                "classe": first.get("classe", ""),
                "assunto": first.get("assunto", ""),
                "orgao": first.get("orgao", ""),
                "magistrado": first.get("magistrado", ""),
                "valor_causa": first.get("valor_causa", ""),
                "partes": first.get("partes", []),
            }
            return res
        err_total = res.get("error", "") or "Selenium Comunica PJE retornou 0 publicacoes"
    except Exception as e:
        err_total = f"Selenium: {str(e)[:120]}"

    # 2) Fallback DataJud - soh se Selenium nao trouxe nada
    if tribunal in _TRIBUNAL_CODES:
        try:
            pubs, raw = _datajud_get_movs(digits, tribunal)
            if pubs:
                case_info = {
                    "classe": pubs[0].get("classe", ""),
                    "assunto": pubs[0].get("assunto", ""),
                    "orgao": pubs[0].get("orgao", ""),
                    "magistrado": pubs[0].get("magistrado", ""),
                    "valor_causa": pubs[0].get("valor_causa", ""),
                    "partes": pubs[0].get("partes", []),
                }
                return {
                    "pubs": pubs, "url": f"{DATAJUD_BASE}/api_publica_{_TRIBUNAL_CODES[tribunal]}/_search",
                    "error": "", "tribunal": tribunal, "case_info": case_info, "source": "datajud",
                }
        except Exception as e:
            err_total += f" | DataJud: {str(e)[:80]}"

    return {"pubs": [], "url": "", "error": err_total, "tribunal": tribunal, "case_info": {}}


def scraper_pje_for_oab(numero_oab, uf, timeout=45):
    """Busca por OAB no Comunica PJE (sem Selenium direto no DataJud, porque o DataJud nao indexa OAB).
    URL: https://comunica.pje.jus.br/consulta?siglaTribunal={TJ}&numeroOab={num}&ufOab={uf}
    Retorna dict com pubs, url, error.
    """
    if not numero_oab or not uf or not SELENIUM_OK:
        return {"pubs": [], "url": "", "error": "selenium nao disponivel ou parametros invalidos", "tribunal": ""}
    sigla = uf_to_tribunal(uf)
    url = f"https://comunica.pje.jus.br/consulta?siglaTribunal={sigla}&numeroOab={numero_oab}&ufOab={uf}"
    pubs, err = [], ""
    try:
        driver = _get_driver_singleton()
        # v4.3.0: set_page_load_timeout 30s (sem filtro de data - URL limpa)
        driver.set_page_load_timeout(30)
        try:
            driver.get(url)
        except Exception as e:
            return {"pubs": [], "url": url, "error": f"timeout carregando Comunica PJE (30s)", "tribunal": sigla, "source": "selenium"}
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "table tbody tr, [class*='item'], [class*='publicacao'], [class*='resultado'], [class*='movimentacao'], mat-card, app-publicacao, .ng-star-inserted"
                ))
            )
            time.sleep(2.0)
            # v4.3.0: scrolla ate o fim pra carregar todas as publicacoes (lazy load)
            try:
                for _ in range(5):
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(0.6)
            except Exception: pass
            # volta pro topo
            try: driver.execute_script("window.scrollTo(0, 0);")
            except Exception: pass
            rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr, [class*='item'], [class*='publicacao'], mat-card, app-publicacao, .publicacao-item, .ng-star-inserted")
            seen = set()
            for row in rows:
                txt = row.text or ""
                if not txt or len(txt) < 5: continue
                # dedup por hash do texto - evita duplicar quando varios seletores pegam o mesmo elemento
                key = txt[:120]
                if key in seen: continue
                seen.add(key)
                p = _extract_pub_from_row(txt, "PJE-OAB")
                if p: pubs.append(p)
            # Sem fallback por body scan: CNJ aleatorio no DOM nao e publicacao real
            # Se nao achou linhas, retorna lista vazia (e o handler vai dizer "0 pubs")
        except Exception as e:
            err = f"timeout: {str(e)[:120]}"
    except Exception as e:
        err = str(e)[:200]
    return {"pubs": pubs, "url": url, "error": err, "tribunal": sigla, "source": "selenium"}


# ===================== WORKER =====================

class MonitoringWorker:
    def __init__(self, db_path=None, get_api_key_fn=None, interval_seconds=2160):
        if db_path is None and interval_seconds == 2160:
            pass
        self.interval = max(15, int(interval_seconds))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._check_all()
            except Exception as e:
                print(f"[worker] {e}")
            self._stop.wait(self.interval)

    def _check_all(self):
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT DISTINCT u.id AS user_id, u.oab, u.oab_uf
                FROM users u JOIN cases c ON c.responsible_id = u.id
                WHERE c.monitoring_active = 1 AND c.deleted_at IS NULL
                  AND (c.system IS NULL OR c.system IN ('pje','eproc','projudi','esaj','trt','stf','stj','manual'))
                  AND u.oab IS NOT NULL
            """).fetchall()
            for r in rows:
                oab = _parse_oab(r["oab"] or "")
                uf = r["oab_uf"] or oab["uf"]
                num = oab["numero"]
                if not (num and uf):
                    continue
                res = scraper_pje_for_oab(num, uf)
                pubs = res.get("pubs", [])
                now = datetime.utcnow().isoformat()
                for p in pubs:
                    try:
                        conn.execute("""
                            INSERT INTO monitoring_log(id,case_id,checked_at,publications_found,user_id,source,raw)
                            VALUES(?,?,?,?,?,?,?)
                        """, (f"log-{int(time.time()*1000)}-{hash(p.get('raw',''))%1000000:06d}",
                              None, now, json.dumps(p, ensure_ascii=False), r["user_id"], "oab",
                              p.get("raw","")[:1000]))
                    except Exception:
                        pass
            conn.commit()
        finally:
            conn.close()

    def _find_case_by_cnj(self, cnj_fmt, responsible_id=None):
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return _find_case_by_cnj(conn, cnj_fmt, responsible_id)
        finally:
            conn.close()

    def _auto_create_case(self, pub, responsible_id, system="pje"):
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return _auto_create_case(conn, pub, responsible_id, system)
        finally:
            conn.close()

    def _insert_dedupe_pubs_for_case(self, case_id, pubs):
        if not case_id:
            return 0
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return _insert_dedupe_pubs_for_case(conn, case_id, pubs)
        finally:
            conn.close()



# Mapeamento de classe CNJ -> area juridica
_AREA_MAP = [
    ("fiscal", "Tributario"),
    ("tribut", "Tributario"),
    ("consumo", "Consumidor"),
    ("consumid", "Consumidor"),
    ("familia", "Familia"),
    ("sucess", "Sucessoes"),
    ("empresarial", "Empresarial"),
    ("societar", "Empresarial"),
    ("recuperacao judicial", "Empresarial"),
    ("falenc", "Empresarial"),
    ("previdenc", "Previdenciario"),
    ("ambient", "Ambiental"),
    ("penal", "Criminal"),
    ("criminal", "Criminal"),
    ("inquerito", "Criminal"),
    ("homicid", "Criminal"),
    ("roubo", "Criminal"),
    ("trabalh", "Trabalhista"),
    ("civel", "Civel"),
    ("obrigac", "Civel"),
    ("contrat", "Civel"),
    ("indenizat", "Civel"),
    ("civil", "Civel"),
]

def _class_to_area(classe):
    if not classe:
        return "Civel"
    cl = classe.lower()
    for needle, area in _AREA_MAP:
        if needle in cl:
            return area
    return "Civel"


# ===================== DB HELPERS =====================

def _auto_create_case(conn, pub, responsible_id, system="pje"):
    cnj_fmt, cnj_digits = normalize_cnj(pub.get("cnj",""))
    if not cnj_digits or len(cnj_digits) != 20:
        return None
    existing = _find_case_by_cnj(conn, cnj_fmt, responsible_id)
    if existing:
        return existing
    cid = f"case-{int(time.time()*1000)}-{hash(cnj_fmt)%1000000:06d}"
    now = datetime.utcnow().isoformat()
    title = pub.get("title") or f"Processo {cnj_fmt}"
    area = _class_to_area(pub.get("classe",""))
    court = pub.get("orgao") or pub.get("assunto") or ""
    try:
        conn.execute("""
            INSERT INTO cases(id,code,title,area,court,status,priority,responsible_id,system,monitoring_active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (cid, cnj_fmt, title, area, court, "em_andamento", "media",
              responsible_id, system, 1, now))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO monitoring(case_id, status, interval_minutes, tribunal, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (cid, "active", 60, cnj_to_tribunal(cnj_digits), now, now),
            )
        except Exception:
            pass
        # Cria primeira tarefa derivada da publicacao, se for intimacao/citacao
        try:
            tipo = (pub.get("type") or "").lower()
            if "intima" in tipo or "cita" in tipo:
                due = now[:10]
                tid = f"task-{int(time.time()*1000)}-{hash(cnj_fmt+'init')%1000000:06d}"
                conn.execute("""
                    INSERT INTO tasks(id,case_id,title,description,status,priority,due_date,created_at)
                    VALUES(?,?,?,?,?,?,?,?)
                """, (tid, cid, f"Responder {pub.get('type','publicacao')} - {cnj_fmt}",
                      (pub.get("description","")[:500]), "pendente", "alta", due, now))
        except Exception:
            pass
        conn.commit()
        return cid
    except Exception as e:
        print(f"[auto_create] {e}")
        return None


def _insert_dedupe_pubs_for_case(conn, case_id, pubs):
    """Insere pubs como case_updates com dedup forte baseado em hash(titulo+desc+date)."""
    inserted = 0
    for p in pubs:
        date_iso = ""
        if p.get("date"):
            try:
                d, m, y = p["date"].split("/")
                date_iso = f"{y}-{m}-{d}"
            except Exception:
                pass
        title = (p.get("title","") or "")[:200]
        desc = (p.get("description","") or "")[:1000]
        tipo = (p.get("type") or "publicacao").lower()
        # Hash deterministico (sem millisegundos) para dedup
        h = hash((title, desc[:120], date_iso, tipo)) & 0xFFFFFF
        marker = f"[hash:{h:06x}]"
        existing = conn.execute(
            "SELECT id FROM case_updates WHERE case_id=? AND description LIKE ? LIMIT 1",
            (case_id, marker + "%"),
        ).fetchone()
        if existing:
            continue
        uid = f"upd-{int(time.time()*1000)}-{hash(desc[:120]+title)%1000000:06d}"
        try:
            conn.execute("""
                INSERT INTO case_updates(id,case_id,type,description,date,created_at)
                VALUES(?,?,?,?,?,?)
            """, (uid, case_id, "publicacao", marker + " " + desc, date_iso, datetime.utcnow().isoformat()))
            inserted += 1
        except Exception as e:
            print(f"[insert_pub] {e}")
    if inserted:
        conn.commit()
    return inserted


def _find_case_by_cnj(conn, cnj_fmt, responsible_id=None):
    cnj_fmt_n, cnj_digits = normalize_cnj(cnj_fmt or "")
    candidates = [cnj_fmt_n]
    if cnj_digits and len(cnj_digits) == 20 and cnj_digits not in candidates:
        candidates.append(cnj_digits)
    if cnj_digits and len(cnj_digits) == 20:
        formatted = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
        if formatted not in candidates:
            candidates.append(formatted)
    for c in candidates:
        if not c:
            continue
        try:
            row = conn.execute("""
                SELECT id FROM cases
                WHERE code=? AND (deleted_at IS NULL OR deleted_at = '')
                ORDER BY (responsible_id = ?) DESC LIMIT 1
            """, (c, responsible_id or "")).fetchone()
        except Exception:
            return None
        if row:
            try:
                return row["id"]
            except (TypeError, KeyError, IndexError):
                return row[0] if len(row) > 0 else None
    return None


def check_oab(oab_raw, uf, responsible_id=None):
    oab = _parse_oab(oab_raw or "")
    num = oab["numero"] or (oab_raw or "")
    uf = uf or oab["uf"]
    if not (num and uf):
        return {"error": "OAB invalida", "pubs": []}
    res = scraper_pje_for_oab(num, uf)
    pubs = res.get("pubs", [])
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        for p in pubs:
            cnj_fmt, _ = normalize_cnj(p.get("cnj",""))
            if not cnj_fmt:
                continue
            case_id = _find_case_by_cnj(conn, cnj_fmt, responsible_id)
            if not case_id:
                case_id = _auto_create_case(conn, p, responsible_id, system="pje")
            if case_id:
                _insert_dedupe_pubs_for_case(conn, case_id, [p])
        return {"pubs_found": len(pubs), "pubs": pubs, "url": res.get("url", "")}
    finally:
        conn.close()


def check_case(case_id):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT c.id, c.code, c.system, c.responsible_id
            FROM cases c WHERE c.id=? AND c.deleted_at IS NULL
        """, (case_id,)).fetchone()
        if not row:
            return {"error": "Caso nao encontrado", "pubs": []}
        cnj_fmt, cnj_digits = normalize_cnj(row["code"] or "")
        if not cnj_digits or len(cnj_digits) != 20:
            return {"error": "Caso sem CNJ valido", "pubs": []}
        system = row["system"] or "pje"
        res = scraper_pje_for_case(cnj_fmt, system)
        pubs = res.get("pubs", [])
        case_id_use = _find_case_by_cnj(conn, cnj_fmt, row["responsible_id"]) or case_id
        inserted = _insert_dedupe_pubs_for_case(conn, case_id_use, pubs)
        return {
            "pubs_found": len(pubs), "inserted": inserted, "pubs": pubs,
            "url": res.get("url", ""),
            "tribunal": res.get("tribunal", ""),
            "case_info": res.get("case_info", {}),
            "source": res.get("source", "selenium"),
            "error": res.get("error", ""),
        }
    finally:
        conn.close()


def detect_tribunal(cnj):
    return cnj_to_tribunal(re.sub(r"\D","",str(cnj or "")))


scraper_pje = scraper_pje_for_case
