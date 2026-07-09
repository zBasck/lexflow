"""
LexFlow - Subsistema de Monitoramento de Processos (v2.5)
- Unico scraper: Comunica PJE (https://comunica.pje.jus.br/consulta)
- Busca por OAB + UF: retorna publicacoes que mencionam aquela OAB
- Auto-cria caso quando CNJ da publicacao nao esta cadastrado
- Criptografia Fernet da API key (legado, mantida)
- Worker thread com backoff exponencial e circuit breaker
- Log em monitoring_log

Endpoint Comunica PJE:
    GET https://comunica.pje.jus.br/consulta?siglaTribunal={UF}&numeroOab={NUM}&ufOab={UF}
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

try:
    from cryptography.fernet import Fernet  # type: ignore
    HAS_FERNET = True
except Exception:
    HAS_FERNET = False

import base64
import hmac as _hmac

_FERNET_KEY_FILE = ".lexflow.key"
_BACKEND_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _BACKEND_DIR.parent
_KEY_PATH = _PROJECT_ROOT / _FERNET_KEY_FILE


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    k = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 120_000, dklen=32)
    return base64.urlsafe_b64encode(k)


def _get_or_create_master_key() -> bytes:
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
    if HAS_FERNET:
        return Fernet(_get_or_create_master_key())
    return None


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _get_fernet()
    if f is not None:
        return "F:" + f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return "X:" + base64.b64encode(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(ciphertext: str) -> str:
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
    return ciphertext


# --- CNJ regex (formato: NNNNNNN-NN.NNN.N.NN.NNNN) ---

_CNJ_RE = re.compile(r"(\d{7})-(\d{2})\.(\d{4})\.(\d)\.(\d{2})\.(\d{4})")
_CNJ_LOOSE = re.compile(r"(\d{7})\s*-\s*(\d{2})\.(\d{4})\.(\d{1})\.(\d{2})\.(\d{4})")


def normalize_cnj(s: str) -> Optional[str]:
    if not s:
        return None
    s = str(s).strip()
    m = _CNJ_LOOSE.search(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}.{m.group(3)}.{m.group(4)}.{m.group(5)}.{m.group(6)}"
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) == 20:
        return f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}.{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    return None


def uf_to_tribunal(uf: str) -> str:
    """Mapeia UF -> sigla de tribunal usada no Comunica PJE."""
    uf = (uf or "").upper().strip()
    mapping = {
        "AC": "TJAC", "AL": "TJAL", "AP": "TJAP", "AM": "TJAM", "BA": "TJBA",
        "CE": "TJCE", "DF": "TJDFT", "ES": "TJES", "GO": "TJGO", "MA": "TJMA",
        "MT": "TJMT", "MS": "TJMS", "MG": "TJMG", "PA": "TJPA", "PB": "TJPB",
        "PR": "TJPR", "PE": "TJPE", "PI": "TJPI", "RJ": "TJRJ", "RN": "TJRN",
        "RS": "TJRS", "RO": "TJRO", "RR": "TJRR", "SC": "TJSC", "SP": "TJSP",
        "SE": "TJSE", "TO": "TJTO",
    }
    return mapping.get(uf, uf or "TJRJ")




# --- CNJ -> Tribunal (sigla usada no Comunica PJE) ---

_CNJ_TRIBUNAL_BY_JUSTICA = {
    # Justica Estadual (8) - identificado pelo segmento 1 do CNJ (digito 13)
    # O nono digito (segmento 1.0) identifica o tribunal: 8 + codigo TJ
    "8.01": "TJAC", "8.02": "TJAL", "8.03": "TJAP", "8.04": "TJAM", "8.05": "TJBA",
    "8.06": "TJCE", "8.07": "TJDFT", "8.08": "TJES", "8.09": "TJGO", "8.10": "TJMA",
    "8.11": "TJMT", "8.12": "TJMS", "8.13": "TJMG", "8.14": "TJPA", "8.15": "TJPB",
    "8.16": "TJPR", "8.17": "TJPE", "8.18": "TJPI", "8.19": "TJRJ", "8.20": "TJRN",
    "8.21": "TJRS", "8.22": "TJRO", "8.23": "TJRR", "8.24": "TJSC", "8.25": "TJSP",
    "8.26": "TJSE", "8.27": "TJTO",
    # Justica Federal (4)
    "4.01": "TRF1", "4.02": "TRF2", "4.03": "TRF3", "4.04": "TRF4", "4.05": "TRF5",
    # Justica do Trabalho (5)
    "5.01": "TRT1", "5.02": "TRT2", "5.03": "TRT3", "5.04": "TRT4", "5.05": "TRT5",
    "5.06": "TRT6", "5.07": "TRT7", "5.08": "TRT8", "5.09": "TRT9", "5.10": "TRT10",
    "5.11": "TRT11", "5.12": "TRT12", "5.13": "TRT13", "5.14": "TRT14", "5.15": "TRT15",
    "5.16": "TRT16", "5.17": "TRT17", "5.18": "TRT18", "5.19": "TRT19", "5.20": "TRT20",
    "5.21": "TRT21", "5.22": "TRT22", "5.23": "TRT23", "5.24": "TRT24",
    # Justica Eleitoral (6)
    "6.00": "TSE",
    # Justica Militar (7)
    "7.00": "STM",
    # Justica Superior (1, 2, 3)
    "1.00": "STF", "2.00": "STJ", "3.00": "STJ",
}


def cnj_to_tribunal(cnj: str) -> str:
    """Extrai a sigla do tribunal a partir do CNJ (NNNNNNN-NN.NNN.N.NN.NNNN).
    Ex: 0801610-47.2026.8.19.0068 -> 'TJRJ' (segmento 1=8, segmento 1.0=19 -> 8.19).
    """
    n = normalize_cnj(cnj)
    if not n:
        return ""
    # n formatado: NNNNNNN-NN.NNN.N.NN.NNNN
    # Pega o terceiro segmento (justica) e o quarto (tribunal)
    parts = n.split(".")
    if len(parts) >= 4:
        justica = parts[2]  # 8, 4, 5, 6, 7, 1, 2, 3
        tribunal = parts[3]  # 19, 01, 02, ...
        return _CNJ_TRIBUNAL_BY_JUSTICA.get(f"{justica}.{tribunal}", "")
    return ""


# --- Comunica PJE scraper ---

COMUNICA_PJE_URL = "https://comunica.pje.jus.br/consulta"


def _parse_oab(s: str) -> dict:
    """Extrai {numero, uf} de uma string OAB.

    Aceita: "OAB/RJ 244.384", "OAB RJ 244384", "RJ 244384", "244384/RJ",
            "244384-RJ", "OAB244384RJ", "244384".
    """
    if not s:
        return {"numero": None, "uf": None}
    s = str(s).strip()
    uf_match = (
        re.search(r"/([A-Z]{2})\b", s)
        or re.search(r"\b([A-Z]{2})\s+\d", s)
        or re.search(r"\b([A-Z]{2})$", s)
        or re.search(r"^([A-Z]{2})\b", s)
        or re.search(r"\d([A-Z]{2})$", s)  # "OAB244384RJ" -> RJ grudado no numero
    )
    uf = uf_match.group(1) if uf_match else None
    num_match = re.search(r"(\d{1,3}(?:\.\d{3})|\d{4,6})", s)
    numero = None
    if num_match:
        numero = num_match.group(1).replace(".", "")
        if len(numero) < 6:
            numero = numero.zfill(6)
    return {"numero": numero, "uf": uf}


def _parse_pje_html(html: str) -> list:
    """Parser best-effort do HTML retornado pelo Comunica PJE.

    Divide o HTML em blocos (tr/li) e extrai de cada bloco data, tipo e CNJ.
    Dedup por (cnj, date, title).
    """
    if not html:
        return []
    out = []
    seen = set()

    cnj_matches = list(_CNJ_LOOSE.finditer(html))
    if not cnj_matches:
        return []

    for m in cnj_matches:
        cnj_raw = m.group(0)
        cnj_fmt = normalize_cnj(cnj_raw)
        if not cnj_fmt:
            continue
        idx = m.start()
        before = html[:idx]
        start = max(before.rfind("<tr"), before.rfind("<li"))
        if start < 0:
            start = max(0, idx - 500)
        after = html[m.end():]
        end_rel = len(after)
        for closer in ("</tr>", "</li>"):
            i = after.lower().find(closer)
            if 0 <= i < end_rel:
                end_rel = i + len(closer)
        block = html[start:idx + m.end() + end_rel]

        date_match = re.search(r"(\d{2})/(\d{2})/(\d{4})", block)
        if not date_match:
            continue
        date_iso = f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"

        type_match = re.search(
            r"(Intima[cç][aã]o|Cita[cç][aã]o|Notifica[cç][aã]o|Despacho|Decis[aã]o|Senten[cç]a|Audi[eê]ncia|Outros|Atos)",
            block,
            re.I,
        )
        pub_type = type_match.group(1) if type_match else "Publicacao"

        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()
        text = text.replace(cnj_raw, "").strip()
        if len(text) > 400:
            text = text[:400] + "..."

        key = (cnj_fmt, date_iso, pub_type[:30])
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "date": date_iso,
            "title": f"{pub_type} - CNJ {cnj_fmt}",
            "description": text or f"{pub_type} encontrada no Comunica PJE",
            "type": "publicacao",
            "cnj": cnj_fmt,
            "url": COMUNICA_PJE_URL,
        })
    return out



def _scraper_pje_api(numero_oab: str, uf: str, sigla_tribunal: str, timeout: int = 30,
                       numero_processo: str = "") -> list:
    """Tenta a API publica do Comunica PJE (JSON). Retorna [] se falhar.

    numero_oab: numero da OAB (somente digitos) para busca por advogado.
    numero_processo: CNJ (somente digitos) para busca por processo especifico.
    Pelo menos UM dos dois precisa ser fornecido.
    """
    # O Comunica PJE expoe uma API interna em /api/consulta/... (Angular faz GET)
    # Sem auth publica, mas o endpoint aspx pode responder com cookies/JSF
    api_urls = []
    if numero_processo:
        api_urls.append(
            f"https://comunica.pje.jus.br/consulta?siglaTribunal={urllib.parse.quote(sigla_tribunal)}&numeroProcesso={urllib.parse.quote(numero_processo)}&format=json"
        )
        api_urls.append(
            f"https://comunica.pje.jus.br/api/v1/comunicacao?numeroProcesso={urllib.parse.quote(numero_processo)}"
        )
    if numero_oab:
        api_urls.append(
            f"https://comunica.pje.jus.br/api/v1/comunicacao?numeroOab={urllib.parse.quote(numero_oab)}&ufOab={urllib.parse.quote(uf)}"
        )
        api_urls.append(
            f"https://comunica.pje.jus.br/consulta?siglaTribunal={urllib.parse.quote(sigla_tribunal)}&numeroOab={urllib.parse.quote(numero_oab)}&ufOab={urllib.parse.quote(uf)}&format=json"
        )
    for api_url in api_urls:
        try:
            req = urllib.request.Request(
                api_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (LexFlow/2.5)",
                    "Accept": "application/json,text/html",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", errors="ignore")
                if "json" in ct and raw.strip().startswith(("{", "[")):
                    import json as _json
                    data = _json.loads(raw)
                    if isinstance(data, list):
                        out = []
                        for item in data:
                            cnj_raw = item.get("numeroProcesso") or item.get("processo") or item.get("cnj") or ""
                            if not cnj_raw:
                                continue
                            cnj_fmt = normalize_cnj(cnj_raw)
                            if not cnj_fmt:
                                continue
                            out.append({
                                "cnj": cnj_fmt,
                                "date": item.get("dataDisponibilizacao") or item.get("data") or "",
                                "title": item.get("tipoComunicacao") or item.get("tipo") or "Publicacao",
                                "raw": str(item),
                            })
                        if out:
                            return out
        except Exception:
            continue
    return []



def scraper_pje(numero_processo: str = "", numero_oab: str = "", uf: str = "RJ", timeout: int = 30) -> list:
    """Consulta o Comunica PJE e retorna lista de publicacoes.
    
    v2.8: busca por NUMERO DE PROCESSO (CNJ) e sigla do tribunal.
    Aceita tambem busca por OAB (compatibilidade).
    URL: https://comunica.pje.jus.br/consulta?siglaTribunal=TJRJ&numeroProcesso=08016104720268190068
    """
    numero_processo = (numero_processo or "").strip()
    numero_oab = re.sub(r"[^0-9]", "", str(numero_oab or ""))
    uf = (uf or "RJ").upper().strip()
    
    if not numero_processo and not numero_oab:
        return []
    
    # Determinar sigla do tribunal
    if numero_processo:
        sigla_tribunal = cnj_to_tribunal(numero_processo) or uf_to_tribunal(uf)
    else:
        sigla_tribunal = uf_to_tribunal(uf)
    
    qs = f"siglaTribunal={urllib.parse.quote(sigla_tribunal)}"
    if numero_processo:
        # Limpar CNJ: so digitos
        digits = re.sub(r"[^0-9]", "", numero_processo)
        if len(digits) == 20:
            qs += f"&numeroProcesso={urllib.parse.quote(digits)}"
    if numero_oab:
        qs += f"&numeroOab={urllib.parse.quote(numero_oab)}"
        qs += f"&ufOab={urllib.parse.quote(uf)}"
    url = f"{COMUNICA_PJE_URL}?{qs}"
    # Tenta primeiro a API JSON do Comunica PJE (sem dependencia de JS no client).
    # Fallback para o HTML (SPA) se a API nao responder.
    pubs = _scraper_pje_api(numero_oab, uf, sigla_tribunal, timeout=timeout, numero_processo=numero_processo if numero_processo else "")
    if pubs:
        for p in pubs:
            p["url"] = url
            p["tribunal"] = sigla_tribunal
            p["oab"] = f"{uf} {numero_oab}"
        return pubs

    # Fallback: HTML (SPA renderiza no client, entao quase sempre vem vazio)
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (LexFlow/2.5)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return []
    pubs = _parse_pje_html(html)
    for p in pubs:
        p["url"] = url
        p["tribunal"] = sigla_tribunal
        p["oab"] = f"{uf} {numero_oab}"
    return pubs




# --- Extracao de dados do caso a partir do texto da publicacao ---

def _extract_case_info_from_text(text: str) -> dict:
    """Extrai classe, assunto, partes e ultimo andamento de uma publicacao do DJe.
    Retorna dict com chaves: classe, assunto, partes, valor, last_movement.
    """
    info = {"classe": None, "assunto": None, "partes": None, "valor": None, "last_movement": None}
    if not text:
        return info
    t = re.sub(r"\s+", " ", text).strip()
    
    # Classe
    m = re.search(r"Classe\s*[:\-]?\s*([A-Za-zÀ-ÿ\s\-]{3,40}?)(?=\s*(?:Assunto|Parte|Valor|Processo|Advog|Número|$))", t, re.I)
    if m:
        info["classe"] = m.group(1).strip().rstrip(",.;:")[:60]
    
    # Assunto
    m = re.search(r"Assunto\s*[:\-]?\s*([A-Za-zÀ-ÿ\s\-]{3,80}?)(?=\s*(?:Classe|Parte|Valor|Processo|Advog|Número|$))", t, re.I)
    if m:
        info["assunto"] = m.group(1).strip().rstrip(",.;:")[:120]
    
    # Partes (formato comum: "Parte X contra Parte Y" ou "Autor: X / Réu: Y")
    m = re.search(r"(?:Partes?|Polo\s+(?:Ativo|Passivo))\s*[:\-]?\s*(.{20,300}?)(?=\s*(?:Classe|Assunto|Valor|Processo|Vara|Comarca|$))", t, re.I)
    if m:
        info["partes"] = m.group(1).strip().rstrip(",.;:")[:300]
    
    # Valor
    m = re.search(r"Valor\s*(?:da\s*causa)?\s*[:\-]?\s*(R\$\s*[\d.,]+|\d+[\d.,]*)", t, re.I)
    if m:
        info["valor"] = m.group(1).strip()
    
    return info


def scraper_pje_for_case(cnj: str, oab_num: str = "", oab_uf: str = "RJ", timeout: int = 30) -> dict:
    """Busca publicacoes de um processo especifico no Comunica PJE.
    Retorna dict com: pubs (lista), case_info (classe, assunto, partes, etc.).
    """
    out = {"pubs": [], "case_info": {}, "url": "", "tribunal": "", "error": None}
    if not cnj:
        out["error"] = "CNJ vazio"
        return out
    sigla = cnj_to_tribunal(cnj) or uf_to_tribunal(oab_uf)
    out["tribunal"] = sigla
    digits = re.sub(r"[^0-9]", "", cnj)
    if len(digits) != 20:
        out["error"] = f"CNJ invalido: {cnj} (esperado 20 digitos, obtido {len(digits)})"
        return out
    url = f"{COMUNICA_PJE_URL}?siglaTribunal={urllib.parse.quote(sigla)}&numeroProcesso={urllib.parse.quote(digits)}"
    out["url"] = url
    
    # Primeiro tenta a API JSON (passando o CNJ para busca por processo)
    pubs = _scraper_pje_api(oab_num, oab_uf, sigla, timeout=timeout, numero_processo=digits)
    if pubs:
        for p in pubs:
            p["url"] = url
            p["tribunal"] = sigla
            p["oab"] = f"{oab_uf} {oab_num}".strip()
        out["pubs"] = pubs
        return out
    
    # Fallback HTML
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (LexFlow/2.8)", "Accept": "text/html,application/xhtml+xml"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        out["error"] = f"falha HTTP: {str(e)[:120]}"
        return out
    
    pubs = _parse_pje_html(html)
    for p in pubs:
        p["url"] = url
        p["tribunal"] = sigla
        p["oab"] = f"{oab_uf} {oab_num}".strip()
    out["pubs"] = pubs
    
    # Tentar extrair dados do caso do HTML
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    out["case_info"] = _extract_case_info_from_text(text)
    return out


# --- Worker thread ---

class MonitoringWorker(threading.Thread):
    """Thread em background que consulta o Comunica PJE para todas as OABs
    distintas dos casos ativos. Auto-cria caso quando CNJ nao existe."""

    def __init__(self, db_path: str, get_api_key_fn, interval_seconds: int = 30):
        super().__init__(daemon=True, name="lexflow-monitor")
        self.db_path = db_path
        self.get_api_key = get_api_key_fn  # mantido por compatibilidade
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._backoff = {}
        self._circuit_open_until = 0
        self._consecutive_failures = 0

    def stop(self):
        self._stop.set()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def _log(self, target_key, source, ok, message, movements_found=0):
        conn = self._conn()
        try:
            case_id = target_key if not str(target_key).startswith("oab:") else None
            conn.execute(
                "INSERT INTO monitoring_log(id, case_id, checked_at, source, ok, message, movements_found)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(8),
                    case_id,
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    source,
                    1 if ok else 0,
                    (message or "")[:500],
                    movements_found,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _list_active_oabs(self) -> list:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT u.id AS user_id, u.oab, u.name
                FROM monitoring m
                JOIN cases c ON c.id = m.case_id
                JOIN users u ON u.id = c.responsible_id
                WHERE m.status = 'active'
                  AND (c.deleted_at IS NULL OR c.deleted_at = '')
                  AND u.oab IS NOT NULL AND u.oab != ''
                """
            ).fetchall()
            out = []
            for r in rows:
                parsed = _parse_oab(r["oab"] or "")
                # Aceita OABs sem UF parseada (usa RJ como padrao)
                if parsed["numero"]:
                    out.append({
                        "user_id": r["user_id"],
                        "oab_num": parsed["numero"],
                        "oab_uf": parsed["uf"] or "RJ",
                        "oab_text": r["oab"],
                        "user_name": r["name"],
                    })
            return out
        finally:
            conn.close()

    def _find_case_by_cnj(self, cnj: str) -> Optional[str]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id FROM cases WHERE code = ? AND (deleted_at IS NULL OR deleted_at = '') LIMIT 1",
                (cnj,),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def _auto_create_case(self, pub: dict, user_id: str) -> Optional[str]:
        cnj = pub.get("cnj", "")
        if not cnj:
            return None
        conn = self._conn()
        try:
            client_id = None
            cli = conn.execute(
                "SELECT id FROM clients WHERE name = ? AND (deleted_at IS NULL OR deleted_at = '') LIMIT 1",
                ("(Cliente a definir)",),
            ).fetchone()
            if cli:
                client_id = cli["id"]
            else:
                client_id = secrets.token_hex(8)
                conn.execute(
                    "INSERT INTO clients(id, name, type, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        client_id,
                        "(Cliente a definir)",
                        "PF",
                        datetime.datetime.now().isoformat(timespec="seconds"),
                        datetime.datetime.now().isoformat(timespec="seconds"),
                    ),
                )
            case_id = secrets.token_hex(8)
            tribunal_sigla = pub.get("tribunal", "TJRJ")
            title = f"Processo {cnj} (criado por monitoramento)"
            now = datetime.datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO cases(id, code, title, court, status, area, responsible_id, client_id,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id, cnj, title, tribunal_sigla, "ativo", "monitoramento",
                    user_id, client_id, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO monitoring(case_id, status, interval_minutes, created_at, updated_at)"
                " VALUES (?, 'active', 60, ?, ?)",
                (case_id, now, now),
            )
            conn.execute(
                "INSERT INTO case_updates(id, case_id, date, title, description, type)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(8),
                    case_id,
                    pub.get("date", datetime.date.today().isoformat())[:10],
                    (pub.get("title") or "Publicacao")[:200],
                    ((pub.get("description") or "")[:800] + " [fonte: Comunica PJE]"),
                    "publicacao",
                ),
            )
            conn.commit()
            return case_id
        except Exception:
            return None
        finally:
            conn.close()

    def _insert_dedupe_pubs_for_case(self, case_id: str, pubs: list) -> int:
        if not pubs:
            return 0
        conn = self._conn()
        try:
            inserted = 0
            for p in pubs:
                title = (p.get("title") or "").strip()[:200]
                if not title:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM case_updates WHERE case_id = ? AND title = ?"
                    " AND date >= date('now', '-30 days')",
                    (case_id, title),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO case_updates(id, case_id, date, title, description, type)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        secrets.token_hex(8),
                        case_id,
                        (p.get("date") or datetime.date.today().isoformat())[:10],
                        title,
                        ((p.get("description") or "")[:800] + " [fonte: Comunica PJE]"),
                        "publicacao",
                    ),
                )
                inserted += 1
            conn.commit()
            return inserted
        finally:
            conn.close()

    def _check_oab(self, oab_info: dict):
        user_id = oab_info["user_id"]
        oab_num = oab_info["oab_num"]
        oab_uf = oab_info["oab_uf"]
        oab_key = f"oab:{oab_uf} {oab_num}"

        now_ts = time.time()
        next_try = self._backoff.get(oab_key, 0)
        if now_ts < next_try:
            return

        if now_ts < self._circuit_open_until:
            self._log(oab_key, "pje", False, "circuit_open", 0)
            return

        try:
            pubs = scraper_pje(oab_num, oab_uf)
        except Exception as e:
            self._log(oab_key, "pje", False, str(e)[:200], 0)
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._circuit_open_until = now_ts + 600
            return

        if not pubs:
            self._log(oab_key, "pje", True, "0 publicacoes encontradas", 0)
            self._backoff[oab_key] = now_ts + 300
            return

        total_inserted = 0
        new_cases = 0
        for pub in pubs:
            cnj = pub.get("cnj", "")
            if not cnj:
                continue
            case_id = self._find_case_by_cnj(cnj)
            if not case_id:
                case_id = self._auto_create_case(pub, user_id)
                if case_id:
                    new_cases += 1
            if not case_id:
                continue
            ins = self._insert_dedupe_pubs_for_case(case_id, [pub])
            total_inserted += ins

        self._log(
            oab_key, "pje", True,
            f"{len(pubs)} publicacoes, {total_inserted} andamentos novos, {new_cases} casos criados",
            total_inserted,
        )

        self._backoff.pop(oab_key, None)
        self._consecutive_failures = 0
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE monitoring SET last_check_at = ?, error_count = 0, last_error = NULL,"
                " updated_at = ? WHERE case_id IN"
                " (SELECT id FROM cases WHERE responsible_id = ? AND (deleted_at IS NULL OR deleted_at = ''))",
                (
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    user_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def run(self):
        while not self._stop.is_set():
            try:
                oabs = self._list_active_oabs()
                for oab_info in oabs:
                    if self._stop.is_set():
                        break
                    try:
                        self._check_oab(oab_info)
                    except Exception as e:
                        self._log(
                            f"oab:{oab_info.get('oab_uf')} {oab_info.get('oab_num')}",
                            "worker", False, str(e)[:200], 0,
                        )
            except Exception as e:
                sys.stderr.write(f"[monitor] loop error: {e}\n")
            self._stop.wait(self.interval_seconds)
