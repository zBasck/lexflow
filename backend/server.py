"""
LexFlow - Servidor de Gestao Juridica
Backend Python puro (stdlib only) com SQLite.
Roda em http://localhost:8765
"""

import os
import sys
import json
import sqlite3
import hashlib
import secrets
import re
import uuid
import datetime
import inspect
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import monitor as _monitor
    HAS_MONITOR = True
except Exception as _e:
    _monitor = None
    HAS_MONITOR = False
    sys.stderr.write(f"[server] monitor module not loaded: {_e}\n")

# Instância global do worker (iniciada no main)
MONITOR_WORKER = {"instance": None}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "lexflow.db")
FRONTEND_DIR = os.path.join(ROOT, "frontend")
PORT = 8765

os.makedirs(DATA_DIR, exist_ok=True)


# ----------------------------- DATABASE -----------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_pwd(pwd, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2$120000${salt}${h.hex()}"


def check_pwd(pwd, stored):
    try:
        algo, iters, salt, hexhash = stored.split("$")
        h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), int(iters))
        return secrets.compare_digest(h.hex(), hexhash)
    except Exception:
        return False


# ----------------------------- VALIDATORS -----------------------------

def only_digits(s):
    return re.sub(r"\D", "", s or "")


def valid_cpf(cpf):
    cpf = only_digits(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    s = sum(int(cpf[i]) * (10 - i) for i in range(9))
    d1 = (s * 10) % 11
    d1 = 0 if d1 == 10 else d1
    s = sum(int(cpf[i]) * (11 - i) for i in range(10))
    d2 = (s * 10) % 11
    d2 = 0 if d2 == 10 else d2
    return cpf[-2:] == f"{d1}{d2}"


def valid_cnpj(cnpj):
    cnpj = only_digits(cnpj)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    s = sum(int(cnpj[i]) * weights1[i] for i in range(12))
    r = s % 11
    d1 = 0 if r < 2 else 11 - r
    weights2 = [6] + weights1
    s = sum(int(cnpj[i]) * weights2[i] for i in range(13))
    r = s % 11
    d2 = 0 if r < 2 else 11 - r
    return cnpj[-2:] == f"{d1}{d2}"


def valid_cnj(cnj):
    s = re.sub(r"\D", "", cnj or "")
    if len(s) != 20:
        return False
    base = s[:18]
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5, 6, 7, 8, 9, 2, 3]
    acc = 0
    for i, ch in enumerate(reversed(base)):
        acc += int(ch) * weights[i]
    calc = 98 - (acc % 97)
    calc = 0 if calc < 2 else calc
    return s[18:] == f"{calc:02d}"


def valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def valid_doc(tipo, doc):
    if not doc:
        return True
    t = (tipo or "pf").lower()
    if t == "pf":
        return valid_cpf(doc)
    if t == "pj":
        return valid_cnpj(doc)
    return False


def is_socio(user):
    if not user:
        return False
    r = (user.get("role") or "").strip().lower()
    # remove acentuacao comum
    r = r.replace("ç", "c").replace("ã", "a").replace("í", "i").replace("ó", "o").replace("é", "e").replace("á", "a")
    return r in ("socio", "admin", "owner", "partner", "director", "diretor", "sócio", "socia")


def mask_doc(doc):
    s = only_digits(doc)
    if len(s) == 11:
        return f"***.{s[3:6]}.{s[6:9]}-**"
    if len(s) == 14:
        return f"**.***.{s[3:6]}/{s[6:9]}-**"
    return "***"


# ----------------------------- AUDIT -----------------------------

def audit(conn, user_id, action, entity, entity_id, before=None, after=None):
    try:
        conn.execute(
            "INSERT INTO audit_log(id,user_id,action,entity,entity_id,before,after,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                user_id,
                action,
                entity,
                entity_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                datetime.datetime.now().isoformat(timespec="seconds"),
            ),
        )
    except Exception:
        pass


def init_db():
    conn = db()
    cur = conn.cursor()
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'advogado',
            oab TEXT,
            oab_uf TEXT,
            phone TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'pf',
            name TEXT NOT NULL,
            document TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY,
            code TEXT,
            title TEXT NOT NULL,
            client_id TEXT,
            area TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'em_andamento',
            priority TEXT NOT NULL DEFAULT 'media',
            value REAL DEFAULT 0,
            court TEXT,
            opposing_party TEXT,
            description TEXT,
            next_deadline TEXT,
            responsible_id TEXT,
            tags TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL,
            FOREIGN KEY(responsible_id) REFERENCES users(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE IF NOT EXISTS case_updates (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            date TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            type TEXT DEFAULT 'andamento',
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            case_id TEXT,
            responsible_id TEXT,
            priority TEXT DEFAULT 'media',
            status TEXT DEFAULT 'pendente',
            due_date TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
            FOREIGN KEY(responsible_id) REFERENCES users(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT,
            duration INTEGER,
            case_id TEXT,
            location TEXT,
            notes TEXT,
            responsible_id TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
            FOREIGN KEY(responsible_id) REFERENCES users(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            date TEXT NOT NULL,
            due_date TEXT,
            status TEXT DEFAULT 'pendente',
            category TEXT,
            case_id TEXT,
            client_id TEXT,
            payment_method TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
            FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE IF NOT EXISTS case_folders (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            path TEXT NOT NULL,
            label TEXT,
            created_at TEXT,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            case_id TEXT,
            category TEXT,
            type TEXT,
            size TEXT,
            date TEXT NOT NULL,
            responsible_id TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE SET NULL,
            FOREIGN KEY(responsible_id) REFERENCES users(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS monitoring (
            case_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'paused',
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            last_check_at TEXT,
            last_movement_at TEXT,
            last_movement_title TEXT,
            last_movement_source TEXT,
            error_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            tribunal TEXT,
            court_segment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS monitoring_log (
            id TEXT PRIMARY KEY,
            case_id TEXT,
            checked_at TEXT NOT NULL,
            source TEXT NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1,
            message TEXT,
            movements_found INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS monitoring_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            action TEXT NOT NULL,
            entity TEXT NOT NULL,
            entity_id TEXT,
            before TEXT,
            after TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
        )""",
    ]
    for s in statements:
        cur.execute(s)

    # Adicionar coluna deleted_at nas tabelas principais (idempotente)
    for t in ("clients", "cases", "tasks", "events", "transactions", "documents", "case_updates"):
        try:
            cur.execute(f"ALTER TABLE {t} ADD COLUMN deleted_at TEXT")
        except Exception:
            pass  # coluna ja existe

    # Adicionar coluna oab_uf em users (migration idempotente)
    for col_sql in (
        "ALTER TABLE users ADD COLUMN oab_uf TEXT",
    ):
        try:
            cur.execute(col_sql)
        except Exception:
            pass  # coluna ja existe

    cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_deleted ON cases(deleted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_clients_deleted ON clients(deleted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_deleted ON tasks(deleted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_deadline ON cases(next_deadline)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_monitoring_status ON monitoring(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_monitoring_log_case ON monitoring_log(case_id, checked_at DESC)")

    # Migrar role do usuario Patrick/seed para "socio" (caixa baixa) para checagem de permissão
    cur.execute("UPDATE users SET role='socio' WHERE LOWER(REPLACE(REPLACE(REPLACE(role, 'ç','c'), 'ã','a'), 'ó','o')) IN ('socio', 'socia', 'sócio', 'sócia', 'admin', 'owner', 'partner')")

    # Defaults de monitoramento (idempotente)
    for k, v in {
        "monitor.api_key": "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",  # chave publica CNJ (Patrick)
        "monitor.default_interval_minutes": "60",
        "monitor.notify_desktop": "1",
        "monitor.notify_email": "0",
        "monitor.notify_email_address": "",
        "monitor.dje_enabled": "1",
        "monitor.datajud_enabled": "1",
    }.items():
        cur.execute("INSERT OR IGNORE INTO monitoring_settings(key, value) VALUES (?, ?)", (k, v))

    # Migracao: troca o placeholder inutil "APIKeyPublicaCNJ" pela chave real do Patrick
    cur.execute(
        "UPDATE monitoring_settings SET value = ? WHERE key = 'monitor.api_key' AND (value = 'APIKeyPublicaCNJ' OR value = '')",
        ("cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",),
    )

    cur.execute("SELECT COUNT(*) AS c FROM users")
    if cur.fetchone()["c"] == 0:
        seed(conn)
    conn.close()


def seed(conn):
    now = datetime.datetime.now().isoformat(timespec="seconds")

    users = [
        ("helena", "Helena Coutinho", "helena@lexflow.demo", "Sócia", "OAB/SP 123.456", "SP", "(11) 98765-4321"),
        ("rafael", "Rafael Monteiro", "rafael@lexflow.demo", "Advogado Sênior", "OAB/RJ 98.765", "RJ", "(21) 99887-6655"),
        ("camila", "Camila Vasconcelos", "camila@lexflow.demo", "Paralegal", None, None, "(11) 91234-5678"),
    ]
    user_ids = {}
    for slug, name, email, role, oab, oab_uf, phone in users:
        uid = str(uuid.uuid4())
        user_ids[slug] = uid
        conn.execute(
            "INSERT INTO users(id,name,email,password,role,oab,oab_uf,phone,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, name, email, hash_pwd("123456"), role, oab, oab_uf, phone, now),
        )

    clients = [
        ("pf", "Marcos Almeida Pereira", "123.456.789-00", "marcos.almeida@email.com", "(11) 98123-4567", "Rua das Flores, 123 - São Paulo/SP", "Cliente desde 2019"),
        ("pj", "Construtora Horizonte Ltda", "12.345.678/0001-90", "juridico@horizonte.com.br", "(11) 3344-5566", "Av. Paulista, 1000 - São Paulo/SP", "Empresa familiar - cliente corporativo"),
        ("pf", "Juliana Ribeiro Santos", "987.654.321-00", "juliana.rs@email.com", "(21) 99654-3210", "Rua do Catete, 50 - Rio de Janeiro/RJ", "Indicação da Dra. Helena"),
        ("pf", "Roberto Carlos Mendes", "456.789.123-00", "roberto.mendes@email.com", "(31) 98888-7777", "Av. Afonso Pena, 200 - Belo Horizonte/MG", None),
        ("pj", "TechBrasil Soluções SA", "98.765.432/0001-10", "contato@techbrasil.com.br", "(11) 2255-7788", "Av. Faria Lima, 3000 - São Paulo/SP", "Contrato anual de assessoria"),
        ("pf", "Fernanda Souza Lima", "321.654.987-00", "fernanda.lima@email.com", "(47) 99123-4567", "Rua XV de Novembro, 80 - Joinville/SC", None),
        ("pf", "Antonio Ferreira da Silva", "789.123.456-00", "antonio.ferreira@email.com", "(71) 98554-3322", "Largo do Pelourinho, 12 - Salvador/BA", "Caso de família - sigilo total"),
        ("pj", "Indústrias MetalMax SA", "45.678.901/0001-23", "rh@metalmax.com.br", "(19) 3322-1100", "Distrito Industrial - Campinas/SP", "Trabalhista recorrente"),
    ]
    client_ids = []
    for c in clients:
        cid = str(uuid.uuid4())
        client_ids.append(cid)
        conn.execute(
            "INSERT INTO clients(id,type,name,document,email,phone,address,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, *c, now),
        )

    cases = [
        ("0001234-56.2024.8.26.0100", "Almeida vs Construtora Cyrela", 0, "Cível", "em_andamento", "alta", 45000, "1ª Vara Cível de SP", "Construtora Cyrela", "Ação indenizatória por vícios construtivos no imóvel entregue em 2022.", "2026-07-20", "helena", "indenização,imobiliário"),
        ("0009876-12.2023.5.02.0060", "Reclamação Trabalhista MetalMax", 7, "Trabalhista", "em_andamento", "alta", 80000, "60ª Vara do Trabalho de São Paulo", "Indústrias MetalMax SA", "Reclamação de ex-funcionário por horas extras e adicionais não pagos.", "2026-07-25", "rafael", "trabalhista,horas-extras"),
        ("0005555-33.2024.8.19.0001", "Divórcio Litigioso Almeida", 2, "Família", "em_andamento", "media", 25000, "1ª Vara de Família da Capital/RJ", "Juliana Almeida", "Divórcio com partilha de bens e guarda de filhos menores.", "2026-07-30", "helena", "família,divórcio,guarda"),
        ("0007777-88.2024.8.26.0010", "Contrato TechBrasil - Revisão", 4, "Empresarial", "em_andamento", "baixa", 120000, "Foro Central Cível de SP", "TechBrasil Soluções", "Revisão de cláusulas contratuais de contrato de prestação de serviços.", "2026-08-05", "rafael", "contratos,empresarial"),
        ("0003333-22.2024.8.24.0001", "Inventário Mendes", 3, "Sucessões", "em_andamento", "media", 60000, "1ª Vara de Família de BH", None, "Inventário extrajudicial convertido em judicial por desacordo entre herdeiros.", "2026-08-10", "helena", "sucessões,inventário"),
    ]
    case_ids = []
    for cs in cases:
        csid = str(uuid.uuid4())
        case_ids.append(csid)
        conn.execute(
            """INSERT INTO cases(id,code,title,client_id,area,status,priority,value,court,opposing_party,description,next_deadline,responsible_id,tags,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (csid, cs[0], cs[1], client_ids[cs[2]] if cs[2] is not None else None, cs[3], cs[4], cs[5], cs[6], cs[7], cs[8], cs[9], cs[10], user_ids.get(cs[11]), cs[12], now),
        )

    updates = [
        (case_ids[0], "2026-07-01", "Protocolo de inicial", "Petição inicial protocolada com sucesso.", "andamento"),
        (case_ids[0], "2026-07-05", "Contestação apresentada", "Parte ré apresentou contestação no prazo legal.", "andamento"),
        (case_ids[0], "2026-07-08", "Réplica protocolada", "Manifestação sobre documentos novos juntados pela ré.", "andamento"),
        (case_ids[1], "2026-06-20", "Audiência inicial designada", "Audiência una designada para o dia 25/07/2026.", "audiencia"),
        (case_ids[1], "2026-07-02", "Juntada de documentos", "Cliente trouxe os cartões de ponto e holerites para análise.", "andamento"),
        (case_ids[2], "2026-06-15", "Reunião com cliente", "Conversa sobre estratégia do processo e mediação prévia.", "andamento"),
        (case_ids[3], "2026-07-03", "Análise contratual", "Concluída primeira leitura do contrato. Pontos críticos identificados.", "andamento"),
        (case_ids[4], "2026-07-06", "Despacho judicial", "Juiz determinou manifestação do MP no prazo de 15 dias.", "andamento"),
    ]
    for u in updates:
        conn.execute(
            "INSERT INTO case_updates(id,case_id,date,title,description,type) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), *u),
        )

    tasks = [
        ("Elaborar réplica - caso Almeida", "Preparar manifestação sobre contestação e documentos novos.", case_ids[0], "helena", "alta", "pendente", "2026-07-12"),
        ("Reunião com Marcos Almeida", "Alinhar estratégia após réplica protocolada.", case_ids[0], "helena", "media", "pendente", "2026-07-15"),
        ("Preparar audiência MetalMax", "Compilar documentos e preparar sustentação oral para audiência una.", case_ids[1], "rafael", "alta", "pendente", "2026-07-23"),
        ("Análise de holerites", "Verificar cálculo de horas extras e adicional noturno.", case_ids[1], "rafael", "media", "concluida", "2026-07-05"),
        ("Petição de inventário complementar", "Juntar documentos faltantes solicitados pelo cartório.", case_ids[4], "helena", "media", "pendente", "2026-07-20"),
        ("Revisão de cláusula 8.3", "Cláusula de não-concorrência pode ser abusiva - análise detalhada.", case_ids[3], "rafael", "alta", "em_andamento", "2026-07-10"),
        ("Atualizar cliente TechBrasil", "Agendar reunião para apresentar parecer inicial.", case_ids[3], "camila", "baixa", "pendente", "2026-07-18"),
        ("Renovar procuração Mendes", "Procuração ad judicia et extra vence em 30 dias.", case_ids[4], "camila", "media", "pendente", "2026-07-25"),
        ("Organizar documentos divórcio", "Separar toda documentação patrimonial do casal.", case_ids[2], "camila", "media", "em_andamento", "2026-07-14"),
        ("Emitir nota fiscal - honorários", "Honorários do mês de junho a receber.", None, "camila", "baixa", "pendente", "2026-07-08"),
    ]
    for t in tasks:
        # t = (title, description, case_id, resp_slug, priority, status, due_date)
        conn.execute(
            """INSERT INTO tasks(id,title,description,case_id,responsible_id,priority,status,due_date,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), t[0], t[1], t[2], user_ids.get(t[3]), t[4], t[5], t[6], now),
        )

    events = [
        ("Audiência Una - MetalMax", "audiencia", "2026-07-25", "09:30", 120, case_ids[1], "60ª Vara do Trabalho - SP", "Levar todos os documentos originais e cópia do contrato.", "rafael"),
        ("Reunião com cliente - Marcos Almeida", "reuniao", "2026-07-15", "14:00", 60, case_ids[0], "Escritório - Sala 2", None, "helena"),
        ("Prazo: Réplica caso Cyrela", "prazo", "2026-07-12", "23:59", 0, case_ids[0], None, "Prazo fatal - 15 dias úteis.", "helena"),
        ("Sessão de mediação - Divórcio Almeida", "audiencia", "2026-07-30", "10:00", 90, case_ids[2], "CEJUSC Rio de Janeiro", "Mediação conduzida pelo Dr. Paulo Siqueira.", "helena"),
        ("Reunião TechBrasil - parecer inicial", "reuniao", "2026-07-18", "15:30", 90, case_ids[3], "Sala de reuniões - escritório", None, "rafael"),
        ("Prazo: Manifestação MP - Inventário", "prazo", "2026-07-21", "23:59", 0, case_ids[4], None, "Cuidar para não cair em final de semana.", "helena"),
        ("Audiência instrução - Trabalhista", "audiencia", "2026-08-10", "13:00", 180, case_ids[1], "Vara do Trabalho de SP", "Trazer testemunha se houver.", "rafael"),
    ]
    for e in events:
        # e = (title, type, date, time, duration, case_id, location, notes, resp_slug)
        conn.execute(
            """INSERT INTO events(id,title,type,date,time,duration,case_id,location,notes,responsible_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7], user_ids.get(e[8]), now),
        )

    today = datetime.date.today()
    def dstr(offset):
        return (today + datetime.timedelta(days=offset)).isoformat()

    transactions = [
        ("receita", "Honorários iniciais - Marcos Almeida", 5000, dstr(-90), None, "pago", "Honorários Advocatícios", case_ids[0], client_ids[0], "Transferência", now),
        ("receita", "Parcela 1 - Honorários TechBrasil", 12000, dstr(-60), None, "pago", "Honorários Advocatícios", case_ids[3], client_ids[4], "Boleto", now),
        ("receita", "Honorários - Divórcio Almeida", 8000, dstr(-30), None, "pago", "Honorários Advocatícios", case_ids[2], client_ids[2], "PIX", now),
        ("receita", "Parcela 2 - TechBrasil", 12000, dstr(-15), None, "pago", "Honorários Advocatícios", case_ids[3], client_ids[4], "Boleto", now),
        ("receita", "Honorários - Inventário Mendes", 6000, dstr(-5), None, "pago", "Honorários Advocatícios", case_ids[4], client_ids[3], "Transferência", now),
        ("receita", "Consultoria Construtora Horizonte", 3500, dstr(7), None, "pendente", "Consultoria", None, client_ids[1], "Boleto", now),
        ("receita", "Parcela 3 - TechBrasil", 12000, dstr(15), None, "pendente", "Honorários Advocatícios", case_ids[3], client_ids[4], "Boleto", now),
        ("receita", "Honorários adicionais - Cyrela", 8000, dstr(30), None, "pendente", "Honorários Advocatícios", case_ids[0], client_ids[0], "Boleto", now),
        ("despesa", "Aluguel do escritório", 8500, dstr(-25), None, "pago", "Aluguel", None, None, "Transferência", now),
        ("despesa", "Salários - equipe", 32000, dstr(-20), None, "pago", "Folha", None, None, "Transferência", now),
        ("despesa", "Softwares jurídicos (Clio, Astrea)", 1200, dstr(-10), None, "pago", "Software", None, None, "Cartão de crédito", now),
        ("despesa", "Material de escritório", 450, dstr(-8), None, "pago", "Material", None, None, "Cartão de crédito", now),
        ("despesa", "Certidões e custas processuais", 380, dstr(-2), None, "pago", "Custas", None, None, "Boleto", now),
        ("despesa", "Aluguel do escritório", 8500, dstr(5), None, "pendente", "Aluguel", None, None, "Transferência", now),
        ("despesa", "Salários - equipe", 32000, dstr(10), None, "pendente", "Folha", None, None, "Transferência", now),
    ]
    for t in transactions:
        conn.execute(
            """INSERT INTO transactions(id,type,description,amount,date,due_date,status,category,case_id,client_id,payment_method,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), *t),
        )

    documents = [
        ("Procuração - Marcos Almeida", case_ids[0], "Procuração", "PDF", "245 KB", dstr(-100), "helena", "Procuração ad judicia et extra assinada com firma reconhecida."),
        ("Contrato Honorários - Almeida", case_ids[0], "Contrato", "PDF", "180 KB", dstr(-95), "helena", "Contrato de prestação de serviços advocatícios."),
        ("Petição Inicial - Cyrela", case_ids[0], "Petição", "PDF", "1.2 MB", dstr(-90), "helena", "Petição inicial protocolada em 01/07/2026."),
        ("Contestação - Cyrela", case_ids[0], "Petição", "PDF", "980 KB", dstr(-25), "helena", "Contestação apresentada pela construtora."),
        ("Holerites - Funcionário MetalMax", case_ids[1], "Documento", "PDF", "3.4 MB", dstr(-30), "rafael", "12 holerites do período de vigência do contrato."),
        ("Cartões de Ponto", case_ids[1], "Documento", "PDF", "5.1 MB", dstr(-28), "rafael", "Cartões de ponto dos últimos 2 anos."),
        ("Contrato de Trabalho MetalMax", case_ids[1], "Contrato", "PDF", "420 KB", dstr(-35), "rafael", None),
        ("Certidão de Casamento - Almeida", case_ids[2], "Documento", "PDF", "90 KB", dstr(-60), "helena", None),
        ("Plano de Partilha proposto", case_ids[2], "Parecer", "DOCX", "320 KB", dstr(-10), "helena", "Primeira versão do plano de partilha de bens."),
        ("Contrato TechBrasil - Original", case_ids[3], "Contrato", "PDF", "780 KB", dstr(-70), "rafael", "Contrato original assinado entre as partes."),
        ("Parecer inicial - TechBrasil", case_ids[3], "Parecer", "DOCX", "560 KB", dstr(-3), "rafael", "Parecer técnico sobre cláusulas abusivas."),
        ("Certidão de óbito - Sr. Mendes", case_ids[4], "Documento", "PDF", "120 KB", dstr(-50), "helena", None),
        ("Plano de inventário - Mendes", case_ids[4], "Parecer", "DOCX", "410 KB", dstr(-15), "helena", "Versão para discussão com herdeiros."),
    ]
    for d in documents:
        # d = (title, case_id, category, type, size, date, resp_slug, notes)
        conn.execute(
            """INSERT INTO documents(id,title,case_id,category,type,size,date,responsible_id,notes,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), d[0], d[1], d[2], d[3], d[4], d[5], user_ids.get(d[6]), d[7], now),
        )

    conn.commit()


# ----------------------------- HELPERS -----------------------------

def json_response(handler, status, data):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def read_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def require_auth(handler):
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    conn = db()
    row = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        conn.close()
        return None
    user = conn.execute("SELECT id,name,email,role,oab,phone FROM users WHERE id=?", (row["user_id"],)).fetchone()
    conn.close()
    if not user:
        return None
    return dict(user)


def serialize_row(r):
    if r is None:
        return None
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, (datetime.date, datetime.datetime)):
            d[k] = v.isoformat()
    return d


# ----------------------------- ROUTES -----------------------------

ROUTES = {}

def route(method, path, fn):
    ROUTES[(method, path)] = fn


def not_found(handler):
    json_response(handler, 404, {"error": "not found"})


def dispatch(handler, method, path):
    # Try static match first
    fn = ROUTES.get((method, path))
    if fn:
        try:
            nparams = len(inspect.signature(fn).parameters)
        except Exception:
            nparams = 1
        if nparams >= 2:
            if method in ("POST", "PUT", "PATCH"):
                body = read_body(handler)
            else:
                body = None
            return fn(handler, body)
        return fn(handler)

    # Pattern routes for /api/{resource}/<id>
    m = re.match(r"^/api/(\w+)/([\w\-]+)$", path)
    if m:
        resource = m.group(1)
        rid = m.group(2)
        fn = ROUTES.get((method, f"/api/{resource}/{{id}}"))
        if fn:
            return fn(handler, rid)

    # Pattern for /api/trash/{table}/{id}/restore (4 segmentos)
    m = re.match(r"^/api/trash/(\w+)/([\w\-]+)/restore$", path)
    if m and method == "POST":
        fn = ROUTES.get(("POST", "/api/trash/{table}/{id}/restore"))
        if fn:
            return fn(handler, m.group(1), m.group(2))

    # Pattern para /api/trash/{table}/{id} com DELETE
    m = re.match(r"^/api/trash/(\w+)/([\w\-]+)$", path)
    if m and method == "DELETE":
        fn = ROUTES.get(("DELETE", "/api/trash/{table}/{id}"))
        if fn:
            return fn(handler, m.group(1), m.group(2))

    # Pattern para /api/cases/{id}/monitor/run (POST) e /api/cases/{id}/monitor (POST)
    m = re.match(r"^/api/cases/([\w\-]+)/monitor/run$", path)
    if m and method == "POST":
        fn = ROUTES.get(("POST", "/api/cases/{id}/monitor/run"))
        if fn:
            return fn(handler, m.group(1), None)

    m = re.match(r"^/api/cases/([\w\-]+)/monitor$", path)
    if m and method == "POST":
        fn = ROUTES.get(("POST", "/api/cases/{id}/monitor"))
        if fn:
            body = read_body(handler)
            return fn(handler, m.group(1), body)

    # Pattern generico /api/{resource}/{id}/{action} (3 segmentos)
    m = re.match(r"^/api/(\w+)/([\w\-]+)/(\w+)$", path)
    if m:
        resource, rid, action = m.group(1), m.group(2), m.group(3)
        fn = ROUTES.get((method, f"/api/{resource}/{{id}}/{action}"))
        if fn:
            # Aridade adaptativa: alguns handlers (ex.: case_updates_create) so aceitam (handler, rid)
            # IMPORTANTE: o body do rfile so pode ser lido UMA vez. Se o handler esperar 2 args
            # (handler, rid), ele le o body internamente. Se esperar 3, lemos aqui e passamos.
            try:
                nparams = len(inspect.signature(fn).parameters)
            except Exception:
                nparams = 2
            if nparams >= 3:
                body = read_body(handler) if method in ("POST", "PUT", "PATCH") else None
                return fn(handler, rid, body)
            elif nparams == 2:
                # NAO le o body aqui - deixa o handler fazer isso
                return fn(handler, rid)
            else:
                return fn(handler)

    not_found(handler)


# ---- AUTH ----

def auth_register(handler):
    body = read_body(handler)
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    role = (body.get("role") or "Advogado").strip()
    oab = (body.get("oab") or "").strip() or None
    phone = (body.get("phone") or "").strip() or None

    if not name or not email or len(password) < 4:
        return json_response(handler, 400, {"error": "Dados invalidos. Verifique nome, email e senha (min 4 caracteres)."})
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return json_response(handler, 400, {"error": "Email invalido."})

    conn = db()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if exists:
        conn.close()
        return json_response(handler, 409, {"error": "Email ja cadastrado."})

    uid = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users(id,name,email,password,role,oab,oab_uf,phone,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, name, email, hash_pwd(password), role, oab, oab_uf, phone, now),
    )
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES (?,?,?)", (token, uid, now))
    user = conn.execute("SELECT id,name,email,role,oab,phone FROM users WHERE id=?", (uid,)).fetchone()
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"token": token, "user": dict(user)})


def auth_login(handler):
    body = read_body(handler)
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or not password:
        return json_response(handler, 400, {"error": "Email e senha sao obrigatorios."})

    conn = db()
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_pwd(password, row["password"]):
        conn.close()
        return json_response(handler, 401, {"error": "Email ou senha incorretos."})
    token = secrets.token_urlsafe(32)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES (?,?,?)", (token, row["id"], now))
    user = {k: row[k] for k in ("id", "name", "email", "role", "oab", "phone")}
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"token": token, "user": user})


def auth_logout(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    return json_response(handler, 200, {"ok": True})


def auth_me(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    return json_response(handler, 200, {"user": user})


route("POST", "/api/auth/register", auth_register)
route("POST", "/api/auth/login", auth_login)
route("POST", "/api/auth/logout", auth_logout)
route("GET", "/api/auth/me", auth_me)


# ---- GENERIC CRUD ----

def make_list(table, order_by="created_at DESC"):
    def fn(handler):
        if not require_auth(handler):
            return json_response(handler, 401, {"error": "Nao autenticado."})
        # Suporte a ?include_deleted=1 (apenas para endpoints que o chamador opt-in)
        # e ?q= para busca simples em tabelas com coluna name
        parsed = urlparse(handler.path)
        qs = parse_qs(parsed.query)
        include_deleted = qs.get("include_deleted", ["0"])[0] == "1"
        q = (qs.get("q", [""])[0] or "").strip()
        try:
            limit = min(int(qs.get("limit", ["500"])[0]), 1000)
        except Exception:
            limit = 500
        try:
            offset = max(int(qs.get("offset", ["0"])[0]), 0)
        except Exception:
            offset = 0
        conn = db()
        sql = f"SELECT * FROM {table}"
        conds = []
        params = []
        if not include_deleted:
            try:
                conds.append("(deleted_at IS NULL OR deleted_at='')")
            except Exception:
                pass
        if q and table in ("clients", "cases", "tasks", "events", "transactions", "documents", "users"):
            like = f"%{q}%"
            if table == "clients":
                conds.append("(name LIKE ? OR document LIKE ? OR email LIKE ?)")
                params += [like, like, like]
            elif table == "cases":
                conds.append("(title LIKE ? OR code LIKE ? OR opposing_party LIKE ? OR tags LIKE ?)")
                params += [like, like, like, like]
            elif table == "tasks":
                conds.append("(title LIKE ? OR description LIKE ?)")
                params += [like, like]
            elif table == "events":
                conds.append("(title LIKE ? OR location LIKE ?)")
                params += [like, like]
            elif table == "transactions":
                conds.append("(description LIKE ? OR category LIKE ?)")
                params += [like, like]
            elif table == "documents":
                conds.append("(title LIKE ? OR notes LIKE ?)")
                params += [like, like]
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY {order_by} LIMIT {limit} OFFSET {offset}"
        rows = conn.execute(sql, params).fetchall()
        data = [serialize_row(r) for r in rows]
        # mascarar documento de clientes
        if table == "clients":
            for d in data:
                if d.get("document"):
                    d["document_masked"] = mask_doc(d["document"])
        conn.close()
        return json_response(handler, 200, data)
    return fn

def make_get(table):
    def fn(handler, rid):
        if not require_auth(handler):
            return json_response(handler, 401, {"error": "Nao autenticado."})
        conn = db()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        conn.close()
        if not row:
            return json_response(handler, 404, {"error": "Nao encontrado."})
        out = serialize_row(row)
        if table == "clients" and out.get("document"):
            out["document_masked"] = mask_doc(out["document"])
        return json_response(handler, 200, out)
    return fn

def make_create(table, fields):
    def fn(handler):
        user = require_auth(handler)
        if not user:
            return json_response(handler, 401, {"error": "Nao autenticado."})
        body = read_body(handler)
        # Validações especificas por tabela
        if table == "clients":
            if not (body.get("name") or "").strip():
                return json_response(handler, 400, {"error": "Nome do cliente é obrigatório."})
            if not valid_doc(body.get("type"), body.get("document")):
                return json_response(handler, 400, {"error": "Documento inválido. Verifique CPF/CNPJ."})
            if body.get("email") and not valid_email(body["email"]):
                return json_response(handler, 400, {"error": "E-mail inválido."})
        elif table == "cases":
            if not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título do caso é obrigatório."})
            if not (body.get("area") or "").strip():
                return json_response(handler, 400, {"error": "Área jurídica é obrigatória."})
            if body.get("code") and not valid_cnj(body["code"]):
                # Não bloqueia: aceita formato livre, mas avisa (CNJ é opcional para casos novos)
                pass
        elif table == "tasks":
            if not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título da tarefa é obrigatório."})
        elif table == "events":
            if not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título do evento é obrigatório."})
            if not (body.get("date") or "").strip():
                return json_response(handler, 400, {"error": "Data do evento é obrigatória."})
        elif table == "transactions":
            try:
                amt = float(body.get("amount") or 0)
                if amt == 0:
                    return json_response(handler, 400, {"error": "Valor não pode ser zero."})
            except Exception:
                return json_response(handler, 400, {"error": "Valor inválido."})
        elif table == "documents":
            if not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título do documento é obrigatório."})
        rid = str(uuid.uuid4())
        now = datetime.datetime.now().isoformat(timespec="seconds")
        values = [rid]
        cols = ["id"]
        for f in fields:
            cols.append(f)
            v = body.get(f)
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            values.append(v)
        cols.append("created_at")
        values.append(now)
        conn = db()
        try:
            conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['?']*len(values))})", values)
            audit(conn, user["id"], "create", table, rid, after=body)
            conn.commit()
        except Exception as e:
            conn.close()
            return json_response(handler, 400, {"error": str(e)})
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        out = serialize_row(row)
        if table == "clients" and out.get("document"):
            out["document_masked"] = mask_doc(out["document"])
        conn.close()
        return json_response(handler, 200, out)
    return fn

def make_update(table, fields):
    def fn(handler, rid):
        user = require_auth(handler)
        if not user:
            return json_response(handler, 401, {"error": "Nao autenticado."})
        body = read_body(handler)
        # Validações por tabela
        if table == "clients":
            if "name" in body and not (body.get("name") or "").strip():
                return json_response(handler, 400, {"error": "Nome do cliente é obrigatório."})
            if "document" in body and body.get("document") and not valid_doc(body.get("type", "pf"), body["document"]):
                return json_response(handler, 400, {"error": "Documento inválido. Verifique CPF/CNPJ."})
            if body.get("email") and not valid_email(body["email"]):
                return json_response(handler, 400, {"error": "E-mail inválido."})
        elif table == "cases":
            if "title" in body and not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título do caso é obrigatório."})
        elif table == "tasks":
            if "title" in body and not (body.get("title") or "").strip():
                return json_response(handler, 400, {"error": "Título da tarefa é obrigatório."})
        sets = []
        values = []
        for f in fields:
            if f in body:
                v = body[f]
                if isinstance(v, (list, dict)):
                    v = json.dumps(v, ensure_ascii=False)
                sets.append(f"{f}=?")
                values.append(v)
        if not sets:
            return json_response(handler, 400, {"error": "Nada para atualizar."})
        values.append(rid)
        conn = db()
        # Capturar estado anterior para auditoria
        before_row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        conn.execute(f"UPDATE {table} SET {','.join(sets)} WHERE id=?", values)
        audit(conn, user["id"], "update", table, rid, before=dict(before_row) if before_row else None, after=body)
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        out = serialize_row(row)
        if table == "clients" and out.get("document"):
            out["document_masked"] = mask_doc(out["document"])
        conn.close()
        if not row:
            return json_response(handler, 404, {"error": "Nao encontrado."})
        return json_response(handler, 200, out)
    return fn

def make_delete(table):
    SOFT_TABLES = ("clients", "cases", "tasks", "events", "transactions", "documents", "case_updates")
    def fn(handler, rid):
        user = require_auth(handler)
        if not user:
            return json_response(handler, 401, {"error": "Nao autenticado."})
        conn = db()
        before_row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        if not before_row:
            conn.close()
            return json_response(handler, 404, {"error": "Nao encontrado."})
        now = datetime.datetime.now().isoformat(timespec="seconds")
        if table in SOFT_TABLES:
            conn.execute(f"UPDATE {table} SET deleted_at=? WHERE id=?", (now, rid))
            audit(conn, user["id"], "soft_delete", table, rid, before=dict(before_row))
        else:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            audit(conn, user["id"], "delete", table, rid, before=dict(before_row))
        conn.commit()
        conn.close()
        return json_response(handler, 200, {"ok": True, "soft": table in SOFT_TABLES})
    return fn


# Register CRUD routes
route("GET",    "/api/clients",  make_list("clients", "name ASC"))
route("POST",   "/api/clients",  make_create("clients", ["type","name","document","email","phone","address","notes"]))
route("GET",    "/api/clients/{id}", make_get("clients"))
route("PUT",    "/api/clients/{id}", make_update("clients", ["type","name","document","email","phone","address","notes"]))
route("DELETE", "/api/clients/{id}", make_delete("clients"))

CASE_FIELDS = ["code","title","client_id","area","status","priority","value","court","opposing_party","description","next_deadline","responsible_id","tags"]
route("GET",    "/api/cases", make_list("cases", "created_at DESC"))
route("POST",   "/api/cases", make_create("cases", CASE_FIELDS))
route("GET",    "/api/cases/{id}", make_get("cases"))
route("PUT",    "/api/cases/{id}", make_update("cases", CASE_FIELDS))
route("DELETE", "/api/cases/{id}", make_delete("cases"))

route("GET",    "/api/tasks", make_list("tasks", "due_date ASC"))
route("POST",   "/api/tasks", make_create("tasks", ["title","description","case_id","responsible_id","priority","status","due_date"]))
route("PUT",    "/api/tasks/{id}", make_update("tasks", ["title","description","case_id","responsible_id","priority","status","due_date"]))
route("DELETE", "/api/tasks/{id}", make_delete("tasks"))

route("GET",    "/api/events", make_list("events", "date ASC, time ASC"))
route("POST",   "/api/events", make_create("events", ["title","type","date","time","duration","case_id","location","notes","responsible_id"]))
route("PUT",    "/api/events/{id}", make_update("events", ["title","type","date","time","duration","case_id","location","notes","responsible_id"]))
route("DELETE", "/api/events/{id}", make_delete("events"))

route("GET",    "/api/transactions", make_list("transactions", "date DESC"))
route("POST",   "/api/transactions", make_create("transactions", ["type","description","amount","date","due_date","status","category","case_id","client_id","payment_method"]))
route("PUT",    "/api/transactions/{id}", make_update("transactions", ["type","description","amount","date","due_date","status","category","case_id","client_id","payment_method"]))
route("DELETE", "/api/transactions/{id}", make_delete("transactions"))

route("GET",    "/api/documents", make_list("documents", "date DESC"))
route("POST",   "/api/documents", make_create("documents", ["title","case_id","category","type","size","date","responsible_id","notes"]))
route("PUT",    "/api/documents/{id}", make_update("documents", ["title","case_id","category","type","size","date","responsible_id","notes"]))
route("DELETE", "/api/documents/{id}", make_delete("documents"))


# ---- USERS / TEAM ----

def users_list(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    rows = conn.execute("SELECT id,name,email,role,oab,phone,created_at FROM users ORDER BY name").fetchall()
    data = [serialize_row(r) for r in rows]
    conn.close()
    return json_response(handler, 200, data)

def users_create(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem adicionar membros à equipe."})
    body = read_body(handler)
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or "123456"
    role = (body.get("role") or "Advogado").strip()
    oab = (body.get("oab") or "").strip() or None
    oab_uf = (body.get("oab_uf") or "").strip().upper() or None
    phone = (body.get("phone") or "").strip() or None
    if not name or not email:
        return json_response(handler, 400, {"error": "Nome e email sao obrigatorios."})
    if not valid_email(email):
        return json_response(handler, 400, {"error": "E-mail inválido."})
    if len(password) < 6:
        return json_response(handler, 400, {"error": "Senha deve ter no mínimo 6 caracteres."})
    conn = db()
    exists = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if exists:
        conn.close()
        return json_response(handler, 409, {"error": "Email ja cadastrado."})
    uid = str(uuid.uuid4())
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users(id,name,email,password,role,oab,phone,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid, name, email, hash_pwd(password), role, oab, phone, now),
    )
    audit(conn, user["id"], "create", "users", uid, after={"name": name, "email": email, "role": role})
    conn.commit()
    row = conn.execute("SELECT id,name,email,role,oab,oab_uf,phone,created_at FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return json_response(handler, 200, serialize_row(row))

def users_update(handler, uid):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user) and user["id"] != uid:
        return json_response(handler, 403, {"error": "Apenas sócios podem editar outros membros."})
    body = read_body(handler)
    conn = db()
    before = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not before:
        conn.close()
        return json_response(handler, 404, {"error": "Não encontrado."})
    sets, vals = [], []
    for f in ("name", "email", "role", "oab", "oab_uf", "phone"):
        if f in body:
            if f == "oab_uf" and body[f]:
                sets.append(f"{f}=?")
                vals.append(str(body[f]).strip().upper() or None)
            else:
                sets.append(f"{f}=?")
                vals.append(body[f] or None)
    if body.get("password"):
        if len(body["password"]) < 6:
            conn.close()
            return json_response(handler, 400, {"error": "Senha deve ter no mínimo 6 caracteres."})
        sets.append("password=?")
        vals.append(hash_pwd(body["password"]))
    if sets:
        vals.append(uid)
        conn.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
        audit(conn, user["id"], "update", "users", uid, before=dict(before), after=body)
        conn.commit()
    row = conn.execute("SELECT id,name,email,role,oab,phone,created_at FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return json_response(handler, 200, serialize_row(row))

def users_delete(handler, uid):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem remover membros."})
    if user["id"] == uid:
        return json_response(handler, 400, {"error": "Você não pode remover seu próprio usuário."})
    conn = db()
    before = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    audit(conn, user["id"], "delete", "users", uid, before=dict(before) if before else None)
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True})

route("GET",    "/api/users", users_list)
route("POST",   "/api/users", users_create)
route("PUT",    "/api/users/{id}", users_update)
route("DELETE", "/api/users/{id}", users_delete)


# ---- DASHBOARD ----

def dashboard_summary(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    today = datetime.date.today().isoformat()
    in15 = (datetime.date.today() + datetime.timedelta(days=15)).isoformat()

    def cnt(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    def sumval(sql, params=()):
        r = conn.execute(sql, params).fetchone()[0]
        return r or 0

    total_cases = cnt("SELECT COUNT(*) FROM cases")
    active_cases = cnt("SELECT COUNT(*) FROM cases WHERE status='em_andamento'")
    total_clients = cnt("SELECT COUNT(*) FROM clients")
    total_users = cnt("SELECT COUNT(*) FROM users")

    today_events = conn.execute("SELECT * FROM events WHERE date=? ORDER BY time", (today,)).fetchall()
    upcoming_events = conn.execute("SELECT * FROM events WHERE date>=? AND date<=? ORDER BY date, time", (today, in15)).fetchall()
    overdue_tasks = conn.execute("SELECT * FROM tasks WHERE status='pendente' AND due_date<? ORDER BY due_date", (today,)).fetchall()
    pending_tasks = conn.execute("SELECT * FROM tasks WHERE status='pendente' ORDER BY due_date").fetchall()
    upcoming_deadlines = conn.execute("SELECT * FROM cases WHERE next_deadline IS NOT NULL AND next_deadline>=? AND next_deadline<=? ORDER BY next_deadline", (today, in15)).fetchall()

    monthly = []
    for i in range(5, -1, -1):
        m = (datetime.date.today().replace(day=1) - datetime.timedelta(days=30 * i))
        first = m.replace(day=1).isoformat()
        if m.month == 12:
            next_first = m.replace(year=m.year + 1, month=1, day=1)
        else:
            next_first = m.replace(month=m.month + 1, day=1)
        rec = sumval("SELECT SUM(amount) FROM transactions WHERE type='receita' AND status='pago' AND date>=? AND date<?", (first, next_first.isoformat()))
        exp = sumval("SELECT SUM(amount) FROM transactions WHERE type='despesa' AND status='pago' AND date>=? AND date<?", (first, next_first.isoformat()))
        monthly.append({"month": m.strftime("%b/%y"), "receita": rec, "despesa": exp})

    by_area = conn.execute("SELECT area, COUNT(*) AS total FROM cases GROUP BY area").fetchall()
    by_status = conn.execute("SELECT status, COUNT(*) AS total FROM cases GROUP BY status").fetchall()

    pending_receivable = sumval("SELECT SUM(amount) FROM transactions WHERE type='receita' AND status='pendente'")
    pending_payable = sumval("SELECT SUM(amount) FROM transactions WHERE type='despesa' AND status='pendente'")
    received_total = sumval("SELECT SUM(amount) FROM transactions WHERE type='receita' AND status='pago'")
    paid_total = sumval("SELECT SUM(amount) FROM transactions WHERE type='despesa' AND status='pago'")

    conn.close()

    return json_response(handler, 200, {
        "kpi": {
            "active_cases": active_cases,
            "total_cases": total_cases,
            "total_clients": total_clients,
            "total_users": total_users,
            "today_events": len(today_events),
            "overdue_tasks": len(overdue_tasks),
            "pending_tasks": len(pending_tasks),
            "upcoming_deadlines": len(upcoming_deadlines),
            "pending_receivable": pending_receivable,
            "pending_payable": pending_payable,
            "received_total": received_total,
            "paid_total": paid_total,
            "balance": received_total - paid_total,
        },
        "today_events": [serialize_row(e) for e in today_events],
        "upcoming_events": [serialize_row(e) for e in upcoming_events],
        "overdue_tasks": [serialize_row(t) for t in overdue_tasks],
        "pending_tasks": [serialize_row(t) for t in pending_tasks],
        "upcoming_deadlines": [serialize_row(c) for c in upcoming_deadlines],
        "monthly": monthly,
        "by_area": [dict(r) for r in by_area],
        "by_status": [dict(r) for r in by_status],
    })


route("GET", "/api/dashboard", dashboard_summary)


# ---- CASE UPDATES (andamento) ----

def case_updates_list(handler, case_id):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    rows = conn.execute("SELECT * FROM case_updates WHERE case_id=? ORDER BY date DESC", (case_id,)).fetchall()
    data = [serialize_row(r) for r in rows]
    conn.close()
    return json_response(handler, 200, data)

def case_updates_create(handler, case_id):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    body = read_body(handler)
    rid = str(uuid.uuid4())
    conn = db()
    conn.execute(
        "INSERT INTO case_updates(id,case_id,date,title,description,type) VALUES (?,?,?,?,?,?)",
        (rid, case_id, body.get("date") or datetime.date.today().isoformat(), body.get("title") or "Andamento", body.get("description"), body.get("type") or "andamento"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM case_updates WHERE id=?", (rid,)).fetchone()
    conn.close()
    return json_response(handler, 200, serialize_row(row))

def case_updates_delete(handler, case_id, update_id):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    conn.execute("DELETE FROM case_updates WHERE id=? AND case_id=?", (update_id, case_id))
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True})

def case_updates_delete_with_id(handler, update_id):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    conn.execute("DELETE FROM case_updates WHERE id=?", (update_id,))
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True})

route("GET",    "/api/cases/{id}/updates", case_updates_list)
route("POST",   "/api/cases/{id}/updates", case_updates_create)
route("DELETE", "/api/case-updates/{id}", case_updates_delete_with_id)


# ---- SETTINGS ----

def settings_get(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    data = {r["key"]: r["value"] for r in rows}
    conn.close()
    return json_response(handler, 200, data)

def settings_set(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    body = read_body(handler)
    conn = db()
    for k, v in body.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True})

route("GET",  "/api/settings", settings_get)
route("POST", "/api/settings", settings_set)


# ---- EXPORT / IMPORT ----

def export_all(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    tables = ["users", "clients", "cases", "case_updates", "tasks", "events", "transactions", "documents", "settings", "audit_log"]
    data = {}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t}").fetchall()
        rows = [dict(r) for r in rows]
        if t == "users":
            for r in rows:
                r.pop("password", None)  # segurança: nunca exportar hashes
        data[t] = rows
    data["_meta"] = {
        "version": "2.0",
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "app": "LexFlow",
    }
    conn.close()
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="lexflow_backup.json"')
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

route("GET", "/api/export", export_all)


# ---- SEARCH ----

def search_all(handler):
    if not require_auth(handler):
        return json_response(handler, 401, {"error": "Nao autenticado."})
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    q = (qs.get("q", [""])[0] or "").strip()
    if not q:
        return json_response(handler, 200, {"q": "", "cases": [], "clients": [], "tasks": [], "events": [], "documents": [], "transactions": []})
    like = f"%{q}%"
    conn = db()
    cases = conn.execute(
        "SELECT id,code,title,client_id,status,priority,next_deadline FROM cases "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (title LIKE ? OR code LIKE ? OR opposing_party LIKE ? OR tags LIKE ?) ORDER BY created_at DESC LIMIT 10",
        (like, like, like, like),
    ).fetchall()
    clients_rows = conn.execute(
        "SELECT id,type,name,document,email FROM clients "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (name LIKE ? OR document LIKE ? OR email LIKE ?) ORDER BY name LIMIT 10",
        (like, like, like),
    ).fetchall()
    clients = []
    for c in clients_rows:
        d = dict(c)
        if d.get("document"):
            d["document_masked"] = mask_doc(d["document"])
        clients.append(d)
    tasks = conn.execute(
        "SELECT id,title,case_id,responsible_id,status,due_date FROM tasks "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (title LIKE ? OR description LIKE ?) ORDER BY due_date LIMIT 10",
        (like, like),
    ).fetchall()
    events = conn.execute(
        "SELECT id,title,type,date,time,case_id FROM events "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (title LIKE ? OR location LIKE ?) ORDER BY date LIMIT 10",
        (like, like),
    ).fetchall()
    documents = conn.execute(
        "SELECT id,title,case_id,category,date FROM documents "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (title LIKE ? OR notes LIKE ?) ORDER BY date DESC LIMIT 10",
        (like, like),
    ).fetchall()
    transactions = conn.execute(
        "SELECT id,type,description,amount,date,status FROM transactions "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND (description LIKE ? OR category LIKE ?) ORDER BY date DESC LIMIT 10",
        (like, like),
    ).fetchall()
    conn.close()
    # Achatar tudo em "items" para o frontend (que mapeia n.type/link/title)
    items = []
    for kind, rows in (("case", cases), ("client", clients), ("task", tasks), ("event", events), ("document", documents), ("transaction", transactions)):
        for r in rows:
            d = serialize_row(r)
            items.append({
                "type": kind,
                "id": d.get("id"),
                "link": {"case": "case-detail", "client": "clients", "task": "tasks", "event": "agenda", "document": "documents", "transaction": "finance"}.get(kind, "dashboard"),
                "params": {"id": d.get("id"), "case_id": d.get("case_id") or d.get("id")} if kind in ("case",) else {"id": d.get("id")} if kind != "transaction" else {},
                "title": d.get("title") or d.get("name") or d.get("description") or "(sem título)",
                "subtitle": d.get("code") or d.get("document_masked") or d.get("email") or d.get("date") or "",
            })
    return json_response(handler, 200, {"q": q, "items": items, "cases": [serialize_row(c) for c in cases], "clients": [serialize_row(c) for c in clients]})

route("GET", "/api/search", search_all)


# ---- NOTIFICATIONS ----

def notifications_list(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    today = datetime.date.today()
    in3 = (today + datetime.timedelta(days=3)).isoformat()
    in7 = (today + datetime.timedelta(days=7)).isoformat()
    in15 = (today + datetime.timedelta(days=15)).isoformat()
    today_s = today.isoformat()
    conn = db()
    overdue_tasks = conn.execute(
        "SELECT id,title,case_id,responsible_id,due_date,priority FROM tasks "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND status='pendente' AND due_date<? "
        "ORDER BY due_date LIMIT 20",
        (today_s,),
    ).fetchall()
    due_soon_tasks = conn.execute(
        "SELECT id,title,case_id,responsible_id,due_date,priority FROM tasks "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND status='pendente' AND due_date>=? AND due_date<=? "
        "ORDER BY due_date LIMIT 20",
        (today_s, in3),
    ).fetchall()
    today_events = conn.execute(
        "SELECT id,title,type,date,time,case_id,location FROM events "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND date=? ORDER BY time",
        (today_s,),
    ).fetchall()
    upcoming_events = conn.execute(
        "SELECT id,title,type,date,time,case_id,location FROM events "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND date>=? AND date<=? ORDER BY date, time LIMIT 15",
        (today_s, in7),
    ).fetchall()
    upcoming_deadlines = conn.execute(
        "SELECT id,code,title,client_id,next_deadline,priority,status FROM cases "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND next_deadline IS NOT NULL AND next_deadline>=? AND next_deadline<=? "
        "ORDER BY next_deadline LIMIT 15",
        (today_s, in15),
    ).fetchall()
    overdue_deadlines = conn.execute(
        "SELECT id,code,title,client_id,next_deadline,priority,status FROM cases "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND next_deadline IS NOT NULL AND next_deadline<? "
        "ORDER BY next_deadline LIMIT 20",
        (today_s,),
    ).fetchall()
    pending_receivable = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS total FROM transactions "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND type='receita' AND status='pendente'"
    ).fetchone()["total"]
    pending_payable = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS total FROM transactions "
        "WHERE (deleted_at IS NULL OR deleted_at='') AND type='despesa' AND status='pendente'"
    ).fetchone()["total"]
    conn.close()
    return json_response(handler, 200, {
        "counts": {
            "overdue_tasks": len(overdue_tasks),
            "due_soon_tasks": len(due_soon_tasks),
            "today_events": len(today_events),
            "upcoming_events": len(upcoming_events),
            "upcoming_deadlines": len(upcoming_deadlines),
            "overdue_deadlines": len(overdue_deadlines),
            "total_alerts": len(overdue_tasks) + len(due_soon_tasks) + len(today_events) + len(overdue_deadlines),
        },
        "overdue_tasks": [serialize_row(t) for t in overdue_tasks],
        "due_soon_tasks": [serialize_row(t) for t in due_soon_tasks],
        "today_events": [serialize_row(e) for e in today_events],
        "upcoming_events": [serialize_row(e) for e in upcoming_events],
        "upcoming_deadlines": [serialize_row(c) for c in upcoming_deadlines],
        "overdue_deadlines": [serialize_row(c) for c in overdue_deadlines],
        "pending_receivable": pending_receivable,
        "pending_payable": pending_payable,
    })

route("GET", "/api/notifications", notifications_list)


# ---- TRASH (lixeira) ----

TRASH_TABLES = ("clients", "cases", "tasks", "events", "transactions", "documents", "case_updates")

def trash_list(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    conn = db()
    out = {}
    for t in TRASH_TABLES:
        try:
            rows = conn.execute(
                f"SELECT * FROM {t} WHERE deleted_at IS NOT NULL AND deleted_at<>'' ORDER BY deleted_at DESC LIMIT 200"
            ).fetchall()
            out[t] = [serialize_row(r) for r in rows]
        except Exception:
            out[t] = []
    conn.close()
    return json_response(handler, 200, out)

def trash_restore(handler, table, rid):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if table not in TRASH_TABLES:
        return json_response(handler, 400, {"error": "Tabela não suporta lixeira."})
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close()
        return json_response(handler, 404, {"error": "Não encontrado."})
    conn.execute(f"UPDATE {table} SET deleted_at=NULL WHERE id=?", (rid,))
    audit(conn, user["id"], "restore", table, rid, before=dict(row))
    conn.commit()
    out = serialize_row(row)
    if table == "clients" and out.get("document"):
        out["document_masked"] = mask_doc(out["document"])
    conn.close()
    return json_response(handler, 200, out)

def trash_purge(handler, table, rid):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem excluir definitivamente."})
    if table not in TRASH_TABLES:
        return json_response(handler, 400, {"error": "Tabela não suporta lixeira."})
    conn = db()
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
    if not row:
        conn.close()
        return json_response(handler, 404, {"error": "Não encontrado."})
    conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
    audit(conn, user["id"], "purge", table, rid, before=dict(row))
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True})

route("GET",   "/api/trash",                        trash_list)
route("POST",  "/api/trash/{table}/{id}/restore",   trash_restore)
route("DELETE","/api/trash/{table}/{id}",           trash_purge)


# ---- AUDIT LOG ----

def audit_list(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem ver a auditoria."})
    parsed = urlparse(handler.path)
    qs = parse_qs(parsed.query)
    try:
        limit = min(int(qs.get("limit", ["200"])[0]), 1000)
    except Exception:
        limit = 200
    entity = (qs.get("entity", [""])[0] or "").strip()
    user_id = (qs.get("user_id", [""])[0] or "").strip()
    sql = ("SELECT a.id,a.user_id,a.action,a.entity,a.entity_id,a.created_at, "
           "u.name AS user_name, u.email AS user_email "
           "FROM audit_log a LEFT JOIN users u ON u.id=a.user_id")
    conds = []
    params = []
    if entity:
        conds.append("a.entity=?")
        params.append(entity)
    if user_id:
        conds.append("a.user_id=?")
        params.append(user_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += f" ORDER BY a.created_at DESC LIMIT {limit}"
    conn = db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    for d in data:
        d["details"] = (d.get("entity") or "") + ((" • " + d["entity_id"][:8]) if d.get("entity_id") else "")
    return json_response(handler, 200, {"items": data, "total": len(data)})

def audit_get_detail(handler, aid):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem ver a auditoria."})
    conn = db()
    row = conn.execute("SELECT * FROM audit_log WHERE id=?", (aid,)).fetchone()
    conn.close()
    if not row:
        return json_response(handler, 404, {"error": "Não encontrado."})
    out = dict(row)
    # Tentar decodificar before/after JSON
    for f in ("before", "after"):
        if out.get(f):
            try:
                out[f] = json.loads(out[f])
            except Exception:
                pass
    return json_response(handler, 200, out)


# ----------------------------- MONITORING -----------------------------

def _mon_settings_get(key, default=None):
    conn = db()
    try:
        r = conn.execute("SELECT value FROM monitoring_settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        conn.close()


def _mon_settings_set(key, value):
    conn = db()
    try:
        conn.execute("INSERT INTO monitoring_settings(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()
    finally:
        conn.close()


def _mon_get_api_key():
    """Retorna a API key em plaintext. Busca primeiro no settings (criptografada)
    e cai pra APIKeyPublicaCNJ se nada tiver sido configurado."""
    enc = _mon_settings_get("monitor.api_key", "")
    if not enc or enc == "APIKeyPublicaCNJ":
        return enc or "APIKeyPublicaCNJ"
    if HAS_MONITOR:
        return _monitor.decrypt_value(enc)
    return enc


def _mon_upsert_monitoring(case_id, status="active", interval_minutes=None, tribunal=None):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn = db()
    try:
        existing = conn.execute("SELECT case_id FROM monitoring WHERE case_id=?", (case_id,)).fetchone()
        if existing:
            sets = ["status=?", "updated_at=?"]
            vals = [status, now]
            if interval_minutes is not None:
                sets.append("interval_minutes=?")
                vals.append(interval_minutes)
            if tribunal is not None:
                sets.append("tribunal=?")
                vals.append(tribunal)
            vals.append(case_id)
            conn.execute(f"UPDATE monitoring SET {', '.join(sets)} WHERE case_id=?", vals)
        else:
            conn.execute(
                "INSERT INTO monitoring(case_id, status, interval_minutes, tribunal, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (case_id, status, interval_minutes or 60, tribunal, now, now)
            )
        conn.commit()
    finally:
        conn.close()


def monitor_case_toggle(handler, case_id, body):
    """POST /api/cases/{id}/monitor — body: {status: 'active'|'paused', interval_minutes: int}"""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    status = (body or {}).get("status", "active")
    if status not in ("active", "paused"):
        return json_response(handler, 400, {"error": "status invalido"})
    try:
        interval = int((body or {}).get("interval_minutes", 60))
    except Exception:
        interval = 60
    if interval < 5:
        interval = 5
    if interval > 1440:
        interval = 1440
    tribunal = (body or {}).get("tribunal") or None
    # Buscar caso
    conn = db()
    try:
        c = conn.execute("SELECT id, code, court, title FROM cases WHERE id=? AND (deleted_at IS NULL OR deleted_at='')", (case_id,)).fetchone()
        if not c:
            return json_response(handler, 404, {"error": "caso nao encontrado"})
        # Comunica PJE descobre o tribunal pela UF da OAB do responsavel
        # (campo "tribunal" do monitoring fica apenas informativo)
        _mon_upsert_monitoring(case_id, status=status, interval_minutes=interval, tribunal=tribunal)
    finally:
        conn.close()
    audit(user["id"], "update", "monitoring", case_id, None,
          {"status": status, "interval_minutes": interval, "tribunal": tribunal})
    return json_response(handler, 200, {"ok": True, "status": status, "interval_minutes": interval, "tribunal": tribunal})


def monitor_run_now(handler, case_id, body=None):
    """POST /api/cases/{id}/monitor/run — checagem imediata via Comunica PJE.

    v2.8: busca publicacoes pelo NUMERO DE PROCESSO (CNJ) do caso.
    URL: https://comunica.pje.jus.br/consulta?siglaTribunal={TJ}&numeroProcesso={CNJ}
    Auto-preenche dados do caso (classe, assunto, partes) a partir da publicacao.
    """
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    if not HAS_MONITOR or _monitor is None:
        return json_response(handler, 503, {"error": "monitor indisponivel"})
    conn = db()
    try:
        row = conn.execute(
            "SELECT c.id, c.code AS cnj, c.responsible_id, c.title, c.area, c.court, "
            "       u.oab AS responsible_oab, u.oab_uf AS responsible_oab_uf "
            "FROM cases c LEFT JOIN users u ON u.id = c.responsible_id "
            "WHERE c.id=? AND (c.deleted_at IS NULL OR c.deleted_at='')",
            (case_id,),
        ).fetchone()
        if not row:
            return json_response(handler, 404, {"error": "caso nao encontrado"})
        cnj = (row["cnj"] or "").strip()
        if not cnj or not _monitor.normalize_cnj(cnj):
            return json_response(handler, 400, {"error": "caso sem CNJ valido cadastrado", "cnj": cnj})
        cnj_fmt = _monitor.normalize_cnj(cnj)
        # Determina UF: prioriza oab_uf do user, senao tenta extrair do CNJ
        oab_uf = (row["responsible_oab_uf"] or "").strip().upper()
        oab_num = ""
        oab_text = (row["responsible_oab"] or "").strip()
        if oab_text:
            parsed = _monitor._parse_oab(oab_text)
            oab_num = parsed.get("numero") or ""
            if not oab_uf and parsed.get("uf"):
                oab_uf = parsed["uf"]
        if not oab_uf:
            oab_uf = "RJ"  # fallback
        # Chama o Comunica PJE por NUMERO DE PROCESSO
        try:
            res = _monitor.scraper_pje_for_case(cnj_fmt, oab_num, oab_uf)
        except Exception as e:
            return json_response(handler, 500, {"error": "falha no Comunica PJE", "detail": str(e)[:200]})
        if res.get("error"):
            return json_response(handler, 502, {"error": res["error"], "url": res.get("url", "")})
        pubs = res.get("pubs", [])
        case_info = res.get("case_info", {})
        
        # Auto-preencher dados do caso a partir da publicacao
        auto_filled = {}
        if case_info:
            updates = []
            params = []
            if case_info.get("classe") and not row["area"]:
                updates.append("area = ?"); params.append(case_info["classe"])
                auto_filled["area"] = case_info["classe"]
            if case_info.get("assunto") and not row["court"]:
                updates.append("court = ?"); params.append(case_info["assunto"])
                auto_filled["court"] = case_info["assunto"]
            if updates:
                params.append(case_id)
                conn.execute(f"UPDATE cases SET {', '.join(updates)} WHERE id = ?", params)
                conn.commit()
        
        worker = MONITOR_WORKER.get("instance")
        total_inserted = 0
        sample_pubs = []
        # Como ja temos o caso, NAO auto-criamos caso novo aqui - so inserimos andamentos
        ins = worker._insert_dedupe_pubs_for_case(case_id, pubs) if worker else 0
        total_inserted = ins
        for pub in pubs:
            sample_pubs.append({
                "date": (pub.get("date") or "")[:10],
                "title": (pub.get("title") or "")[:200],
                "cnj": pub.get("cnj") or cnj_fmt,
                "case_id": case_id,
                "new_case": False,
                "url": pub.get("url") or "",
            })
        result = {
            "source": "comunica_pje",
            "cnj": cnj_fmt,
            "tribunal": res.get("tribunal", ""),
            "pubs_found": len(pubs),
            "inserted": total_inserted,
            "new_cases": 0,
            "auto_filled": auto_filled,
            "url": res.get("url", ""),
            "pubs": sample_pubs[:5],
        }
        # update last_check_at
        if worker:
            worker._update_monitoring_state(case_id,
                last_check_at=datetime.datetime.now().isoformat(timespec="seconds"),
                error_count=0, last_error=None)
        audit(user["id"], "monitor_run", "case", case_id, None,
              {"pubs_found": len(pubs), "inserted": total_inserted, "auto_filled": auto_filled})
    finally:
        conn.close()
    return json_response(handler, 200, result)


def monitor_status(handler):
    """GET /api/monitoring/status — resumo por caso."""
    user = require_auth(handler)
    if not user:
        return
    conn = db()
    try:
        rows = conn.execute("""
            SELECT m.case_id, m.status, m.interval_minutes, m.last_check_at,
                   m.last_movement_at, m.last_movement_title, m.last_movement_source,
                   m.error_count, m.last_error, m.tribunal,
                   c.code AS cnj, c.title AS case_title, c.court, c.responsible_id,
                   u.oab AS responsible_oab,
                   u.name AS responsible_name,
                   (SELECT COUNT(*) FROM monitoring_log ml WHERE ml.case_id=m.case_id) AS log_count
            FROM monitoring m
            JOIN cases c ON c.id = m.case_id
            LEFT JOIN users u ON u.id = c.responsible_id
            WHERE (c.deleted_at IS NULL OR c.deleted_at='')
            ORDER BY c.title
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            oab = (d.get("responsible_oab") or "").strip()
            if oab:
                import re as _re
                m_num = _re.search(r"(\d{4,6})", oab)
                # Aceita: "OAB/RJ 244.384", "OAB RJ 244384", "RJ 244384",
                #         "244384/RJ", "244384-RJ", "OAB244384RJ", etc.
                m_uf = (
                    _re.search(r"/([A-Z]{2})\b", oab)        # /RJ
                    or _re.search(r"\b([A-Z]{2})\s+\d", oab)  # RJ 244
                    or _re.search(r"\b([A-Z]{2})$", oab)      # termina com RJ
                    or _re.search(r"^([A-Z]{2})\b", oab)      # comeca com RJ
                )
                d["responsible_oab_num"] = m_num.group(1) if m_num else None
                d["responsible_oab_uf"] = m_uf.group(1) if m_uf else None
            out.append(d)
    finally:
        conn.close()
    return json_response(handler, 200, {"items": out})


def monitor_settings_get(handler):
    """GET /api/monitoring/settings"""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    keys = [
        "monitor.api_key", "monitor.default_interval_minutes", "monitor.notify_desktop",
        "monitor.notify_email", "monitor.notify_email_address",
        "monitor.dje_enabled", "monitor.datajud_enabled",
    ]
    out = {k: _mon_settings_get(k, "") for k in keys}
    if "monitor.api_key" in out and out["monitor.api_key"]:
        out["monitor.api_key_masked"] = "***" + out["monitor.api_key"][-4:]
    else:
        out["monitor.api_key_masked"] = "(padrao CNJ)"
    return json_response(handler, 200, out)


def monitor_settings_set(handler, body):
    """POST /api/monitoring/settings"""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    body = body or {}
    if "api_key" in body and body["api_key"]:
        plain = str(body["api_key"]).strip()
        if HAS_MONITOR and plain and plain != "APIKeyPublicaCNJ":
            enc = _monitor.encrypt_value(plain)
            _mon_settings_set("monitor.api_key", enc)
        else:
            _mon_settings_set("monitor.api_key", plain)
    if "default_interval_minutes" in body:
        try:
            v = max(5, min(1440, int(body["default_interval_minutes"])))
            _mon_settings_set("monitor.default_interval_minutes", str(v))
        except Exception:
            pass
    for k in ("notify_desktop", "notify_email"):
        if k in body:
            _mon_settings_set(f"monitor.{k}", "1" if body[k] else "0")
    # dje_enabled e datajud_enabled sao ignorados (v2.5 usa apenas Comunica PJE)
    # mantemos retrocompat para nao quebrar UI antiga
    if "notify_email_address" in body:
        _mon_settings_set("monitor.notify_email_address", str(body["notify_email_address"] or ""))
    return monitor_settings_get(handler)


def monitor_log_list(handler):
    """GET /api/monitoring/log — ultimas N checagens."""
    user = require_auth(handler)
    if not user:
        return
    q = parse_qs(urlparse(handler.path).query or "")
    limit = int((q.get("limit") or [50])[0])
    limit = max(1, min(500, limit))
    conn = db()
    try:
        rows = conn.execute("""
            SELECT ml.id, ml.case_id, ml.checked_at, ml.source, ml.ok, ml.message, ml.movements_found,
                   c.title AS case_title,
                   u.oab AS responsible_oab
            FROM monitoring_log ml
            LEFT JOIN cases c ON c.id = ml.case_id
            LEFT JOIN users u ON u.id = c.responsible_id
            ORDER BY ml.checked_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            oab = (d.get("responsible_oab") or "").strip()
            if oab:
                import re as _re
                m_num = _re.search(r"(\d{4,6})", oab)
                # Aceita: "OAB/RJ 244.384", "OAB RJ 244384", "RJ 244384",
                #         "244384/RJ", "244384-RJ", "OAB244384RJ", etc.
                m_uf = (
                    _re.search(r"/([A-Z]{2})\b", oab)        # /RJ
                    or _re.search(r"\b([A-Z]{2})\s+\d", oab)  # RJ 244
                    or _re.search(r"\b([A-Z]{2})$", oab)      # termina com RJ
                    or _re.search(r"^([A-Z]{2})\b", oab)      # comeca com RJ
                )
                d["responsible_oab_num"] = m_num.group(1) if m_num else None
                d["responsible_oab_uf"] = m_uf.group(1) if m_uf else None
            out.append(d)
    finally:
        conn.close()
    return json_response(handler, 200, {"items": out})




# ---- CASE FOLDERS (vincular pasta do sistema ao caso) ----

def case_folder_set(handler, case_id, body):
    """POST /api/cases/{id}/folder - vincula uma pasta do sistema ao caso."""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    body = body or {}
    folder_path = (body.get("path") or "").strip()
    label = (body.get("label") or "").strip() or None
    if not folder_path:
        return json_response(handler, 400, {"error": "path obrigatorio"})
    # Validacao basica de seguranca: caminho absoluto e existente
    import os as _os
    if not _os.path.isabs(folder_path):
        return json_response(handler, 400, {"error": "path deve ser absoluto"})
    if not _os.path.isdir(folder_path):
        return json_response(handler, 400, {"error": f"pasta nao encontrada: {folder_path}"})
    conn = db()
    try:
        existing = conn.execute(
            "SELECT id FROM case_folders WHERE case_id=? AND path=?",
            (case_id, folder_path),
        ).fetchone()
        if existing:
            conn.execute("UPDATE case_folders SET label=?, updated_at=? WHERE id=?",
                         (label, datetime.datetime.now().isoformat(timespec="seconds"), existing["id"]))
            fid = existing["id"]
        else:
            fid = secrets.token_hex(8)
            conn.execute(
                "INSERT INTO case_folders(id, case_id, path, label, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (fid, case_id, folder_path, label,
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
        conn.commit()
        audit(user["id"], "update", "case_folder", case_id, None, {"path": folder_path, "label": label})
    finally:
        conn.close()
    return json_response(handler, 200, {"ok": True, "id": fid, "path": folder_path, "label": label})


def case_folder_unset(handler, case_id, body):
    """DELETE /api/cases/{id}/folder - remove vinculo da pasta."""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    body = body or {}
    folder_id = (body.get("id") or "").strip()
    if not folder_id:
        return json_response(handler, 400, {"error": "id obrigatorio"})
    conn = db()
    try:
        conn.execute("DELETE FROM case_folders WHERE id=? AND case_id=?", (folder_id, case_id))
        conn.commit()
    finally:
        conn.close()
    return json_response(handler, 200, {"ok": True})


def case_folder_list_files(handler, case_id):
    """GET /api/cases/{id}/folder/files - lista arquivos das pastas vinculadas."""
    user = require_auth(handler)
    if not user:
        return
    conn = db()
    try:
        folders = conn.execute(
            "SELECT id, path, label, created_at FROM case_folders WHERE case_id=? ORDER BY created_at",
            (case_id,),
        ).fetchall()
        folders_list = [dict(f) for f in folders]
    finally:
        conn.close()
    
    import os as _os
    files = []
    for f in folders_list:
        p = f["path"]
        if not _os.path.isdir(p):
            files.append({
                "folder_id": f["id"], "folder_path": p, "folder_label": f.get("label") or "",
                "error": f"pasta inacessivel: {p}",
                "files": [],
            })
            continue
        try:
            entries = []
            for name in sorted(_os.listdir(p)):
                full = _os.path.join(p, name)
                try:
                    st = _os.stat(full)
                    entries.append({
                        "name": name,
                        "path": full,
                        "size": st.st_size,
                        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                        "is_dir": _os.path.isdir(full),
                        "ext": (_os.path.splitext(name)[1] or "").lower().lstrip("."),
                    })
                except Exception:
                    continue
            files.append({
                "folder_id": f["id"], "folder_path": p, "folder_label": f.get("label") or "",
                "files": entries,
            })
        except Exception as e:
            files.append({
                "folder_id": f["id"], "folder_path": p, "folder_label": f.get("label") or "",
                "error": str(e)[:200], "files": [],
            })
    
    return json_response(handler, 200, {"folders": folders_list, "lists": files})


def case_folder_read_file(handler, case_id, body):
    """POST /api/cases/{id}/folder/read - le conteudo de um arquivo (somente texto)."""
    user = require_auth(handler)
    if not user:
        return
    if not is_socio(user):
        return json_response(handler, 403, {"error": "forbidden"})
    body = body or {}
    file_path = (body.get("path") or "").strip()
    if not file_path:
        return json_response(handler, 400, {"error": "path obrigatorio"})
    import os as _os
    if not _os.path.isabs(file_path):
        return json_response(handler, 400, {"error": "path deve ser absoluto"})
    if not _os.path.isfile(file_path):
        return json_response(handler, 400, {"error": "arquivo nao encontrado"})
    if _os.path.getsize(file_path) > 2 * 1024 * 1024:
        return json_response(handler, 400, {"error": "arquivo maior que 2MB"})
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fp:
            content = fp.read(200_000)
    except UnicodeDecodeError:
        return json_response(handler, 400, {"error": "arquivo binario (nao UTF-8)"})
    return json_response(handler, 200, {"path": file_path, "content": content, "size": len(content)})


# Rotas de monitoramento (wrappers definidos ANTES)
def monitor_case_toggle_with_id(handler, case_id, body):
    return monitor_case_toggle(handler, case_id, body)


def monitor_run_now_with_id(handler, case_id, body):
    return monitor_run_now(handler, case_id, body)


route("POST", "/api/cases/{id}/monitor",       monitor_case_toggle_with_id)
route("POST", "/api/cases/{id}/monitor/run",   monitor_run_now_with_id)
route("GET",  "/api/monitoring/status",        monitor_status)
route("GET",  "/api/monitoring/settings",      monitor_settings_get)
route("POST", "/api/monitoring/settings",      monitor_settings_set)
route("GET",  "/api/monitoring/log",           monitor_log_list)

route("POST",   "/api/cases/{id}/folder",        case_folder_set)
route("DELETE", "/api/cases/{id}/folder",        case_folder_unset)
route("GET",    "/api/cases/{id}/folder/files",  case_folder_list_files)
route("POST",   "/api/cases/{id}/folder/read",   case_folder_read_file)

route("GET", "/api/audit",            audit_list)
route("GET", "/api/audit/{id}",       audit_get_detail)


# ---- IMPORT ----

def import_all(handler):
    user = require_auth(handler)
    if not user:
        return json_response(handler, 401, {"error": "Nao autenticado."})
    if not is_socio(user):
        return json_response(handler, 403, {"error": "Apenas sócios podem importar backup."})
    body = read_body(handler)
    if not isinstance(body, dict):
        return json_response(handler, 400, {"error": "Backup inválido."})
    if body.get("_meta", {}).get("app") != "LexFlow":
        return json_response(handler, 400, {"error": "Arquivo não parece ser um backup do LexFlow."})
    # Modo seguro: apenas INSERT (não substitui). Ignora audit_log e users (por causa de senhas ausentes)
    ALLOWED = ("clients", "cases", "case_updates", "tasks", "events", "transactions", "documents", "settings")
    conn = db()
    imported = {}
    for t in ALLOWED:
        rows = body.get(t, [])
        if not isinstance(rows, list):
            continue
        count = 0
        for r in rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            try:
                cols = [c for c in r.keys() if c != "id"]
                vals = [r[c] for c in cols]
                placeholders = ",".join(["?"] * (len(cols) + 1))
                col_list = "id," + ",".join(cols)
                # Verificar se já existe
                exists = conn.execute(f"SELECT 1 FROM {t} WHERE id=?", (r["id"],)).fetchone()
                if exists:
                    continue
                conn.execute(
                    f"INSERT INTO {t} ({col_list}) VALUES ({placeholders})",
                    [r["id"]] + vals,
                )
                count += 1
            except Exception as e:
                continue
        imported[t] = count
        audit(conn, user["id"], "import", t, None, after={"count": count})
    conn.commit()
    conn.close()
    return json_response(handler, 200, {"ok": True, "imported": imported})

route("POST", "/api/import", import_all)


# ----------------------------- HTTP HANDLER -----------------------------

class LexFlowHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self):    self.handle_request("GET")
    def do_POST(self):   self.handle_request("POST")
    def do_PUT(self):    self.handle_request("PUT")
    def do_DELETE(self): self.handle_request("DELETE")

    def handle_request(self, method):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            return dispatch(self, method, path)

        # Serve frontend static files
        if path == "/" or path == "":
            path = "/index.html"

        # Path traversal protection
        safe_path = os.path.normpath(path).lstrip(os.sep)
        if safe_path.startswith("..") or os.path.isabs(safe_path):
            return json_response(self, 403, {"error": "forbidden"})

        full = os.path.join(FRONTEND_DIR, safe_path)
        if not os.path.exists(full) or not os.path.isfile(full):
            return json_response(self, 404, {"error": "not found"})

        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".ico":  "image/x-icon",
        }.get(ext, "application/octet-stream")

        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ----------------------------- MAIN -----------------------------

def main():
    print("=" * 60)
    print("  LexFlow - Sistema de Gestao Juridica v2.1")
    print("=" * 60)
    init_db()
    # Inicia o worker de monitoramento em background
    if HAS_MONITOR and _monitor is not None:
        try:
            _interval = int(_mon_settings_get("monitor.default_interval_minutes", "60") or "60")
            _loop_seconds = max(15, min(_interval * 60, 86400) // 10)  # loop rapido (default 36s), checagem real respeita intervalo individual
            MONITOR_WORKER["instance"] = _monitor.MonitoringWorker(
                db_path=DB_PATH, get_api_key_fn=_mon_get_api_key,
                interval_seconds=_loop_seconds,
            )
            MONITOR_WORKER["instance"].start()
            print(f"  Monitor Datajud/DJE: ON (loop {_loop_seconds}s, default interval {_interval} min)")
        except Exception as _e:
            sys.stderr.write(f"[server] nao foi possivel iniciar monitor: {_e}\n")
    else:
        print("  Monitor Datajud/DJE: OFF (modulo indisponivel)")

    print(f"  Banco de dados: {DB_PATH}")
    print(f"  Frontend:       {FRONTEND_DIR}")
    print(f"  Servidor:       http://localhost:{PORT}")
    print("=" * 60)
    print("  Acesse http://localhost:%d no seu navegador" % PORT)
    print("  Pressione Ctrl+C para encerrar")
    print("=" * 60)
    try:
        with HTTPServer(("0.0.0.0", PORT), LexFlowHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  LexFlow encerrado.")


if __name__ == "__main__":
    main()
