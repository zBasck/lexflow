# -*- coding: utf-8 -*-
"""Integracao com LLM local (Ollama). Modelos: mistral, llama3, phi3."""
import os, json, urllib.request, urllib.error

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("LEXFLOW_LLM_MODEL", "mistral")

SYSTEM_PT = (
    "Voce e um assistente juridico brasileiro especializado em analise de publicacoes "
    "e processos. Responda em portugues, de forma concisa e objetiva."
)


def _post(path, payload, timeout=120):
    try:
        req = urllib.request.Request(
            OLLAMA_URL + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_available():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def list_models():
    r = _post("/api/tags", {}, timeout=5)
    if not r: return []
    return [m.get("name","") for m in r.get("models",[])]


def generate(prompt, model=None, system=None, max_tokens=512):
    model = model or DEFAULT_MODEL
    payload = {"model": model, "prompt": prompt, "stream": False,
               "options": {"num_predict": max_tokens, "temperature": 0.3}}
    if system: payload["system"] = system
    r = _post("/api/generate", payload, timeout=120)
    if not r: return None
    return r.get("response", "").strip()


def summarize_publication(raw_text):
    prompt = ("Resuma a publicacao juridica abaixo em 1-2 frases curtas, "
              "destacando: o que esta sendo pedido, prazo (se houver) e consequencia.\n\n"
              f"Publicacao:\n{raw_text[:2000]}\n\nResumo:")
    return generate(prompt, system=SYSTEM_PT, max_tokens=200)


def classify_publication(raw_text):
    prompt = ("Analise a publicacao abaixo e responda APENAS em JSON valido, sem markdown:\n"
              '{"tipo": "Intimacao|Citacao|Sentenca|Despacho|Edital|Outros", '
              '"urgencia": "baixa|media|alta|critica", '
              '"prazo_dias": numero_ou_null, '
              '"acao_sugerida": "frase curta do que fazer"}\n\n'
              f"Publicacao:\n{raw_text[:1500]}")
    out = generate(prompt, system=SYSTEM_PT, max_tokens=150)
    if not out: return None
    s = out.find("{"); e = out.rfind("}")
    if s >= 0 and e > s:
        try: return json.loads(out[s:e+1])
        except Exception: return None
    return None


def suggest_next_steps(case_summary, recent_publications):
    pubs = "\n".join(f"- {p.get('title','')}: {p.get('description','')[:200]}"
                       for p in recent_publications[:5])
    prompt = ("Voce e advogado brasileiro. Dado o caso abaixo e suas publicacoes recentes, "
              "sugira 3 proximos passos praticos em lista numerada.\n\n"
              f"Caso: {case_summary}\n\nPublicacoes recentes:\n{pubs}\n\nProximos passos:")
    return generate(prompt, system=SYSTEM_PT, max_tokens=300)


def prioritize_tasks(open_tasks):
    items = "\n".join(f"{i+1}. {t.get('title','')} (prazo: {t.get('due_date','sem prazo')})"
                       for i, t in enumerate(open_tasks))
    prompt = ("Reordene as tarefas abaixo por prioridade juridica sugerida "
              "(mais urgente primeiro). Responda APENAS com a lista numerada na nova ordem.\n\n"
              f"{items}\n\nNova ordem:")
    return generate(prompt, system=SYSTEM_PT, max_tokens=200)


# Aliases para o manager.py
def status():
    return {"available": is_available(), "models": list_models()}

def complete(prompt, model=None, max_tokens=2000):
    return generate(prompt, model=model, max_tokens=max_tokens)
