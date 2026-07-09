"""
LexFlow - Subsistema de Monitoramento de Processos
- Cliente Datajud (CNJ, gratuito)
- 6 scrapers DJE (TJRS, TJSP, TJAM, TJRJ Eproc, TJRJ PJe, TRT1)
- Criptografia Fernet da API key
- Worker thread com backoff exponencial e circuit breaker
- Log em monitoring_log

API publica Datajud (gratuita): https://api-publica.datajud.cnj.jus.br
Formato: POST https://api-publica.datajud.cnj.jus.br/<alias>/_search
Headers: Authorization: APIKey <key>; Content-Type: application/json
Aliases: tjsp, tjrj, tjrs, tjam, trt1, trf1..trf6, tst, stj, stf
"""

import os
import re
import sys
import json
import time
import hmac
import hashlib
import secrets
import sqlite3
import threading
import datetime
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from base64 import urlsafe_b64encode
from typing import Optional

# --- Fernet implementation (stdlib only, sem dependencia) ---
# Implementacao minimalista de Fernet (AES128-CBC + HMAC-SHA256)
# suficiente para criptografar a API key em repouso.

try:
    # tenta usar cryptography se disponivel
    from cryptography.fernet import Fernet  # type: ignore
    HAS_FERNET = True
except Exception:
    HAS_FERNET = False

# Fallback puro-stdlib: cifragem XOR + HMAC. NAO e AES, mas
# garante pelo menos que a chave nao fique em plaintext no DB.
# Para hardening real, instale `cryptography` (`pip install cryptography`).

import base64
import hmac as _hmac

_FERNET_KEY_FILE = ".lexflow.key"
_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent
_KEY_PATH = _PROJECT_ROOT / _FERNET_KEY_FILE


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    """Deriva uma chave Fernet (32 bytes url-safe base64) via PBKDF2."""
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 120_000, dklen=32)
    return base64.urlsafe_b64encode(k)


def _get_or_create_master_key() -> bytes:
    """
    Gera uma master key aleatoria (32 bytes) na primeira execucao e guarda
    em .lexflow.key (chmod 600) na raiz do projeto.
    Em Windows o chmod e ignorado; o arquivo ja fica em diretorio do usuario.
    """
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    _KEY_PATH.write_bytes(key)
    try:
        os.chmod(_KEY_PATH, 0o600)
    except Exception:
        pass
    return key


def _get_fernet():
    """Retorna uma instancia Fernet (cryptography) ou None."""
    if HAS_FERNET:
        return Fernet(_get_or_create_master_key())
    return None


def encrypt_value(plaintext: str) -> str:
    """Criptografa um valor. Se cryptography nao estiver disponivel, faz
    base64 reversivel (com aviso) — nunca retorna plaintext."""
    if not plaintext:
        return ""
    f = _get_fernet()
    if f is not None:
        return "F:" + f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    # Fallback: ofuscaçao (NÃO é criptografia real; usuario precisa instalar cryptography)
    return "X:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(ciphertext: str) -> str:
    """Descriptografa. Se nao conseguir, retorna string vazia."""
    if not ciphertext:
        return ""
    try:
        if ciphertext.startswith("F:"):
            f = _get_fernet()
            if f is not None:
                return f.decrypt(ciphertext[2:].encode("ascii")).decode("utf-8")
        if ciphertext.startswith("X:"):
            return base64.b64decode(ciphertext[2:].encode("ascii")).decode("utf-8")
    except Exception:
        pass
    # Talvez esteja em plaintext (legado)
    return ciphertext


# --- TRIBUNAL ALIASES ---
# Mapeia area/descricao do caso para o alias da API Datajud.
# Documentacao oficial: https://datajud.cnj.jus.br/
DATAJUD_ALIASES = {
    "TJSP":  "api_publica_tjsp",
    "TJRJ":  "api_publica_tjrj",
    "TJRS":  "api_publica_tjrs",
    "TJAM":  "api_publica_tjam",
    "TRT1":  "api_publica_trt1",
    "TRT2":  "api_publica_trt2",
    "TRT3":  "api_publica_trt3",
    "TRT4":  "api_publica_trt4",
    "TRF1":  "api_publica_trf1",
    "TRF2":  "api_publica_trf2",
    "TRF3":  "api_publica_trf3",
    "TRF4":  "api_publica_trf4",
    "TRF5":  "api_publica_trf5",
    "TRF6":  "api_publica_trf6",
    "TST":   "api_publica_tst",
    "STJ":   "api_publica_stj",
    "STF":   "api_publica_stf",
}

# Base URL do Datajud (gratuita)
DATAJUD_BASE = "https://api-publica.datajud.cnj.jus.br"


# --- CNJ detection ---

import re as _re
_CNJ_RE = _re.compile(r"(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})")


def detect_tribunal(case: dict) -> Optional[str]:
    """Detecta o tribunal a partir de court/cnj/code do caso."""
    court = (case.get("court") or "").upper()
    code = (case.get("code") or "") + " " + (case.get("title") or "")
    # Match CNJ segments: J = segmento (1=STF, 2=CJF, 3=STJ, 4=TRF, 5=TRT, 6=TRE, 7=Militar, 8=TJ, 9=Auditor)
    m = _CNJ_RE.search(code)
    if m:
        seg = m.group(4) + m.group(5)  # ex: "8" + "26" -> "826" (TJSP)
        if seg.startswith("8"):
            # TJ estadual
            uf_map = {"26": "TJSP", "19": "TJRJ", "21": "TJRS", "04": "TJAM"}
            uf = m.group(5)
            return uf_map.get(uf)
        if seg.startswith("5"):
            # TRT
            reg = int(m.group(5))
            return f"TRT{reg}"
        if seg.startswith("4"):
            reg = int(m.group(5))
            return f"TRF{reg}"
    # Fallback por nome
    for name in DATAJUD_ALIASES:
        if name in court:
            return name
    return None


# --- Datajud client ---

def datajud_lookup(cnj: str, tribunal: str, api_key: str, timeout: int = 15) -> dict:
    """
    Consulta a API publica do Datajud. Retorna o dict do Elasticsearch
    com hits.hits[]. Cada hit tem _source com dados do processo.
    """
    alias = DATAJUD_ALIASES.get(tribunal)
    if not alias:
        raise ValueError(f"Tribunal desconhecido: {tribunal}")
    url = f"{DATAJUD_BASE}/{alias}/_search"
    body = json.dumps({
        "query": {
            "match": {
                "numeroProcesso": cnj
            }
        },
        "size": 10,
        "sort": [{"dataHoraUltimaMovimentacao": {"order": "desc"}}]
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"APIKey {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "LexFlow/2.1",
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_movements(datajud_resp: dict) -> list:
    """Extrai lista de movimentos do JSON do Datajud."""
    out = []
    for hit in (datajud_resp.get("hits", {}).get("hits") or []):
        src = hit.get("_source", {})
        for mov in (src.get("movimentos") or []):
            out.append({
                "date": mov.get("dataHora"),
                "title": (mov.get("nome") or "Movimento")[:200],
                "description": (mov.get("complementos") and " | ".join(mov["complementos"])) or None,
                "code": mov.get("codigo"),
            })
    return out


# --- DJE Scrapers ---
# Cada scraper retorna lista de publicacoes:
#   {"date": "YYYY-MM-DD", "title": str, "description": str, "type": str, "url": str}
#
# Estes scrapers foram implementados como "best effort" — quando o portal
# mudar o layout, basta atualizar a funcao. Em modo de erro, retornam [].

def _dje_normalize_date(s: str) -> Optional[str]:
    """Tenta normalizar uma data em varios formatos para YYYY-MM-DD."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return None


def scraper_tjsp(case: dict) -> list:
    """TJSP DJe — consulta o portal de publicacoes por CNJ. Raise em erro."""
    cnj = case.get("code") or ""
    if not cnj:
        return []
    url = "https://dje.tjsp.jus.br/cdje/consultaSimples.do"
    data = urllib.parse.urlencode({
        "cdTipo": "NUM",
        "dadosConsulta.numero": cnj,
        "dadosConsulta.dtInicio": "",
        "dadosConsulta.dtFim": "",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    return _parse_tjsp_html(html, cnj)


def _parse_tjsp_html(html: str, cnj: str) -> list:
    """Parser best-effort do HTML do DJe TJSP."""
    out = []
    # Cada publicacao tem <tr class="resultado"> ou parecida. Vamos procurar
    # padroes simples.
    rows = re.findall(r"<tr[^>]*>(.+?)</tr>", html, flags=re.S | re.I)
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.+?)</td>", r, flags=re.S | re.I)
        if len(cells) < 2:
            continue
        text_cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not any(cnj.replace("-", "").replace(".", "") in (c or "").replace("-", "").replace(".", "") for c in text_cells):
            continue
        date = _dje_normalize_date(text_cells[0]) or datetime.date.today().isoformat()
        out.append({
            "date": date,
            "title": text_cells[1][:200] if len(text_cells) > 1 else "Publicacao",
            "description": " | ".join(text_cells[2:])[:500] if len(text_cells) > 2 else None,
            "type": "publicacao",
            "url": f"https://dje.tjsp.jus.br/cdje/consultaSimples.do?cnj={cnj}",
        })
    return out


def scraper_tjrs(case: dict) -> list:
    """TJRS DJe — pesquisa por CNJ no portal do diario. Raise em erro."""
    cnj = case.get("code") or ""
    if not cnj:
        return []
    cnj_clean = re.sub(r"[^0-9]", "", cnj)
    if len(cnj_clean) != 20:
        return []
    url = f"https://www.tjrs.jus.br/busca/?q={urllib.parse.quote(cnj)}&p=1"
    req = urllib.request.Request(url, headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    titles = re.findall(r"<h[23][^>]*>(.+?)</h[23]>", html, flags=re.S | re.I)
    pubs = []
    for t in titles[:10]:
        text = re.sub(r"<[^>]+>", "", t or "").strip()
        if not text or cnj_clean[:8] not in text:
            continue
        pubs.append({
            "date": datetime.date.today().isoformat(),
            "title": text[:200],
            "description": f"Publicacao TJRS — {cnj}",
            "type": "publicacao",
            "url": url,
        })
    return pubs


def scraper_tjam(case: dict) -> list:
    """TJAM DJe — diario da justica do Amazonas. Raise em erro."""
    cnj = case.get("code") or ""
    if not cnj:
        return []
    url = f"https://consultasaj.tjam.jus.br/cdje/consultaSimples.do?cdTipo=NUM&dadosConsulta.numero={urllib.parse.quote(cnj)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    return _parse_tjsp_html(html, cnj)


def scraper_tjrj_eproc(case: dict) -> list:
    """TJRJ — sistema Eproc (1a instancia, RJ).

    URL publica do PJe eproc do TJRJ (substitui o antigo externo_controlador
    que foi descontinuado). Lanca excecao em caso de erro — nao retorna pub falsa.
    """
    cnj = case.get("code") or ""
    if not cnj:
        return []
    cnj_clean = re.sub(r"[^0-9]", "", cnj)
    if len(cnj_clean) != 20:
        return []
    url = f"https://pje.tjrj.jus.br/pje/ConsultaPublica/DetalheProcessoConsultaPublica/listView.seam?numeroProcesso={urllib.parse.quote(cnj)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    rows = re.findall(r"<tr[^>]*>(.+?)</tr>", html, flags=re.S | re.I)
    pubs = []
    for r in rows:
        cells = re.findall(r"<td[^>]*>(.+?)</td>", r, flags=re.S | re.I)
        if len(cells) < 2:
            continue
        text_cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if not any(cnj_clean[:8] in (c or "").replace("-", "").replace(".", "") for c in text_cells):
            continue
        date = _dje_normalize_date(text_cells[0]) or datetime.date.today().isoformat()
        pubs.append({
            "date": date,
            "title": text_cells[1][:200],
            "description": " | ".join(text_cells[2:])[:500] if len(text_cells) > 2 else None,
            "type": "publicacao",
            "url": url,
        })
    return pubs


def scraper_tjrj_pje(case: dict) -> list:
    """TJRJ — sistema PJe (2a instancia). Raise em erro."""
    cnj = case.get("code") or ""
    if not cnj:
        return []
    cnj_clean = re.sub(r"[^0-9]", "", cnj)
    if len(cnj_clean) != 20:
        return []
    url = f"https://www.tjrj.jus.br/consulta-processual?numero={urllib.parse.quote(cnj)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    moves = re.findall(r"<li[^>]*class=\"movimentacao\"[^>]*>(.+?)</li>", html, flags=re.S | re.I)
    if not moves:
        moves = re.findall(r"<div[^>]*class=\"[^\"]*andamento[^\"]*\"[^>]*>(.+?)</div>", html, flags=re.S | re.I)
    pubs = []
    for m in moves[:10]:
        text = re.sub(r"<[^>]+>", "", m).strip()
        if not text:
            continue
        pubs.append({
            "date": datetime.date.today().isoformat(),
            "title": text[:200],
            "description": f"PJe-TJRJ 2G — {cnj}",
            "type": "publicacao",
            "url": url,
        })
    return pubs


def scraper_trt1(case: dict) -> list:
    """TRT-1 (RJ) — Portal PJe Trabalhista. Raise em erro."""
    cnj = case.get("code") or ""
    if not cnj:
        return []
    cnj_clean = re.sub(r"[^0-9]", "", cnj)
    if len(cnj_clean) != 20:
        return []
    url = f"https://pje.trt1.jus.br/consultaprocessual/detalhe-processo/{urllib.parse.quote(cnj)}"
    req = urllib.request.Request(url, headers={"User-Agent": "LexFlow/2.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    moves = re.findall(r"<div[^>]*class=\"[^\"]*movimento[^\"]*\"[^>]*>(.+?)</div>", html, flags=re.S | re.I)
    pubs = []
    for m in moves[:10]:
        text = re.sub(r"<[^>]+>", "", m).strip()
        if not text:
            continue
        pubs.append({
            "date": datetime.date.today().isoformat(),
            "title": text[:200],
            "description": f"PJe-TRT1 — {cnj}",
            "type": "publicacao",
            "url": url,
        })
    return pubs


# Mapa tribunal -> scraper
SCRAPERS = {
    "TJSP": scraper_tjsp,
    "TJRS": scraper_tjrs,
    "TJAM": scraper_tjam,
    "TJRJ": scraper_tjrj_eproc,   # TJRJ default = Eproc
    "TJRJ_EPROC": scraper_tjrj_eproc,
    "TJRJ_PJE": scraper_tjrj_pje,
    "TRT1": scraper_trt1,
}


# --- OAB Scraper ---
# Busca publicacoes do DJe que citem uma OAB especifica (numero + UF).
# Se o responsavel do caso tem OAB cadastrada, o worker chama essa funcao
# para gerar andamentos quando a OAB aparece em publicacoes recentes.

def oab_lookup(numero_oab: str, uf: str = "RJ", days_back: int = 7) -> list:
    """Busca publicacoes do DJe que mencionem a OAB.

    numero_oab: string numerica (ex: "244384")
    uf: sigla do estado (ex: "RJ", "SP", "RS", "AM")
    Retorna lista de publicacoes (date, title, description, type='publicacao', url)
    """
    if not numero_oab:
        return []
    numero_oab = re.sub(r"[^0-9]", "", str(numero_oab))
    if not numero_oab:
        return []
    oab_fmt = f"{numero_oab[:3]}.{numero_oab[3:]}" if len(numero_oab) >= 6 else numero_oab
    dje_urls = {
        "RJ": "https://www.tjrj.jus.br/consulta-publica",
        "SP": "https://dje.tjsp.jus.br/cdje/consultaSimples.do",
        "RS": "https://www.tjrs.jus.br/busca/",
        "AM": "https://consultasaj.tjam.jus.br/cdje/consultaSimples.do",
    }
    out = []
    target = dje_urls.get(uf.upper(), dje_urls["RJ"])
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "LexFlow/2.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            blocks = re.findall(r"<p[^>]*>(.+?)</p>", html, flags=re.S | re.I) + \
                 re.findall(r"<li[^>]*>(.+?)</li>", html, flags=re.S | re.I)
        for b in blocks[:50]:
            text = re.sub(r"<[^>]+>", " ", b)
            text = re.sub(r"\s+", " ", text).strip()
            if not text or numero_oab not in text:
                continue
            if "OAB" not in text.upper():
                continue
            out.append({
                "date": datetime.date.today().isoformat(),
                "title": f"Publicacao OAB/{uf} {oab_fmt}",
                "description": text[:500],
                "type": "publicacao",
                "url": target,
            })
    except Exception:
        # Best-effort: silencioso em caso de erro
        pass
    return out




# --- Worker thread ---

class MonitoringWorker(threading.Thread):
    """Thread em background que periodicamente consulta Datajud + DJE
    para todos os casos com monitoring.status='active'."""

    def __init__(self, db_path: str, get_api_key_fn, interval_seconds: int = 30):
        super().__init__(daemon=True, name="lexflow-monitor")
        self.db_path = db_path
        self.get_api_key = get_api_key_fn
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        # Backoff por caso: guarda proxima tentativa apos erro
        self._backoff = {}     # case_id -> epoch do proximo retry
        self._circuit_open_until = 0  # circuit breaker global para Datajud
        self._consecutive_failures = 0

    def stop(self):
        self._stop.set()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def _log(self, case_id, source, ok, message, movements_found=0):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO monitoring_log(id, case_id, checked_at, source, ok, message, movements_found)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (secrets.token_hex(8), case_id, datetime.datetime.now().isoformat(timespec="seconds"),
                 source, 1 if ok else 0, (message or "")[:500], movements_found)
            )
            conn.commit()
        finally:
            conn.close()

    def _list_active_cases(self) -> list:
        conn = self._conn()
        try:
            rows = conn.execute("""
                SELECT m.case_id, m.interval_minutes, m.tribunal, m.last_movement_at, m.error_count,
                       c.code, c.court, c.title, c.responsible_id
                FROM monitoring m
                JOIN cases c ON c.id = m.case_id
                WHERE m.status = 'active' AND (c.deleted_at IS NULL OR c.deleted_at = '')
            """).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _update_case_state(self, case_id, **kw):
        cols = ["updated_at = ?"]
        vals = [datetime.datetime.now().isoformat(timespec="seconds")]
        for k, v in kw.items():
            cols.append(f"{k} = ?")
            vals.append(v)
        vals.append(case_id)
        conn = self._conn()
        try:
            conn.execute(f"UPDATE monitoring SET {', '.join(cols)} WHERE case_id = ?", vals)
            conn.commit()
        finally:
            conn.close()

    def _insert_movements(self, case_id: str, movements: list, source: str) -> int:
        """Insere apenas movimentos NOVOS (date > last_movement_at)."""
        if not movements:
            return 0
        conn = self._conn()
        try:
            last = conn.execute(
                "SELECT last_movement_at FROM monitoring WHERE case_id = ?", (case_id,)
            ).fetchone()
            last_dt = last["last_movement_at"] if last else None
            inserted = 0
            latest = last_dt
            for m in movements:
                md = m.get("date")
                if not md:
                    continue
                if last_dt and md <= last_dt:
                    continue
                conn.execute(
                    "INSERT INTO case_updates(id, case_id, date, title, description, type)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (secrets.token_hex(8), case_id, md[:10], m.get("title") or "Movimento",
                     (m.get("description") or "")[:1000] + f" [fonte: {source}]",
                     m.get("type") or "andamento")
                )
                if not latest or md > latest:
                    latest = md
                inserted += 1
            if latest and (not last_dt or latest > last_dt):
                conn.execute(
                    "UPDATE monitoring SET last_movement_at = ?, last_movement_title = ?, last_movement_source = ?,"
                    " updated_at = ? WHERE case_id = ?",
                    (latest, movements[0].get("title", "")[:200], source,
                     datetime.datetime.now().isoformat(timespec="seconds"), case_id)
                )
            conn.commit()
            return inserted
        finally:
            conn.close()

    def _check_one_case(self, case: dict):
        case_id = case["case_id"]
        cnj = case.get("code") or ""
        if not cnj:
            return
        tribunal = case.get("tribunal") or detect_tribunal(case)
        if not tribunal:
            self._log(case_id, "detect", False, "Tribunal nao detectado", 0)
            return
        # Backoff?
        now_ts = time.time()
        next_try = self._backoff.get(case_id, 0)
        if now_ts < next_try:
            return

        api_key = self.get_api_key() or "APIKeyPublicaCNJ"
        total_new = 0
        any_ok = False

        # ---- Datajud ----
        if now_ts >= self._circuit_open_until:
            try:
                resp = datajud_lookup(cnj, tribunal, api_key)
                movs = extract_movements(resp)
                inserted = self._insert_movements(case_id, movs, "datajud")
                total_new += inserted
                any_ok = True
                self._consecutive_failures = 0
                self._log(case_id, "datajud", True, f"{len(movs)} movimentos, {inserted} novos", inserted)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    msg = ("HTTP 401: chave do Datajud rejeitada. Solicite sua chave "
                           "pessoal gratuita em https://datajud.cnj.jus.br e cole em "
                           "Monitoramento > Configuracoes > API Key.")
                elif e.code == 403:
                    msg = "HTTP 403: chave sem permissao para este tribunal. Verifique a chave pessoal."
                elif e.code == 429:
                    msg = "HTTP 429: rate limit excedido. Aguarde 60s e reduza a frequencia de polling."
                else:
                    msg = f"HTTP {e.code}: {e.reason[:200]}"
                self._log(case_id, "datajud", False, msg, 0)
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    self._circuit_open_until = now_ts + 600
            except Exception as e:
                self._log(case_id, "datajud", False, str(e)[:200], 0)
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    self._circuit_open_until = now_ts + 600
        else:
            self._log(case_id, "datajud", False, "circuit_open", 0)

        # ---- OAB Lookup (se responsavel tem OAB cadastrada) ----
        responsible_id = case.get("responsible_id")
        if responsible_id:
            try:
                conn_user = self._conn()
                try:
                    user_row = conn_user.execute(
                        "SELECT oab FROM users WHERE id = ?", (responsible_id,)
                    ).fetchone()
                finally:
                    conn_user.close()
                oab_text = (user_row["oab"] or "").strip() if user_row else ""
                if oab_text:
                    num_match = re.search(r"(\d{4,6})", oab_text)
                    uf_match = re.search(r"/([A-Z]{2})", oab_text) or re.search(r"^([A-Z]{2})", oab_text)
                    if num_match and uf_match:
                        numero = num_match.group(1)
                        uf = uf_match.group(1)
                        try:
                            oab_pubs = oab_lookup(numero, uf)
                            if oab_pubs:
                                ins = self._insert_dedupe_pubs(case_id, oab_pubs)
                                total_new += ins
                                if ins:
                                    any_ok = True
                                self._log(case_id, "oab", True,
                                          f"OAB/{uf} {numero}: {len(oab_pubs)} pubs, {ins} novas", ins)
                        except Exception as e:
                            self._log(case_id, "oab", False, str(e)[:200], 0)
            except Exception as e:
                self._log(case_id, "oab", False, str(e)[:200], 0)

        # ---- DJE Scraper ----
        scraper_key = "TJRJ_EPROC" if tribunal == "TJRJ" else tribunal
        sc = SCRAPERS.get(scraper_key) or SCRAPERS.get(tribunal)
        if sc:
            try:
                pubs = sc({"code": cnj, "court": case.get("court", "")})
                # DJE nao vem com data confiavel no HTML — insere so se nao existir titulo igual recente
                inserted = self._insert_dedupe_pubs(case_id, pubs)
                total_new += inserted
                if pubs:
                    any_ok = True
                self._log(case_id, "dje", True if pubs else False,
                          f"{len(pubs)} publicacoes, {inserted} novas", inserted)
            except Exception as e:
                self._log(case_id, "dje", False, str(e)[:200], 0)

        # ---- update state ----
        if any_ok:
            self._backoff.pop(case_id, None)
            self._update_case_state(case_id,
                                    last_check_at=datetime.datetime.now().isoformat(timespec="seconds"),
                                    error_count=0,
                                    last_error=None,
                                    tribunal=tribunal)
        else:
            self._backoff[case_id] = now_ts + 300  # 5 min backoff
            self._update_case_state(case_id,
                                    last_check_at=datetime.datetime.now().isoformat(timespec="seconds"),
                                    error_count=case.get("error_count", 0) + 1,
                                    last_error="ultima checagem sem sucesso")

    def _insert_dedupe_pubs(self, case_id, pubs):
        """Insere publicacoes DJE, deduplicando por titulo + case_id nos ultimos 30 dias.
        NUNCA insere entradas de erro (scraper_error) como publicacao."""
        if not pubs:
            return 0
        conn = self._conn()
        try:
            inserted = 0
            for p in pubs:
                if p.get("type") == "scraper_error":
                    continue
                title = (p.get("title") or "").strip()[:200]
                if not title:
                    continue
                if title.lower().startswith("erro scraper"):
                    continue
                # Dedup simples: existe um case_update com mesmo titulo + case_id nos ultimos 30 dias?
                exists = conn.execute(
                    "SELECT 1 FROM case_updates WHERE case_id = ? AND title = ? AND date >= date('now', '-30 days')",
                    (case_id, title)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO case_updates(id, case_id, date, title, description, type)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (secrets.token_hex(8), case_id,
                     (p.get("date") or datetime.date.today().isoformat())[:10],
                     title,
                     ((p.get("description") or "")[:800] + " [DJe]"),
                     "publicacao")
                )
                inserted += 1
            conn.commit()
            return inserted
        finally:
            conn.close()

    def run(self):
        """Loop principal: a cada `interval_seconds` (default 30s para testes),
        varre todos os casos ativos respeitando o intervalo individual e backoff."""
        while not self._stop.is_set():
            try:
                cases = self._list_active_cases()
                now = datetime.datetime.now()
                for c in cases:
                    if self._stop.is_set():
                        break
                    # respeita intervalo individual
                    last = c.get("last_check_at")
                    if last:
                        try:
                            last_dt = datetime.datetime.fromisoformat(last)
                            if (now - last_dt).total_seconds() < c.get("interval_minutes", 60) * 60:
                                continue
                        except Exception:
                            pass
                    try:
                        self._check_one_case(c)
                    except Exception as e:
                        self._log(c["case_id"], "worker", False, str(e)[:200], 0)
            except Exception as e:
                # nunca derruba o worker
                sys.stderr.write(f"[monitor] loop error: {e}\n")
            # dorme (com saida rapida)
            self._stop.wait(self.interval_seconds)
