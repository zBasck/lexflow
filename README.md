# LexFlow — Sistema de Gestao Juridica

Plataforma **completa e local** para gestao de escritorio de advocacia.
Tudo salvo **na pasta do seu computador** — nao na nuvem, nao em cache do navegador.

Inspirado em plataformas como Clio, Legal One, Themis e Astrea, com modulos
de Casos, Clientes, Agenda, Tarefas, Financeiro, Documentos, Equipe e Dashboard.

## Como rodar (3 passos)

### 1. Instalar o Python (uma unica vez)

Acesse: **https://www.python.org/downloads/**
- Clique em "Download Python 3.x.x"
- Execute o instalador
- **IMPORTANTE:** na primeira tela, marque a caixa **"Add Python to PATH"**
- Clique em "Install Now"

### 2. Clonar o repositorio

```bash
git clone https://github.com/zBasck/lexflow.git
cd lexflow
```

### 3. Iniciar o sistema

**Windows** (duplo clique ou):
```cmd
iniciar.bat
```

**Linux / macOS**:
```bash
chmod +x scripts/run.sh
./scripts/run.sh
```

O navegador abre automaticamente em **http://localhost:8765**.

### Login de demonstracao

| E-mail | Senha | Funcao |
|---|---|---|
| `helena@lexflow.demo` | `123456` | Socia |
| `rafael@lexflow.demo` | `123456` | Advogado Senior |
| `camila@lexflow.demo` | `123456` | Paralegal |

Ou clique em **"Criar conta"** e cadastre a sua propria.

## O que esta incluido

- **Casos (Processos)** com numero CNJ, areas, status, prioridades, andamentos, tarefas e documentos
- **Clientes** PF e PJ com historico financeiro
- **Agenda** com calendario mensal interativo (audiencias, prazos, reunioes)
- **Tarefas** com prioridades, prazos e vinculo com casos
- **Financeiro** com receitas, despesas, fluxo de caixa mensal, status pago/pendente
- **Documentos** organizados por caso e categoria
- **Equipe** (advogados, paralegais, socios) com controle de funcao e OAB
- **Dashboard executivo** com KPIs, graficos de receita vs despesa e distribuicao por area
- **Configuracoes** com backup em JSON, restaurar dados demo, apagar tudo

## Estrutura do projeto

```
lexflow/
├── iniciar.bat              ← Execute para abrir (Windows)
├── README.md
├── LICENSE
├── .gitignore
├── backend/
│   └── server.py            ← Servidor Python + API REST + Banco SQLite
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── scripts/
│   └── run.sh               ← Alternativa para Linux/macOS
└── data/
    └── lexflow.db           ← Banco SQLite (criado automaticamente)
```

## Stack tecnica

- **Backend**: Python 3.8+ (somente biblioteca padrao `http.server` + `sqlite3`)
- **Banco**: SQLite (arquivo unico em `data/lexflow.db`)
- **Frontend**: HTML5 + CSS3 + JavaScript vanilla (zero dependencias externas)
- **API**: REST com autenticacao por token
- **Sem build step, sem npm, sem frameworks**

## Comandos uteis

```bash
# Mudar a porta (padrao: 8765)
# Abra backend/server.py e altere a linha: PORT = 8765

# Resetar o banco de dados (apaga tudo e recria com dados demo)
# Dentro do sistema: Configuracoes > Apagar todos os dados
# Ou manualmente: delete o arquivo data/lexflow.db e reinicie o servidor

# Backup
# Dentro do sistema: Configuracoes > Baixar backup JSON
# Ou copie a pasta data/ inteira

# Parar o servidor
# Pressione Ctrl+C no terminal
```

## Onde os dados ficam salvos

Todos os dados ficam no arquivo `data/lexflow.db` (SQLite padrao). Voce pode:
- Copiar a pasta `data/` para fazer backup
- Mover o banco de computador para outro
- Abrir no DB Browser for SQLite (https://sqlitebrowser.org) se quiser inspecionar
- Apagar o arquivo para resetar tudo (o sistema recria com dados demo)

## Privacidade

- **100% offline** — o servidor roda apenas no seu computador (localhost)
- **Seus dados nunca sao enviados para a internet**
- **Sem telemetria, sem cookies, sem CDN**
- O banco de dados e um arquivo SQLite local que voce controla

## Solucao de problemas

| Problema | Solucao |
|---|---|
| "python nao encontrado" | Instale o Python marcando "Add to PATH" |
| "Porta 8765 em uso" | Mude a linha `PORT = 8765` em `backend/server.py` |
| Tela em branco no navegador | Abra o Console (F12) e veja o erro |
| Quero resetar tudo | Delete `data/lexflow.db` e reinicie o servidor |
| Quero usar outra porta | Altere `PORT` no `server.py` e abra `http://localhost:NOVA_PORTA` |



---

## Novidades da versao 2.0

Esta versao adiciona um conjunto robusto de melhorias de seguranca, produtividade e experiencia.

### Seguranca

- **Validacao de CPF/CNPJ/CNJ/e-mail** em todos os formularios com checksum real (digitos verificadores)
- **Permissoes por papel**: apenas socios podem adicionar/remover membros da equipe
- **Backup seguro**: o `/api/export` nao expoe mais hashes de senha
- **Mascaramento de documentos** sensiveis nas respostas da API (`***.456.789-**`)

### Robustez juridica

- **Soft delete (lixeira)**: nada e apagado de verdade. Itens vao para a lixeira e podem ser restaurados
- **Auditoria completa**: tabela `audit_log` registra quem criou, editou, excluiu ou restaurou cada item
- **Pagina de Auditoria** (acessivel a socios no menu lateral) com historico completo
- **Soft delete para casos, clientes, tarefas, eventos, transacoes, documentos e andamentos**

### Produtividade

- **Busca global** (atalho `Ctrl+K` ou botao no topo): busca em casos, clientes, tarefas, eventos, documentos e transacoes
- **Notificacoes** (sino no topo): tarefas atrasadas, prazos proximos, audiencias de hoje, contas a pagar/receber
- **Notificacoes atualizam a cada 60 segundos** automaticamente
- **Lixeira** com visualizacao por tabela e botao de restaurar

### Experiencia

- **Modo escuro** (botao lua/sol no topo): tema navy com variaveis CSS, persistido no navegador
- **Modal de busca** com navegacao por teclado (ESC para fechar)
- **Painel de notificacoes** lateral com icones coloridos por severidade
- **Documento do cliente vem com mascara** por padrao; clica pra revelar

### Performance

- **Suporte a `?q=`, `?limit=`, `?offset=`** em todos os endpoints de listagem
- **Indices** adicionados em colunas usadas em filtros (deleted_at, next_deadline, due_date, etc.)

### Endpoints novos

- `GET /api/search?q=...` — busca global
- `GET /api/notifications` — notificacoes e prazos
- `GET /api/trash` — listar itens na lixeira
- `POST /api/trash/{table}/{id}/restore` — restaurar item
- `DELETE /api/trash/{table}/{id}` — apagar definitivamente (socio)
- `GET /api/audit` — log de auditoria (socio)
- `GET /api/audit/{id}` — detalhe de uma acao
- `POST /api/import` — importar backup (socio)

### Migracao de bancos existentes

Bancos criados com a v1.0 continuam funcionando. Ao iniciar a v2.0:
- A coluna `deleted_at` e adicionada automaticamente em todas as tabelas principais
- A tabela `audit_log` e criada
- Indices sao criados
- Registros antigos tem `deleted_at = NULL` (continuam visiveis normalmente)

## Licenca

MIT — veja [LICENSE](LICENSE).

---

(c) 2026 LexFlow. Feito para o advogado brasileiro.

---

## Novidades da versão 2.1 — Monitoramento + Kanban

### Monitoramento automático de processos (Datajud + DJe)

Agora o LexFlow consulta **de graça** as APIs públicas dos tribunais para buscar andamentos e intimações dos seus processos, sem precisar de assinatura de serviço pago.

**Fontes integradas (todas gratuitas):**
- **Datajud (CNJ)**: API pública oficial que cobre 100% dos tribunais brasileiros (TJSP, TJRJ, TJRS, TJAM, TRTs, TRFs, STJ, STF).
- **DJe (Diário de Justiça Eletrônico)**: scrapers nativos para os 6 tribunais que você usa:
  - **TJSP** (Diário da Justiça de São Paulo)
  - **TJRJ** — Eproc (1ª instância) e PJe (2ª instância)
  - **TJRS** (Diário da Justiça do Rio Grande do Sul)
  - **TJAM** (Diário da Justiça do Amazonas)
  - **TRT1** (PJe Trabalhista)

**Como funciona:**
1. Abra um caso na página de detalhe, aba "Linha do tempo".
2. Clique em **🔔 Monitorar...** para ligar (escolha o intervalo: 5 min a 24 h).
3. O LexFlow checa automaticamente o Datajud e o DJe desse caso.
4. Quando sair movimento novo, ele aparece como andamento no caso **e dispara notificação nativa do navegador** (popup do sistema).
5. Acesse a página **🔔 Monitoramento** na sidebar para ver status, sincronizar manualmente e ver o histórico de checagens.

**Privacidade e segurança:**
- Sua API key do Datajud fica **criptografada com Fernet** (AES + HMAC) no arquivo `.lexflow.key` na raiz do projeto.
- Chave padrão "APIKeyPublicaCNJ" funciona para testes; para produção, solicite sua chave gratuita em **datajud.cnj.jus.br**.
- Backoff exponencial (1m, 5m, 30m, 2h) e **circuit breaker** (3 falhas → pausa por 10 min) para não derrubar o sistema se a API oscilar.

### Kanban de tarefas por urgência

Nova página **🎯 Kanban** com 4 colunas: **Atrasado · Hoje · Esta semana · Mais tarde**.
- Cards arrastáveis — arraste entre colunas para reagendar (a data é atualizada automaticamente).
- Cor da borda esquerda = prioridade (alta = vermelho, média = laranja, baixa = cinza).
- Clique no card para abrir a edição completa da tarefa.

### Outras melhorias
- Worker thread em background (não bloqueia a UI).
- Endpoint `POST /api/cases/{id}/monitor/run` para sincronização imediata.
- Endpoint `GET /api/monitoring/log` para auditoria das últimas 500 checagens.
- Permissões: apenas sócios podem alterar configurações de monitoramento.
