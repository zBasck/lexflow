# -*- coding: utf-8 -*-
"""Cliente da API REST do DJE (Diario de Justiça Eletrônico).

Fonte: comunica-api.pje.jus.br (API pública, sem auth obrigatória)
Endpoint: GET /api/v1/comunicacao
Parametros: numeroOab, ufOab, siglaTribunal, pagina, itensPorPagina (5 ou 100),
            dataDisponibilizacaoInicio, dataDisponibilizacaoFim,
            numeroProcesso, nomeAdvogado, nomeParte, orgaoId, meio (E|D), numeroComunicacao

Rate limit: 429 -> aguardar 1 minuto. Headers retornados:
  - x-ratelimit-limit: janela de quantidade de requisicoes
  - x-ratelimit-remaining: requisicoes restantes na janela

Sem filtro de data por padrao: a busca por OAB retorna TODAS as publicacoes
do numero, sem limite temporal. Quando dataInicio/dataFim sao None, nao envia.
"""

import os, re, json, time, urllib.request, urllib.error, urllib.parse

DJE_BASE = os.environ.get("DJE_API_BASE", "https://comunica-api.pje.jus.br")
DJE_TIMEOUT = 20
DJE_USER_AGENT = "LexFlow/1.0 (https://github.com/zBasck/lexflow)"
# Limite diario conservador de retries para 429
DJE_429_MAX_RETRIES = 2
DJE_429_SLEEP_SECONDS = 60


def _request(path, params=None, method="GET", timeout=None):
    """Faz GET/POST na API DJE. Retorna (status_code, body_dict, headers)."""
    url = DJE_BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(
        url, method=method,
        headers={
            "Accept": "application/json",
            "User-Agent": DJE_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or DJE_TIMEOUT) as r:
            body = r.read().decode("utf-8")
            try:
                return r.status, json.loads(body), dict(r.headers)
            except Exception:
                return r.status, {"_raw": body[:1000]}, dict(r.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode("utf-8")
        except Exception: pass
        try:
            return e.code, json.loads(body), dict(e.headers)
        except Exception:
            return e.code, {"_raw": body[:500]}, dict(e.headers)
    except Exception as e:
        return 0, {"_error": str(e)[:200]}, {}


def dje_search_oab(numero_oab, uf, sigla_tribunal=None,
                   data_inicio=None, data_fim=None,
                   itens_por_pagina=100, pagina=1, nome_advogado=None,
                   numero_processo=None):
    """Busca publicacoes do DJE por numero de OAB.

    Args:
        numero_oab: numero da OAB sem pontos (ex: "244384")
        uf: UF da OAB (ex: "RJ")
        sigla_tribunal: sigla completa (ex: "TJRJ"). Se None, derivado da UF.
        data_inicio: data inicial yyyy-mm-dd ou None
        data_fim: data final yyyy-mm-dd ou None
        itens_por_pagina: 5 ou 100 (a API so aceita esses dois)
        pagina: numero da pagina
        nome_advogado: filtro opcional por nome do advogado
        numero_processo: filtro opcional por CNJ (sem pontuacao)

    Returns:
        dict {"ok": bool, "pubs": [...], "total": int, "url": str,
              "tribunal": str, "source": "dje_api", "error": ""}
    """
    numero_oab = re.sub(r"\D", "", str(numero_oab or ""))
    uf = (uf or "").upper().strip()
    if not numero_oab or not uf:
        return {"ok": False, "pubs": [], "total": 0, "url": "", "tribunal": "",
                "source": "dje_api", "error": "numero_oab ou uf invalido"}

    if not sigla_tribunal:
        sigla_tribunal = "TJ" + uf

    # itensPorPagina: 5 ou 100 (validado pela documentacao)
    if itens_por_pagina not in (5, 100):
        itens_por_pagina = 100

    params = {
        "numeroOab": numero_oab,
        "ufOab": uf,
        "siglaTribunal": sigla_tribunal,
        "pagina": pagina,
        "itensPorPagina": itens_por_pagina,
        "dataDisponibilizacaoInicio": data_inicio,
        "dataDisponibilizacaoFim": data_fim,
        "nomeAdvogado": nome_advogado,
        "numeroProcesso": numero_processo,
    }

    last_status, last_body, last_headers = 0, {"_error": "no-attempt"}, {}
    for attempt in range(DJE_429_MAX_RETRIES + 1):
        status, body, headers = _request("/api/v1/comunicacao", params=params)
        last_status, last_body, last_headers = status, body, headers
        # 429: rate limit, aguarda 1 minuto
        if status == 429 and attempt < DJE_429_MAX_RETRIES:
            time.sleep(DJE_429_SLEEP_SECONDS)
            continue
        break

    if last_status == 429:
        return {"ok": False, "pubs": [], "total": 0, "url": _build_url(params),
                "tribunal": sigla_tribunal, "source": "dje_api",
                "error": "DJE: rate limit (HTTP 429) mesmo apos retries"}
    if last_status == 0 or last_status >= 400:
        err = (last_body.get("_error") or last_body.get("message")
               or last_body.get("_raw") or f"HTTP {last_status}")[:200]
        return {"ok": False, "pubs": [], "total": 0, "url": _build_url(params),
                "tribunal": sigla_tribunal, "source": "dje_api", "error": f"DJE: {err}"}

    pubs = _parse_dje_response(last_body, uf=uf, tribunal=sigla_tribunal)
    total = _extract_total(last_body, default=len(pubs))
    return {"ok": True, "pubs": pubs, "total": total, "url": _build_url(params),
            "tribunal": sigla_tribunal, "source": "dje_api", "error": ""}


def dje_search_processo(cnj_digits, uf, sigla_tribunal=None, itens_por_pagina=100, pagina=1):
    """Busca publicacoes do DJE por CNJ."""
    cnj_digits = re.sub(r"\D", "", str(cnj_digits or ""))
    uf = (uf or "").upper().strip()
    if len(cnj_digits) != 20 or not uf:
        return {"ok": False, "pubs": [], "total": 0, "url": "", "tribunal": "",
                "source": "dje_api", "error": "cnj ou uf invalido"}
    if not sigla_tribunal:
        sigla_tribunal = "TJ" + uf
    if itens_por_pagina not in (5, 100):
        itens_por_pagina = 100
    params = {
        "numeroProcesso": cnj_digits,
        "ufOab": uf,
        "siglaTribunal": sigla_tribunal,
        "pagina": pagina,
        "itensPorPagina": itens_por_pagina,
    }
    status, body, headers = _request("/api/v1/comunicacao", params=params)
    if status == 0 or status >= 400:
        err = (body.get("_error") or body.get("message") or body.get("_raw")
               or f"HTTP {status}")[:200]
        return {"ok": False, "pubs": [], "total": 0, "url": _build_url(params),
                "tribunal": sigla_tribunal, "source": "dje_api", "error": f"DJE: {err}"}
    pubs = _parse_dje_response(body, uf=uf, tribunal=sigla_tribunal, cnj_hint=cnj_digits)
    total = _extract_total(body, default=len(pubs))
    return {"ok": True, "pubs": pubs, "total": total, "url": _build_url(params),
            "tribunal": sigla_tribunal, "source": "dje_api", "error": ""}


def _parse_dje_response(body, uf="", tribunal="", cnj_hint=""):
    """Extrai lista de publicacoes do JSON de resposta do DJE.
    A API pode retornar {items: [...]}, {results: [...]}, {content: [...]}, {data: [...]},
    ou uma lista direta. Cada item contem pelo menos numeroProcesso, dataDisponibilizacao,
    texto, tipoComunicacao, meio (D/E).
    """
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        items = (body.get("items") or body.get("results") or body.get("content")
                 or body.get("data") or body.get("comunicacoes") or [])
    else:
        return []
    if not isinstance(items, list):
        return []

    pubs = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        cnj_digits = re.sub(r"\D", "", str(raw.get("numeroProcesso") or raw.get("processo") or ""))
        if not cnj_digits and cnj_hint:
            cnj_digits = cnj_hint
        cnj_fmt = ""
        if len(cnj_digits) == 20:
            cnj_fmt = f"{cnj_digits[0:7]}-{cnj_digits[7:9]}.{cnj_digits[9:13]}.{cnj_digits[13]}.{cnj_digits[14:16]}.{cnj_digits[16:20]}"
        elif cnj_hint:
            cnj_fmt = f"{cnj_hint[0:7]}-{cnj_hint[7:9]}.{cnj_hint[9:13]}.{cnj_hint[13]}.{cnj_hint[14:16]}.{cnj_hint[16:20]}"

        date_iso = (raw.get("dataDisponibilizacao") or raw.get("data") or "")[:10]
        date_br = ""
        if date_iso and "-" in date_iso:
            try:
                y, m, d = date_iso.split("-")
                date_br = f"{d}/{m}/{y}"
            except Exception:
                date_br = date_iso

        tipo = (raw.get("tipoComunicacao") or raw.get("tipo")
                or raw.get("descricaoTipo") or "Publicacao")
        texto = (raw.get("texto") or raw.get("conteudo") or raw.get("descricao")
                 or raw.get("ementa") or "")
        if not texto and raw.get("partes"):
            texto = str(raw.get("partes"))
        titulo = (raw.get("titulo") or raw.get("cabecalho")
                  or f"{tipo} - {cnj_fmt or (raw.get('numeroProcesso') or '')}".strip())

        classe = raw.get("classe") or raw.get("classeProcessual") or ""
        orgao = raw.get("orgaoJulgador") or raw.get("orgao") or raw.get("vara") or ""
        valor = raw.get("valorCausa") or raw.get("valor") or ""

        pub = {
            "cnj": cnj_fmt,
            "date": date_br,
            "type": tipo,
            "title": titulo[:200],
            "description": (texto or titulo)[:1000],
            "raw": (texto or titulo)[:2000],
            "classe": classe,
            "orgao": orgao,
            "valor_causa": str(valor) if valor else "",
        }
        if pub["cnj"] or pub["description"]:
            pubs.append(pub)
    return pubs


def _extract_total(body, default=0):
    """Pega o total da resposta se houver."""
    if not isinstance(body, dict):
        return default
    for k in ("total", "totalElements", "totalItems", "count", "size"):
        v = body.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict) and isinstance(v.get("value"), int):
            return v["value"]
    return default


def _build_url(params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return f"{DJE_BASE}/api/v1/comunicacao?{q}"
