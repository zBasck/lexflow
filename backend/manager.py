"""Gerente vivo (Nivel 1+2) - roda em worker thread a cada N minutos.

Nivel 1: regras deterministicas (5 tipos de alertas).
Nivel 2: sintese com Mistral (Ollama) opcional.
"""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import llm

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "lexflow.db")

_sugestoes = []
_last_run = None
_thread = None
_thread_lock = threading.Lock()
_settings = {"enabled": True, "interval_minutes": 60}
_settings_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _now():
    return datetime.utcnow().isoformat()


def get_settings():
    with _settings_lock:
        return dict(_settings)


def set_settings(enabled=None, interval_minutes=None):
    global _settings
    with _settings_lock:
        if enabled is not None:
            _settings["enabled"] = bool(enabled)
        if interval_minutes is not None:
            try:
                im = max(15, min(1440, int(interval_minutes)))
                _settings["interval_minutes"] = im
            except (TypeError, ValueError):
                pass
        return dict(_settings)


def _regra_prazos_vencidos():
    out = []
    today = datetime.utcnow().date().isoformat()
    try:
        c = _conn()
        for row in c.execute(
            "SELECT id, title, due_date, priority, case_id FROM tasks "
            "WHERE due_date < ? AND status != 'concluida' AND (deleted_at IS NULL OR deleted_at='') "
            "ORDER BY due_date ASC LIMIT 5",
            (today,),
        ):
            out.append({
                "tipo": "prazo_vencido",
                "titulo": "Prazo vencido: " + row['title'][:60],
                "descricao": "Tarefa atrasada desde " + str(row['due_date']) + " (prioridade " + str(row['priority']) + ").",
                "prioridade": "alta",
                "action": {"kind": "open_task", "task_id": row["id"]},
            })
        c.close()
    except Exception:
        pass
    return out


def _regra_prazos_proximos():
    out = []
    today = datetime.utcnow().date()
    limite = (today + timedelta(days=3)).isoformat()
    try:
        c = _conn()
        for row in c.execute(
            "SELECT id, title, due_date, priority, case_id FROM tasks "
            "WHERE due_date >= ? AND due_date <= ? AND status != 'concluida' AND (deleted_at IS NULL OR deleted_at='') "
            "ORDER BY due_date ASC LIMIT 5",
            (today.isoformat(), limite),
        ):
            dias = (datetime.fromisoformat(row['due_date']).date() - today).days
            out.append({
                "tipo": "prazo_proximo",
                "titulo": "Prazo proximo: " + row['title'][:60],
                "descricao": "Vence em " + str(row['due_date']) + " (faltam " + str(dias) + " dia(s)).",
                "prioridade": "media",
                "action": {"kind": "open_task", "task_id": row["id"]},
            })
        c.close()
    except Exception:
        pass
    return out


def _regra_financeiro_atrasado():
    out = []
    try:
        c = _conn()
        for row in c.execute(
            "SELECT t.id, t.amount, t.description, t.due_date, c.name AS client_name "
            "FROM transactions t LEFT JOIN clients c ON t.client_id = c.id "
            "WHERE t.type = 'receita' AND t.status = 'pendente' AND t.due_date < date('now','-30 days') "
            "ORDER BY t.due_date ASC LIMIT 5",
        ):
            out.append({
                "tipo": "financeiro_atrasado",
                "titulo": "Receita atrasada: R$ " + ("%.2f" % row['amount']),
                "descricao": (row['client_name'] or 'Cliente') + " - " + row['description'][:50] + " (desde " + str(row['due_date']) + ").",
                "prioridade": "alta",
                "action": {"kind": "open_transaction", "transaction_id": row["id"]},
            })
        c.close()
    except Exception:
        pass
    return out


def _regra_casos_parados():
    out = []
    try:
        c = _conn()
        for row in c.execute(
            "SELECT c.id, c.title, c.code, MAX(u.date) AS last_update "
            "FROM cases c LEFT JOIN case_updates u ON u.case_id = c.id "
            "WHERE c.status = 'ativo' AND (c.deleted_at IS NULL OR c.deleted_at = '') "
            "GROUP BY c.id HAVING last_update IS NULL OR last_update < date('now','-30 days') "
            "ORDER BY last_update ASC LIMIT 5",
        ):
            out.append({
                "tipo": "caso_parado",
                "titulo": "Caso parado ha 30+ dias: " + row['title'][:50],
                "descricao": "CNJ " + str(row['code']) + " sem atualizacoes. Sincronize no Comunica PJE.",
                "prioridade": "media",
                "action": {"kind": "open_case", "case_id": row["id"]},
            })
        c.close()
    except Exception:
        pass
    return out


def _regra_clientes_inativos():
    out = []
    try:
        c = _conn()
        for row in c.execute(
            "SELECT cl.id, cl.name, MAX(c.created_at) AS last_case "
            "FROM clients cl LEFT JOIN cases c ON c.client_id = cl.id "
            "WHERE (cl.deleted_at IS NULL OR cl.deleted_at = '') "
            "GROUP BY cl.id HAVING last_case IS NULL OR last_case < date('now','-180 days') "
            "LIMIT 5",
        ):
            out.append({
                "tipo": "cliente_inativo",
                "titulo": "Cliente sem caso ha 6+ meses: " + row['name'][:50],
                "descricao": "Considere fazer contato e oferecer novos servicos.",
                "prioridade": "baixa",
                "action": {"kind": "open_client", "client_id": row["id"]},
            })
        c.close()
    except Exception:
        pass
    return out


REGRAS = [
    _regra_prazos_vencidos,
    _regra_prazos_proximos,
    _regra_financeiro_atrasado,
    _regra_casos_parados,
    _regra_clientes_inativos,
]


def _prioridade_ord(p):
    return {"alta": 0, "media": 1, "baixa": 2}.get(p, 3)


def gerar_sugestoes():
    global _sugestoes
    bruto = []
    for fn in REGRAS:
        try:
            bruto.extend(fn())
        except Exception:
            pass
    if bruto:
        try:
            status = llm.status()
            if status.get("available"):
                top = sorted(bruto, key=lambda s: _prioridade_ord(s["prioridade"]))[:12]
                prompt = (
                    "Voce e o gerente de um escritorio de advocacia. "
                    "Reorganize e refine os titulos destas sugestoes, mantendo JSON. "
                    "Responda APENAS com JSON valido.\n\n" + json.dumps(top, ensure_ascii=False, indent=2)
                )
                resp = llm.complete(prompt, max_tokens=2000)
                try:
                    resp_clean = resp.strip()
                    if resp_clean.startswith("```"):
                        resp_clean = re.sub(r"^```(?:json)?\n?", "", resp_clean)
                        resp_clean = re.sub(r"\n?```$", "", resp_clean)
                    refined = json.loads(resp_clean)
                    if isinstance(refined, list) and len(refined) == len(top):
                        for orig, new in zip(top, refined):
                            if isinstance(new, dict) and "titulo" in new:
                                orig["titulo"] = new["titulo"]
                                if "descricao" in new:
                                    orig["descricao"] = new["descricao"]
                except Exception:
                    pass
        except Exception:
            pass
    bruto.sort(key=lambda s: _prioridade_ord(s["prioridade"]))
    _sugestoes = bruto
    return _sugestoes


def listar_sugestoes():
    return {
        "sugestoes": _sugestoes,
        "total": len(_sugestoes),
        "last_run": _last_run,
        "settings": get_settings(),
    }


def aplicar(idx):
    global _sugestoes
    if idx < 0 or idx >= len(_sugestoes):
        return {"ok": False, "error": "indice invalido"}
    sug = _sugestoes[idx]
    action = sug.get("action") or {}
    kind = action.get("kind")
    try:
        c = _conn()
        now = _now()
        if kind == "open_task" and action.get("task_id"):
            c.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now, action["task_id"]))
        elif kind == "open_case" and action.get("case_id"):
            c.execute("UPDATE cases SET updated_at=? WHERE id=?", (now, action["case_id"]))
        elif kind == "open_client" and action.get("client_id"):
            c.execute("UPDATE clients SET updated_at=? WHERE id=?", (now, action["client_id"]))
        elif kind == "open_transaction" and action.get("transaction_id"):
            c.execute("UPDATE transactions SET updated_at=? WHERE id=?", (now, action["transaction_id"]))
        c.commit()
        c.close()
        removida = _sugestoes.pop(idx)
        return {"ok": True, "removed": removida}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def dispensar(idx):
    global _sugestoes
    if idx < 0 or idx >= len(_sugestoes):
        return {"ok": False, "error": "indice invalido"}
    removida = _sugestoes.pop(idx)
    return {"ok": True, "removed": removida}


def _worker_loop():
    global _last_run
    while True:
        try:
            cfg = get_settings()
            if cfg["enabled"]:
                gerar_sugestoes()
                _last_run = _now()
        except Exception:
            pass
        wait_total = max(60, int(60 * get_settings().get("interval_minutes", 60)))
        slept = 0
        while slept < wait_total:
            time.sleep(min(30, wait_total - slept))
            slept += 30
            if not get_settings().get("enabled"):
                break


def start():
    global _thread
    with _thread_lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker_loop, daemon=True, name="manager-worker")
        _thread.start()
