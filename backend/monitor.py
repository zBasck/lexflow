# -*- coding: utf-8 -*-
"""Monitor de publicacoes juridicas - LexFlow. Suporta PJE, eProc, Projudi, e-SAJ."""

import os, re, json, time, sqlite3, threading
from datetime import datetime

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


def get_driver():
    if not SELENIUM_OK:
        raise RuntimeError("selenium nao instalado")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def normalize_cnj(raw):
    if not raw:
        return ("", "")
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) != 20:
        return (str(raw).strip(), digits)
    formatted = f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    return (formatted, digits)


def cnj_to_tribunal(cnj_digits):
    if len(cnj_digits) < 16:
        return ""
    uf_map = {"01":"AC","02":"AL","03":"AP","04":"AM","05":"BA","06":"CE","07":"DF","08":"ES","09":"GO","10":"MA","11":"MT","12":"MS","13":"MG","14":"PA","15":"PB","16":"PR","17":"PE","18":"PI","19":"RJ","20":"RN","21":"RS","22":"RO","23":"RR","24":"SC","25":"SP","26":"SE","27":"TO"}
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


_DRIVER = None
_DRIVER_LOCK = threading.Lock()


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


def _wait_for_rows(driver, timeout=20):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr, .publicacao, [class*='resultado'], [class*='movimentacao']"))
        )
        return True
    except Exception:
        return False


def _extract_pub_from_row(txt, source_label):
    txt = (txt or "").strip()
    if not txt or len(txt) < 5:
        return None
    cnj_m = re.search(r"\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}", txt)
    cnj = cnj_m.group(0) if cnj_m else ""
    date_m = re.search(r"\d{2}/\d{2}/\d{4}", txt)
    date = date_m.group(0) if date_m else ""
    tipo = "Publicacao"
    for k in ("Intimacao","Citacao","Sentenca","Despacho","Audiencia","Ato Ordinatorio","Edital","Decisao"):
        if k.lower() in txt.lower():
            tipo = k
            break
    return {
        "cnj": cnj,
        "date": date,
        "title": f"{tipo} - {cnj}" if cnj else f"{tipo} - {source_label}",
        "description": txt[:500],
        "raw": txt,
    }


def _scraper_selenium_pje(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return []
    url = f"https://comunica.pje.jus.br/consulta?numeroProcesso={cnj_digits}"
    pubs = []
    try:
        driver = _get_driver_singleton()
        driver.get(url)
        if not _wait_for_rows(driver):
            return []
        time.sleep(1.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            rows = driver.find_elements(By.CSS_SELECTOR, "[class*='item'], [class*='publicacao']")
        for row in rows:
            p = _extract_pub_from_row(row.text, "PJE")
            if p: pubs.append(p)
    except Exception as e:
        print(f"[pje] {e}")
    return pubs


def _scraper_selenium_eproc(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return []
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://eproc1g.tjrj.jus.br/eproc/externo_controlador.php?acao=consulta_publica&num_processo={cnj_fmt}"
    pubs = []
    try:
        driver = _get_driver_singleton()
        driver.get(url)
        time.sleep(2.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "table.tabelaMovimentacoes tbody tr, table tbody tr")
        for row in rows:
            p = _extract_pub_from_row(row.text, "eProc")
            if p: pubs.append(p)
    except Exception as e:
        print(f"[eproc] {e}")
    return pubs


def _scraper_selenium_projudi(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return []
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://projudi.tjrj.jus.br/projudi/consultaPublica.do?actionType=consulta&numero={cnj_fmt}"
    pubs = []
    try:
        driver = _get_driver_singleton()
        driver.get(url)
        time.sleep(2.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "table.tabelaLinha tbody tr, table tbody tr")
        for row in rows:
            p = _extract_pub_from_row(row.text, "Projudi")
            if p: pubs.append(p)
    except Exception as e:
        print(f"[projudi] {e}")
    return pubs


def _scraper_selenium_esaj(cnj_digits):
    if not cnj_digits or len(cnj_digits) != 20 or not SELENIUM_OK:
        return []
    cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
    url = f"https://esaj.tjsp.jus.br/cpopg/search.do?cbPesquisa=NUMPROC&dadosConsulta.valorConsultaNuUnificado={cnj_fmt}"
    pubs = []
    try:
        driver = _get_driver_singleton()
        driver.get(url)
        time.sleep(2.5)
        rows = driver.find_elements(By.CSS_SELECTOR, "#tabelaUltimasMovimentacoes tr, .movimentacao, table tbody tr")
        for row in rows:
            p = _extract_pub_from_row(row.text, "e-SAJ")
            if p: pubs.append(p)
    except Exception as e:
        print(f"[esaj] {e}")
    return pubs


SCRAPERS = {
    "pje":     _scraper_selenium_pje,
    "eproc":   _scraper_selenium_eproc,
    "projudi": _scraper_selenium_projudi,
    "esaj":    _scraper_selenium_esaj,
}


def scraper_pje_for_case(cnj, system="pje"):
    _, digits = normalize_cnj(cnj)
    fn = SCRAPERS.get((system or "pje").lower(), _scraper_selenium_pje)
    return fn(digits)


def scraper_pje_for_oab(numero_oab, uf):
    if not numero_oab or not uf or not SELENIUM_OK:
        return []
    sigla = uf_to_tribunal(uf)
    url = f"https://comunica.pje.jus.br/consulta?siglaTribunal={sigla}&numeroOab={numero_oab}&ufOab={uf}"
    pubs = []
    try:
        driver = _get_driver_singleton()
        driver.get(url)
        if not _wait_for_rows(driver):
            return []
        time.sleep(1.5)
        for row in driver.find_elements(By.CSS_SELECTOR, "table tbody tr"):
            p = _extract_pub_from_row(row.text, "PJE-OAB")
            if p: pubs.append(p)
    except Exception as e:
        print(f"[pje-oab] {e}")
    return pubs


class MonitoringWorker:
    def __init__(self, db_path=None, get_api_key_fn=None, interval_seconds=2160):
        # Aceita tanto (interval_minutes=60) quanto (db_path=..., interval_seconds=...)
        # para retro-compatibilidade com o que o server.py envia hoje.
        if db_path is None and interval_seconds == 2160:
            # Chamada antiga: MonitoringWorker(interval_minutes=60)
            # nada a fazer, defaults ja estao certos
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
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT DISTINCT u.id AS user_id, u.oab, u.oab_uf
                FROM users u JOIN cases c ON c.responsible_id = u.id
                WHERE c.monitoring_active = 1 AND c.deleted_at IS NULL
                  AND (c.system = 'pje' OR c.system IS NULL) AND u.oab IS NOT NULL
            """).fetchall()
            for r in rows:
                oab = _parse_oab(r["oab"] or "")
                uf = r["oab_uf"] or oab["uf"]
                num = oab["numero"]
                if not (num and uf):
                    continue
                pubs = scraper_pje_for_oab(num, uf)
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


def _auto_create_case(conn, pub, responsible_id, system="pje"):
    cnj_fmt, cnj_digits = normalize_cnj(pub.get("cnj",""))
    if not cnj_digits or len(cnj_digits) != 20:
        return None
    row = conn.execute("SELECT id FROM cases WHERE code=? AND deleted_at IS NULL", (cnj_fmt,)).fetchone()
    if row:
        return row["id"]
    cid = f"case-{int(time.time()*1000)}-{hash(cnj_fmt)%1000000:06d}"
    now = datetime.utcnow().isoformat()
    try:
        conn.execute("""
            INSERT INTO cases(id,code,title,area,status,priority,responsible_id,system,monitoring_active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (cid, cnj_fmt, f"Processo {cnj_fmt}", "monitoramento", "em_andamento", "media",
              responsible_id, system, 1, now))
        conn.commit()
        return cid
    except Exception:
        return None


def _insert_dedupe_pubs_for_case(conn, case_id, pubs):
    inserted = 0
    for p in pubs:
        date_iso = ""
        if p.get("date"):
            try:
                d, m, y = p["date"].split("/")
                date_iso = f"{y}-{m}-{d}"
            except Exception:
                pass
        title = p.get("title","")[:200]
        desc = p.get("description","")[:1000]
        existing = conn.execute("""
            SELECT id FROM case_updates
            WHERE case_id=? AND date=? AND title=? AND description LIKE ?
            LIMIT 1
        """, (case_id, date_iso, title, desc[:120] + "%")).fetchone()
        if existing:
            continue
        uid = f"upd-{int(time.time()*1000)}-{hash(desc[:120]+title)%1000000:06d}"
        try:
            conn.execute("""
                INSERT INTO case_updates(id,case_id,type,description,date,created_at)
                VALUES(?,?,?,?,?,?)
            """, (uid, case_id, "publicacao", desc, date_iso, datetime.utcnow().isoformat()))
            inserted += 1
        except Exception:
            pass
    conn.commit()
    return inserted


def _find_case_by_cnj(conn, cnj_fmt, responsible_id=None):
    row = conn.execute("""
        SELECT id FROM cases
        WHERE code=? AND deleted_at IS NULL
        ORDER BY (responsible_id = ?) DESC LIMIT 1
    """, (cnj_fmt, responsible_id or "")).fetchone()
    return row["id"] if row else None


def check_oab(oab_raw, uf, responsible_id=None):
    oab = _parse_oab(oab_raw or "")
    num = oab["numero"] or (oab_raw or "")
    uf = uf or oab["uf"]
    if not (num and uf):
        return {"error": "OAB invalida", "pubs": []}
    pubs = scraper_pje_for_oab(num, uf)
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
        return {"pubs_found": len(pubs), "pubs": pubs}
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
        pubs = scraper_pje_for_case(cnj_fmt, system)
        case_id_use = _find_case_by_cnj(conn, cnj_fmt, row["responsible_id"]) or case_id
        inserted = _insert_dedupe_pubs_for_case(conn, case_id_use, pubs)
        return {
            "pubs_found": len(pubs), "inserted": inserted, "pubs": pubs,
            "url": f"https://comunica.pje.jus.br/consulta?numeroProcesso={cnj_digits}" if system == "pje" else None,
        }
    finally:
        conn.close()


def detect_tribunal(cnj):
    return cnj_to_tribunal(re.sub(r"\D","",str(cnj or "")))


scraper_pje = scraper_pje_for_case
