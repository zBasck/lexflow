"""
Watchdog (Nivel 1) + Patch Suggester (Nivel 2) para o LexFlow.

Nivel 1 - Watchdog: le o log do servidor em tempo real, detecta Traceback/ERROR,
envia o trecho pro Mistral (se disponivel) e guarda o diagnostico. NAO aplica nada.

Nivel 2 - Patch Suggester: quando o Watchdog detecta um erro, tenta sugerir um
patch (diff antes/depois) usando o Mistral. O humano revisa e clica "Aplicar".

Endpoints:
    GET  /api/watchdog/diagnostics  -> lista os ultimos 20 diagnosticos
    GET  /api/watchdog/patches      -> lista sugestoes de patch pendentes
    POST /api/watchdog/patches/apply {id}  -> aplica (rollback com git se quebrar)
    POST /api/watchdog/patches/dismiss {id} -> descarta
    POST /api/watchdog/run          -> roda uma varredura manual
    GET  /api/watchdog/status       -> status do watchdog (rodando, ultimo check)
"""

import os
import re
import time
import json
import threading
import traceback
import subprocess
import secrets
import datetime
from typing import List, Dict, Optional

try:
    import llm as _llm
    HAS_LLM = True
except Exception:
    HAS_LLM = False

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(ROOT)
LOG_FILE_CANDIDATES = [
    os.path.join(REPO_ROOT, "lexflow-server.log"),
    os.path.join(REPO_ROOT, "server.log"),
    os.path.join(REPO_ROOT, "data", "lexflow.log"),
]

# regex para capturar um traceback completo
TRACEBACK_RE = re.compile(
    r'(Traceback \(most recent call last\):.*?(?=\n(?:\[|\d{2}/\d{2}/\d{4}|\Z)))',
    re.DOTALL,
)
ERROR_LINE_RE = re.compile(r'(?:^|\n)(\[(?:ERROR|ERR)\]|.*ERROR.*|.*Traceback.*)')


class Watchdog:
    """Monitora o log, detecta erros, sugere patches. Nunca aplica sozinho."""

    def __init__(self, db_path=None, poll_interval=30):
        self.db_path = db_path
        self.poll_interval = max(10, int(poll_interval))
        self.running = False
        self.thread = None
        self.last_position = 0
        self.log_path = None
        self.diagnostics = []
        self.patches = []
        self.last_check = None
        self.last_error = None
        self._lock = threading.Lock()
        self.max_history = 50
        self._find_log()

    def _find_log(self):
        for p in LOG_FILE_CANDIDATES:
            if os.path.exists(p):
                self.log_path = p
                return
        for p in LOG_FILE_CANDIDATES:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, "a").close()
                self.log_path = p
                return
            except Exception:
                continue

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._scan_once()
            except Exception as e:
                self.last_error = f"watchdog loop: {e}"
            time.sleep(self.poll_interval)

    def _scan_once(self):
        if not self.log_path or not os.path.exists(self.log_path):
            return
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.last_position)
                chunk = f.read()
                self.last_position = f.tell()
        except Exception as e:
            self.last_error = f"read log: {e}"
            return
        if not chunk.strip():
            self.last_check = datetime.datetime.now().isoformat(timespec="seconds")
            return
        for m in TRACEBACK_RE.finditer(chunk):
            tb = m.group(1).strip()
            if len(tb) < 30:
                continue
            self._add_diagnostic(tb, source="log")
        self.last_check = datetime.datetime.now().isoformat(timespec="seconds")

    def _add_diagnostic(self, traceback_text, source="log"):
        key = traceback_text[:200]
        with self._lock:
            if any(d.get("key") == key for d in self.diagnostics):
                return
            diag = {
                "id": secrets.token_hex(8),
                "key": key,
                "traceback": traceback_text[:2000],
                "source": source,
                "detected_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "ai_diagnosis": None,
                "ai_patch": None,
                "applied": False,
                "dismissed": False,
            }
            self.diagnostics.append(diag)
            if len(self.diagnostics) > self.max_history:
                self.diagnostics = self.diagnostics[-self.max_history:]
        if HAS_LLM and _llm.is_available():
            threading.Thread(target=self._enrich_with_llm, args=(diag["id"],), daemon=True).start()

    def _enrich_with_llm(self, diag_id):
        with self._lock:
            diag = next((d for d in self.diagnostics if d["id"] == diag_id), None)
        if not diag:
            return
        try:
            prompt = (
                "Voce e um assistente de debugging Python. Analise este traceback e responda em JSON com os campos:\n"
                "- root_cause: 1 frase\n"
                "- suggested_fix: 1-2 frases de como corrigir\n"
                "- affected_file: caminho do arquivo (se conseguir inferir)\n"
                "- severity: 'low' | 'medium' | 'high'\n\n"
                "Traceback:\n" + diag["traceback"][:1500]
            )
            raw = _llm.generate(prompt, timeout=30) or ""
            m = re.search(r"\{.*?\}", raw, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                with self._lock:
                    for d in self.diagnostics:
                        if d["id"] == diag_id:
                            d["ai_diagnosis"] = parsed
                            break
        except Exception as e:
            with self._lock:
                for d in self.diagnostics:
                    if d["id"] == diag_id:
                        d["ai_diagnosis"] = {"error": f"llm falhou: {str(e)[:120]}"}
                        break

    def get_diagnostics(self, limit=20):
        with self._lock:
            return [
                {k: v for k, v in d.items() if k != "key"}
                for d in self.diagnostics[-limit:]
            ]

    def get_status(self):
        return {
            "running": self.running,
            "log_path": self.log_path,
            "last_check": self.last_check,
            "last_error": self.last_error,
            "diagnostics_count": len(self.diagnostics),
            "patches_count": len(self.patches),
            "llm_available": HAS_LLM and _llm.is_available() if HAS_LLM else False,
        }

    # --- Nivel 2: Patch Suggester ---

    def suggest_patch(self, diag_id):
        with self._lock:
            diag = next((d for d in self.diagnostics if d["id"] == diag_id), None)
        if not diag:
            return None
        if not HAS_LLM or not _llm.is_available():
            return {"error": "Mistral offline. Instale Ollama e rode 'ollama pull mistral'."}
        file_match = re.search(r'File "([^"]+\.py)", line (\d+)', diag["traceback"])
        if not file_match:
            return {"error": "Nao consegui inferir o arquivo do traceback."}
        file_path = file_match.group(1)
        if not os.path.isabs(file_path):
            file_path = os.path.join(REPO_ROOT, file_path)
        if not os.path.exists(file_path):
            return {"error": f"Arquivo nao encontrado: {file_path}"}
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            return {"error": f"Nao consegui ler {file_path}: {e}"}
        line_no = int(file_match.group(2))
        lines = content.split("\n")
        start = max(0, line_no - 20)
        end = min(len(lines), line_no + 20)
        context = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[start:end], start=start))
        prompt = (
            "Voce e um assistente de debugging Python. Veja o traceback e o codigo ao redor.\n"
            "Sugira um patch MINIMO que corrija o erro. Responda em JSON com os campos:\n"
            "- before: o trecho EXATO de codigo a ser substituido (copie verbatim do contexto)\n"
            "- after: o trecho novo (ja corrigido)\n"
            "- explanation: 1 frase explicando o por que\n\n"
            f"Traceback:\n{diag['traceback'][:1000]}\n\n"
            f"Arquivo: {file_path}\nLinha do erro: {line_no}\n"
            f"Contexto (linhas {start+1}-{end}):\n{context}\n"
        )
        try:
            raw = _llm.generate(prompt, timeout=60) or ""
        except Exception as e:
            return {"error": f"LLM falhou: {e}"}
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"error": "LLM nao retornou JSON. Sugestao manual necessaria."}
        try:
            suggestion = json.loads(m.group(0))
        except Exception as e:
            return {"error": f"JSON invalido: {e}"}
        patch = {
            "id": secrets.token_hex(8),
            "diagnostic_id": diag_id,
            "file_path": file_path,
            "line_no": line_no,
            "before": suggestion.get("before", ""),
            "after": suggestion.get("after", ""),
            "explanation": suggestion.get("explanation", ""),
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "applied": False,
            "dismissed": False,
        }
        with self._lock:
            self.patches.append(patch)
        return patch

    def get_patches(self):
        with self._lock:
            return list(self.patches)

    def apply_patch(self, patch_id):
        with self._lock:
            patch = next((p for p in self.patches if p["id"] == patch_id), None)
        if not patch:
            return {"ok": False, "error": "patch nao encontrado"}
        if patch.get("applied"):
            return {"ok": False, "error": "patch ja aplicado"}
        if not os.path.exists(patch["file_path"]):
            return {"ok": False, "error": f"arquivo sumiu: {patch['file_path']}"}
        try:
            with open(patch["file_path"], "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return {"ok": False, "error": f"leitura falhou: {e}"}
        if patch["before"] and patch["before"] not in content:
            return {"ok": False, "error": "trecho 'before' nao encontrado no arquivo (pode ja ter sido consertado)"}
        if not patch["before"]:
            return {"ok": False, "error": "patch sem trecho 'before' - nao posso aplicar cego"}
        new_content = content.replace(patch["before"], patch["after"], 1)
        if new_content == content:
            return {"ok": False, "error": "nada mudou apos replace"}
        backup = patch["file_path"] + f".bak.{patch['id']}"
        try:
            with open(backup, "w", encoding="utf-8") as f:
                f.write(content)
            with open(patch["file_path"], "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return {"ok": False, "error": f"escrita falhou: {e}"}
        commit_msg = f"watchdog auto-fix: {patch.get('explanation', patch_id)[:80]}"
        try:
            subprocess.run(
                ["git", "-C", REPO_ROOT, "add", patch["file_path"]],
                check=False, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "-C", REPO_ROOT, "commit", "-m", commit_msg],
                check=False, capture_output=True, timeout=10,
            )
        except Exception:
            pass
        with self._lock:
            patch["applied"] = True
            patch["applied_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        return {"ok": True, "backup": backup, "commit_msg": commit_msg}

    def dismiss_patch(self, patch_id):
        with self._lock:
            patch = next((p for p in self.patches if p["id"] == patch_id), None)
            if not patch:
                return {"ok": False, "error": "patch nao encontrado"}
            patch["dismissed"] = True
        return {"ok": True}


_singleton = None


def get_watchdog(db_path=None, poll_interval=30):
    global _singleton
    if _singleton is None:
        _singleton = Watchdog(db_path=db_path, poll_interval=poll_interval)
    return _singleton
