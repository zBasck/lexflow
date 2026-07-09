/* =============== LEXFLOW FRONTEND =============== */
(function () {
  'use strict';

  // ---------------- STATE ----------------
  const S = {
    user: null,
    token: localStorage.getItem('lexflow_token'),
    csrf: localStorage.getItem('lexflow_csrf'),
    theme: localStorage.getItem('lexflow_theme') || (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
    view: 'landing',
    authTab: 'login',
    data: {
      clients: [], cases: [], tasks: [], events: [],
      transactions: [], documents: [], users: []
    },
    filters: { cases: {}, tasks: 'pending', financeTab: 'receita', financePeriod: 'all', financeStatus: 'all' },
    cal: { year: 0, month: 0 },
    modal: null,
    params: {},
    search: { open: false, q: '', results: [], loading: false },
    notifications: { items: [], open: false },
  };
  // Contador de render para cancelar renders obsoletos (evita race conditions)
  let _renderToken = 0;

  // Aplica tema inicial ao body
  function applyTheme(t) {
    S.theme = t;
    document.body.setAttribute('data-theme', t);
    localStorage.setItem('lexflow_theme', t);
  }
  applyTheme(S.theme);

  // ---------------- API ----------------
  const API = {
    async req(method, path, body) {
      const opts = { method, headers: {}, credentials: 'same-origin' };
      if (S.token) opts.headers['Authorization'] = 'Bearer ' + S.token;
      if (body !== undefined) {
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      // CSRF para metodos que modificam estado
      if (method !== 'GET' && method !== 'OPTIONS' && S.csrf) {
        opts.headers['X-CSRF-Token'] = S.csrf;
      }
      const r = await fetch(path, opts);
      let data;
      try { data = await r.json(); } catch (e) { data = {}; }
      if (r.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/register') {
        S.token = null; S.user = null; S.csrf = null;
        localStorage.removeItem('lexflow_token');
        localStorage.removeItem('lexflow_csrf');
        S.view = 'auth'; render(); toast('Sessao expirada. Faca login novamente.', 'warning');
        throw new Error('unauthorized');
      }
      if (r.status === 403) {
        toast(data.error || 'Sem permissao para essa acao.', 'error');
        throw new Error(data.error || 'forbidden');
      }
      if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
      return data;
    },
    get(p)        { return this.req('GET', p); },
    post(p, b)    { return this.req('POST', p, b); },
    put(p, b)     { return this.req('PUT', p, b); },
    del(p)        { return this.req('DELETE', p); },
  };

  // ---------------- HELPERS ----------------
  const h = (tag, attrs, ...children) => {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (k === 'class') el.className = v;
        else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k === 'html') el.innerHTML = v;
        else if (v != null && v !== false) el.setAttribute(k, v);
      }
    }
    for (const c of children.flat(Infinity)) {
      if (c == null || c === false) continue;
      el.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(c) : c);
    }
    return el;
  };
  const fmtBRL = (v) => 'R$ ' + (v || 0).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtDate = (d) => {
    if (!d) return '-';
    const dt = new Date(d + 'T00:00:00');
    return dt.toLocaleDateString('pt-BR');
  };
  const fmtDateShort = (d) => {
    if (!d) return '-';
    const dt = new Date(d + 'T00:00:00');
    return dt.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
  };
  const fmtDateTime = (iso) => {
    if (!iso) return '-';
    try {
      const d = new Date(String(iso).replace(' ', 'T'));
      if (isNaN(d.getTime())) return String(iso);
      const pad = n => String(n).padStart(2, '0');
      return pad(d.getDate()) + '/' + pad(d.getMonth() + 1) + '/' + d.getFullYear() + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    } catch (e) { return String(iso); }
  };
  const initials = (name) => {
    if (!name) return '?';
    return name.split(' ').slice(0, 2).map(n => n[0]).join('').toUpperCase();
  };
  const todayISO = () => new Date().toISOString().slice(0, 10);

  function toast(msg, type) {
    const t = h('div', { class: 'toast ' + (type || '') }, msg);
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 0.3s'; }, 2500);
    setTimeout(() => t.remove(), 3000);
  }

  // ---------------- ROUTING ----------------
  function go(view, params) {
    S.view = view;
    if (params) S.params = params;
    window.scrollTo(0, 0);
    render();
  }

  // ---------------- DATA LOADERS ----------------
  async function loadAll() {
    if (!S.token) return;
    try {
      // Pagina grande para carregar tudo de uma vez (escritorio local, dados modestos)
      const qs = '?page=1&page_size=500';
      const [clients, cases, tasks, events, transactions, documents, users] = await Promise.all([
        API.get('/api/clients' + qs),
        API.get('/api/cases' + qs),
        API.get('/api/tasks' + qs),
        API.get('/api/events' + qs),
        API.get('/api/transactions' + qs),
        API.get('/api/documents' + qs),
        API.get('/api/users'),
      ]);
      S.data.clients = clients.items || clients;
      S.data.cases = cases.items || cases;
      S.data.tasks = tasks.items || tasks;
      S.data.events = events.items || events;
      S.data.transactions = transactions.items || transactions;
      S.data.documents = documents.items || documents;
      S.data.users = users.items || users;
    } catch (e) { /* handled by API */ }
  }

  // ---------------- COMPONENTS ----------------

  function badge(text, type) {
    return h('span', { class: 'badge badge-' + (type || 'neutral') }, text);
  }

  function statusBadge(status) {
    const map = {
      em_andamento: ['Em andamento', 'info'],
      concluido: ['Concluido', 'success'],
      suspenso: ['Suspenso', 'warning'],
      cancelado: ['Cancelado', 'danger'],
      pendente: ['Pendente', 'warning'],
      pago: ['Pago', 'success'],
      atrasado: ['Atrasado', 'danger'],
    };
    const [txt, color] = map[status] || [status, 'neutral'];
    return badge(txt, color);
  }

  function priorityBadge(p) {
    const map = { alta: ['Alta', 'danger'], media: ['Media', 'warning'], baixa: ['Baixa', 'success'] };
    const [txt, color] = map[p] || [p, 'neutral'];
    return badge(txt, color);
  }

  // ---- LANDING ----
  function LandingPage() {
    const feature = (icon, title, desc) => h('div', { class: 'feature-card' },
      h('div', { class: 'feature-icon' }, icon),
      h('h3', null, title),
      h('p', null, desc)
    );
    return h('div', { class: 'landing' },
      h('div', { class: 'landing-nav' },
        h('div', { class: 'landing-logo' },
          h('div', { class: 'sidebar-logo' }, 'L'),
          h('span', { class: 'sidebar-name' }, 'LexFlow')
        ),
        h('div', null,
          h('button', { class: 'btn btn-ghost', style: { color: '#fff', borderColor: 'rgba(255,255,255,0.2)' }, onclick: () => go('auth', { tab: 'login' }) }, 'Entrar'),
          h('button', { class: 'btn btn-gold', onclick: () => go('auth', { tab: 'register' }), style: { marginLeft: '10px' } }, 'Criar conta')
        )
      ),
      h('div', { class: 'landing-hero' },
        h('div', { class: 'landing-eyebrow' }, 'GESTAO JURIDICA MODERNA'),
        h('h1', null, 'O escritorio de advocacia ', h('em', null, 'do futuro'), ' comeca aqui'),
        h('p', null, 'Plataforma completa para gestao de casos, clientes, financeiro, agenda e equipe. Tudo em um so lugar, com a elegancia que seu escritorio merece.'),
        h('div', { class: 'landing-cta' },
          h('button', { class: 'btn btn-gold btn-lg', onclick: () => go('auth', { tab: 'register' }) }, 'Comecar gratuitamente'),
          h('button', { class: 'btn btn-lg', style: { background: 'transparent', color: '#fff', border: '1px solid rgba(255,255,255,0.25)' }, onclick: () => go('auth', { tab: 'login' }) }, 'Ja tenho conta')
        )
      ),
      h('div', { class: 'landing-metrics' },
        h('div', { class: 'metric-card' }, h('div', { class: 'num' }, '500+'), h('div', { class: 'label' }, 'Escritorios')),
        h('div', { class: 'metric-card' }, h('div', { class: 'num' }, '98%'), h('div', { class: 'label' }, 'Aprovacao')),
        h('div', { class: 'metric-card' }, h('div', { class: 'num' }, '4.9'), h('div', { class: 'label' }, 'Avaliacao')),
        h('div', { class: 'metric-card' }, h('div', { class: 'num' }, '24/7'), h('div', { class: 'label' }, 'Disponivel'))
      ),
      h('div', { class: 'landing-section' },
        h('h2', null, 'Tudo que seu escritorio precisa'),
        h('p', { class: 'sub' }, 'Inspirado nas melhores plataformas do mercado. Construido para o advogado brasileiro.'),
        h('div', { class: 'features-grid' },
          feature('📋', 'Gestao de Casos', 'Numero CNJ, areas de atuacao, status, prioridades, andamentos e tarefas vinculadas.'),
          feature('👥', 'Clientes PF e PJ', 'Cadastro completo com vinculacao a casos, valores e historico financeiro.'),
          feature('💰', 'Financeiro completo', 'Receitas, despesas, fluxo de caixa mensal, status de pagamento e relatorios.'),
          feature('📅', 'Agenda integrada', 'Audiencias, prazos, reunioes e tarefas em um calendario mensal interativo.'),
          feature('📄', 'Documentos', 'Biblioteca organizada por caso e categoria, com busca rapida.'),
          feature('⚖️', 'Equipe', 'Advogados, paralegais e estagiarios com controle de funcoes e OAB.'),
          feature('📊', 'Dashboard executivo', 'KPIs em tempo real, graficos de receita e distribuicao por area.'),
          feature('🔒', 'Seguranca total', 'Seus dados ficam no seu computador. Sem nuvem, sem terceiros.'),
          feature('⚡', 'Rapido e simples', 'Sem instalacoes complexas. Abre no navegador e ja esta funcionando.')
        )
      ),
      h('div', { class: 'landing-section alt' },
        h('h2', null, 'Como funciona'),
        h('p', { class: 'sub' }, 'Tres passos para transformar a gestao do seu escritorio.'),
        h('div', { class: 'features-grid' },
          h('div', { class: 'feature-card' },
            h('div', { class: 'feature-icon' }, '1'),
            h('h3', null, 'Instale localmente'),
            h('p', null, 'Baixe, descompacte e clique em iniciar. Sem servidores externos, sem mensalidades.')
          ),
          h('div', { class: 'feature-card' },
            h('div', { class: 'feature-icon' }, '2'),
            h('h3', null, 'Cadastre sua equipe'),
            h('p', null, 'Crie contas para advogados e paralegais. Defina papeis e permissoes.')
          ),
          h('div', { class: 'feature-card' },
            h('div', { class: 'feature-icon' }, '3'),
            h('h3', null, 'Comece a gerenciar'),
            h('p', null, 'Cadastre clientes, casos, prazos. Veja tudo organizado em um dashboard executivo.')
          )
        )
      ),
      h('div', { class: 'landing-cta-band' },
        h('h2', null, 'Pronto para comecar?'),
        h('p', null, 'Crie sua conta agora e tenha controle total sobre seu escritorio.'),
        h('button', { class: 'btn btn-gold btn-lg', onclick: () => go('auth', { tab: 'register' }) }, 'Criar minha conta')
      ),
      h('div', { class: 'landing-footer' }, '© 2026 LexFlow. Sistema de gestao juridica. Feito com atencao para o advogado brasileiro.')
    );
  }

  // ---- AUTH ----
  function AuthPage() {
    const tab = S.params && S.params.tab || 'login';
    if (S.authTab !== tab) S.authTab = tab;

    const switchTab = (t) => { S.authTab = t; render(); };

    const onLogin = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        const r = await API.post('/api/auth/login', { email: fd.get('email'), password: fd.get('password') });
        S.token = r.token; S.csrf = r.csrf; S.user = r.user;
        if (r.user && r.user.theme) applyTheme(r.user.theme);
        localStorage.setItem('lexflow_token', r.token);
        localStorage.setItem('lexflow_csrf', r.csrf);
        await loadAll();
        toast('Bem-vindo, ' + S.user.name.split(' ')[0] + '!', 'success');
        go('dashboard');
      } catch (err) { toast(err.message, 'error'); }
    };

    const onRegister = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      if (fd.get('password') !== fd.get('password2')) return toast('As senhas nao coincidem', 'error');
      if ((fd.get('password') || '').length < 6) return toast('Senha deve ter ao menos 6 caracteres.', 'error');
      try {
        const r = await API.post('/api/auth/register', {
          name: fd.get('name'),
          email: fd.get('email'),
          password: fd.get('password'),
          role: fd.get('role') || 'Advogado',
          oab: fd.get('oab') || '',
          phone: fd.get('phone') || '',
        });
        S.token = r.token; S.csrf = r.csrf; S.user = r.user;
        localStorage.setItem('lexflow_token', r.token);
        localStorage.setItem('lexflow_csrf', r.csrf);
        await loadAll();
        toast('Conta criada com sucesso!', 'success');
        go('dashboard');
      } catch (err) { toast(err.message, 'error'); }
    };

    const loginForm = h('form', { onsubmit: onLogin },
      h('div', { class: 'form-group' }, h('label', null, 'E-mail'), h('input', { type: 'email', name: 'email', required: true, placeholder: 'seu@email.com' })),
      h('div', { class: 'form-group' }, h('label', null, 'Senha'), h('input', { type: 'password', name: 'password', required: true, placeholder: '••••••' })),
      h('button', { type: 'submit', class: 'btn btn-primary btn-block btn-lg', style: { marginTop: '8px' } }, 'Entrar'),
      h('p', { class: 'small muted text-center', style: { marginTop: '14px' } },
        'Contas demo: ',
        h('strong', null, 'helena@lexflow.demo'),
        ' / ',
        h('strong', null, '123456')
      )
    );

    const registerForm = h('form', { onsubmit: onRegister },
      h('div', { class: 'form-group' }, h('label', null, 'Nome completo'), h('input', { type: 'text', name: 'name', required: true })),
      h('div', { class: 'form-group' }, h('label', null, 'E-mail'), h('input', { type: 'email', name: 'email', required: true })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Senha'), h('input', { type: 'password', name: 'password', required: true, minlength: 4 })),
        h('div', { class: 'form-group' }, h('label', null, 'Confirmar'), h('input', { type: 'password', name: 'password2', required: true, minlength: 4 }))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Funcao'), h('select', { name: 'role' },
          h('option', null, 'Socio'), h('option', null, 'Advogado'),
          h('option', null, 'Paralegal'), h('option', null, 'Estagiario'))),
        h('div', { class: 'form-group' }, h('label', null, 'OAB (opcional)'), h('input', { type: 'text', name: 'oab' }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Telefone (opcional)'), h('input', { type: 'text', name: 'phone' })),
      h('button', { type: 'submit', class: 'btn btn-gold btn-block btn-lg' }, 'Criar conta')
    );

    return h('div', { class: 'auth-shell' },
      h('div', { class: 'auth-left' },
        h('div', { class: 'auth-brand' },
          h('div', { class: 'sidebar-logo' }, 'L'),
          h('span', { class: 'sidebar-name' }, 'LexFlow')
        ),
        h('div', null,
          h('h1', { class: 'auth-headline' }, 'A gestao juridica ', h('em', null, 'premium'), ' que seu escritorio merece.'),
          h('p', { style: { color: 'rgba(255,255,255,0.7)', marginTop: '20px', maxWidth: '440px' } },
            'Plataforma completa para gerenciar casos, clientes, financeiro, agenda e equipe com elegancia e eficiencia.')
        ),
        h('div', { class: 'auth-quote' },
          '"A melhor decisao que tomamos foi adotar o LexFlow. Tudo em um so lugar, com a seguranca que o escritorio precisa."',
          h('strong', null, '— Dra. Helena Coutinho, Socia')
        )
      ),
      h('div', { class: 'auth-right' },
        h('div', { class: 'auth-card' },
          h('h2', null, S.authTab === 'login' ? 'Bem-vindo de volta' : 'Criar sua conta'),
          h('p', { class: 'sub' }, S.authTab === 'login' ? 'Entre para acessar o sistema' : 'Preencha seus dados para comecar'),
          h('div', { class: 'auth-tabs' },
            h('div', { class: 'auth-tab ' + (S.authTab === 'login' ? 'active' : ''), onclick: () => switchTab('login') }, 'Entrar'),
            h('div', { class: 'auth-tab ' + (S.authTab === 'register' ? 'active' : ''), onclick: () => switchTab('register') }, 'Criar conta')
          ),
          S.authTab === 'login' ? loginForm : registerForm
        )
      )
    );
  }

  // ---- SHELL (sidebar + topbar) ----
  function AppShell(title, ...content) {
    const navItem = (id, icon, label) => h('div', {
      class: 'nav-item ' + (S.view === id || (id === 'cases' && S.view === 'case-detail') ? 'active' : ''),
      onclick: () => go(id)
    }, h('span', { class: 'nav-icon' }, icon), h('span', null, label));

    const onLogout = async () => {
      try { await API.post('/api/auth/logout'); } catch (e) {}
      S.token = null; S.user = null; S.csrf = null;
      localStorage.removeItem('lexflow_token');
      localStorage.removeItem('lexflow_csrf');
      S.view = 'landing';
      toast('Voce saiu do sistema', 'success');
      render();
    };

    return h('div', { class: 'app-shell' },
      h('div', { class: 'sidebar' },
        h('div', { class: 'sidebar-header' },
          h('div', { class: 'sidebar-logo' }, 'L'),
          h('div', null,
            h('div', { class: 'sidebar-name' }, 'LexFlow'),
            h('div', { class: 'sidebar-tag' }, 'Gestao Juridica')
          )
        ),
        h('div', { class: 'sidebar-nav' },
          h('div', { class: 'sidebar-section' }, 'Principal'),
          navItem('dashboard', '📊', 'Dashboard'),
          navItem('cases', '📋', 'Casos'),
          navItem('clients', '👥', 'Clientes'),
          h('div', { class: 'sidebar-section' }, 'Operacao'),
          navItem('agenda', '📅', 'Agenda'),
          navItem('kanban', '🎯', 'Kanban'),
          navItem('tasks', '✅', 'Tarefas'),
          navItem('documents', '📄', 'Documentos'),
          h('div', { class: 'sidebar-section' }, 'Gestao'),
          navItem('finance', '💰', 'Financeiro'),
          navItem('team', '⚖️', 'Equipe'),
          navItem('monitoring', '🔔', 'Monitoramento'),
          navItem('settings', '⚙️', 'Configuracoes'),
          (S.user && (S.user.role === 'Socio' || S.user.role === 'Advogado')) ? navItem('audit', '🔍', 'Auditoria') : null,
          navItem('trash', '🗑️', 'Lixeira')
        ),
        h('div', { class: 'sidebar-footer' },
          h('div', { class: 'user-avatar' }, initials(S.user && S.user.name)),
          h('div', { class: 'user-info' },
            h('div', { class: 'user-name' }, S.user ? S.user.name : ''),
            h('div', { class: 'user-role' }, S.user ? S.user.role : '')
          ),
          h('button', { class: 'logout-btn', title: 'Sair', onclick: onLogout }, '⏻')
        )
      ),
      h('div', { class: 'main' },
        h('div', { class: 'topbar' },
          h('h1', null, title),
          h('div', { class: 'topbar-actions' },
            h('button', { class: 'topbar-search', onclick: openSearch, title: 'Buscar (Ctrl+K)' },
              h('span', null, 'Buscar...'),
              h('span', { class: 'kbd' }, 'Ctrl K')
            ),
            h('button', { class: 'icon-btn', title: 'Notificacoes', onclick: toggleNotifications },
              h('span', null, '🔔'),
              S.notifications.items.length > 0 ? h('span', { class: 'badge-dot' }, String(S.notifications.items.length > 9 ? '9+' : S.notifications.items.length)) : null
            ),
            h('button', { class: 'icon-btn', title: S.theme === 'dark' ? 'Modo claro' : 'Modo escuro', onclick: toggleTheme },
              h('span', null, S.theme === 'dark' ? '☀️' : '🌙')
            )
          )
        ),
        NotificationsPanel(),
        h('div', { class: 'content' }, ...content)
      )
    );
  }

  // ---- DASHBOARD ----
  async function DashboardPage() {
    let d;
    try { d = await API.get('/api/dashboard'); } catch (e) { return h('div', null, 'Erro ao carregar'); }
    const k = d.kpi;

    const maxMonth = Math.max(...d.monthly.map(m => Math.max(m.receita, m.despesa)), 1);
    const barChart = h('div', { class: 'chart-bar' },
      ...d.monthly.map(m => h('div', { class: 'chart-col' },
        h('div', { class: 'chart-bars' },
          h('div', { class: 'chart-bar-item rec', style: { height: (m.receita / maxMonth * 180) + 'px' }, title: 'Receita: ' + fmtBRL(m.receita) }),
          h('div', { class: 'chart-bar-item des', style: { height: (m.despesa / maxMonth * 180) + 'px' }, title: 'Despesa: ' + fmtBRL(m.despesa) })
        ),
        h('div', { class: 'chart-label' }, m.month)
      ))
    );

    const totalByArea = d.by_area.reduce((s, x) => s + x.total, 0) || 1;
    const areaColors = ['#C9A96E', '#0F1B3D', '#2d6fbf', '#1f8a5b', '#c98c1d', '#b8364d', '#6c5ce7'];
    const donutBg = (() => {
      let acc = 0;
      return 'conic-gradient(' + d.by_area.map((x, i) => {
        const start = (acc / totalByArea) * 360;
        acc += x.total;
        const end = (acc / totalByArea) * 360;
        return areaColors[i % areaColors.length] + ' ' + start + 'deg ' + end + 'deg';
      }).join(', ') + ')';
    })();
    const donut = h('div', { class: 'donut-wrap' },
      h('div', { class: 'donut', style: { background: donutBg } },
        h('div', { class: 'donut-center' },
          h('div', { class: 'num' }, totalByArea),
          h('div', { class: 'lbl' }, 'Casos')
        )
      ),
      h('div', { class: 'donut-legend' },
        ...d.by_area.map((x, i) => h('div', { class: 'donut-legend-row' },
          h('span', { class: 'swatch', style: { background: areaColors[i % areaColors.length] } }),
          h('span', { class: 'name' }, x.area),
          h('span', { class: 'val' }, x.total)
        ))
      )
    );

    return AppShell('Dashboard',
      h('div', { class: 'kpi-grid' },
        h('div', { class: 'kpi' },
          h('div', { class: 'kpi-icon' }, '📋'),
          h('div', { class: 'kpi-label' }, 'Casos ativos'),
          h('div', { class: 'kpi-value' }, k.active_cases),
          h('div', { class: 'kpi-sub' }, k.total_cases + ' no total')
        ),
        h('div', { class: 'kpi' },
          h('div', { class: 'kpi-icon' }, '💰'),
          h('div', { class: 'kpi-label' }, 'Receita (pago)'),
          h('div', { class: 'kpi-value' }, fmtBRL(k.received_total)),
          h('div', { class: 'kpi-sub' }, 'A receber: ' + fmtBRL(k.pending_receivable))
        ),
        h('div', { class: 'kpi' },
          h('div', { class: 'kpi-icon' }, '⏱️'),
          h('div', { class: 'kpi-label' }, 'Tarefas pendentes'),
          h('div', { class: 'kpi-value' }, k.pending_tasks),
          h('div', { class: 'kpi-sub' }, k.overdue_tasks + ' atrasadas')
        ),
        h('div', { class: 'kpi' },
          h('div', { class: 'kpi-icon' }, '📅'),
          h('div', { class: 'kpi-label' }, 'Eventos hoje'),
          h('div', { class: 'kpi-value' }, k.today_events),
          h('div', { class: 'kpi-sub' }, k.upcoming_deadlines + ' prazos em 15 dias')
        )
      ),
      h('div', { class: 'grid-2-1' },
        h('div', { class: 'card' },
          h('div', { class: 'card-header' },
            h('div', null, h('h3', null, 'Receitas vs Despesas'), h('div', { class: 'sub' }, 'Ultimos 6 meses'))
          ),
          barChart,
          h('div', { class: 'chart-legend' },
            h('span', null, h('span', { class: 'legend-dot', style: { background: '#C9A96E' } }), 'Receitas'),
            h('span', null, h('span', { class: 'legend-dot', style: { background: '#2a3a73' } }), 'Despesas')
          )
        ),
        h('div', { class: 'card' },
          h('div', { class: 'card-header' }, h('h3', null, 'Casos por area')),
          donut
        )
      ),
      h('div', { class: 'grid-1-2', style: { marginTop: '18px' } },
        h('div', { class: 'card' },
          h('div', { class: 'card-header' },
            h('h3', null, 'Proximos prazos'),
            h('button', { class: 'btn btn-sm btn-ghost', onclick: () => go('agenda') }, 'Ver agenda')
          ),
          d.upcoming_deadlines.length === 0
            ? h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, '✓'), h('h3', null, 'Nenhum prazo proximo'), h('p', null, 'Voce esta em dia!'))
            : h('div', null,
                ...d.upcoming_deadlines.map(c => h('div', { class: 'list-item', onclick: () => go('case-detail', { id: c.id }) },
                  h('div', { class: 'body' },
                    h('div', { class: 'title' }, c.title),
                    h('div', { class: 'meta' }, fmtDate(c.next_deadline) + ' • ' + (c.area || 'Sem area'))
                  ),
                  statusBadge(c.status)
                ))
              )
        ),
        h('div', { class: 'card' },
          h('div', { class: 'card-header' },
            h('h3', null, 'Tarefas pendentes'),
            h('button', { class: 'btn btn-sm btn-ghost', onclick: () => go('tasks') }, 'Ver todas')
          ),
          d.pending_tasks.length === 0
            ? h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, '✓'), h('h3', null, 'Tudo em dia!'), h('p', null, 'Nenhuma tarefa pendente'))
            : h('div', null,
                ...d.pending_tasks.slice(0, 5).map(t => h('div', { class: 'list-item' },
                  h('div', { class: 'check', onclick: () => toggleTask(t.id, t.status) }, t.status === 'concluida' ? '✓' : ''),
                  h('div', { class: 'body' },
                    h('div', { class: 'title ' + (t.status === 'concluida' ? 'done' : '') }, t.title),
                    h('div', { class: 'meta' }, (t.due_date ? 'Vence em ' + fmtDate(t.due_date) : 'Sem prazo') + (t.case_id ? ' • Caso' : ''))
                  ),
                  priorityBadge(t.priority)
                ))
              )
        )
      )
    );
  }

  async function toggleTask(id, status) {
    const newStatus = status === 'concluida' ? 'pendente' : 'concluida';
    try {
      await API.put('/api/tasks/' + id, { status: newStatus });
      await loadAll();
      render();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---- CASES LIST ----
  async function CasesPage() {
    const cases = S.data.cases;
    const f = S.filters.cases || {};
    let list = cases.slice();
    if (f.search) list = list.filter(c => (c.title + ' ' + (c.code || '') + ' ' + (c.client_name || '')).toLowerCase().includes(f.search.toLowerCase()));
    if (f.area) list = list.filter(c => c.area === f.area);
    if (f.status) list = list.filter(c => c.status === f.status);

    const areas = [...new Set(cases.map(c => c.area).filter(Boolean))];

    const onNew = () => openCaseModal();

    return AppShell('Casos',
      h('div', { class: 'filters' },
        h('input', { type: 'text', placeholder: 'Buscar por titulo, codigo ou cliente...', value: f.search || '', oninput: (e) => { S.filters.cases = { ...S.filters.cases, search: e.target.value }; render(); } }),
        h('select', { onchange: (e) => { S.filters.cases = { ...S.filters.cases, area: e.target.value }; render(); } },
          h('option', { value: '' }, 'Todas as areas'),
          ...areas.map(a => h('option', { value: a, selected: f.area === a }, a))
        ),
        h('select', { onchange: (e) => { S.filters.cases = { ...S.filters.cases, status: e.target.value }; render(); } },
          h('option', { value: '' }, 'Todos os status'),
          h('option', { value: 'em_andamento', selected: f.status === 'em_andamento' }, 'Em andamento'),
          h('option', { value: 'concluido', selected: f.status === 'concluido' }, 'Concluido'),
          h('option', { value: 'suspenso', selected: f.status === 'suspenso' }, 'Suspenso'),
          h('option', { value: 'cancelado', selected: f.status === 'cancelado' }, 'Cancelado')
        ),
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn btn-primary', onclick: onNew }, '+ Novo caso')
      ),
      h('div', { class: 'card' },
        list.length === 0
          ? h('div', { class: 'empty' },
              h('div', { class: 'empty-icon' }, '📋'),
              h('h3', null, 'Nenhum caso encontrado'),
              h('p', null, 'Cadastre seu primeiro caso para comecar.'),
              h('button', { class: 'btn btn-primary', onclick: onNew }, '+ Cadastrar caso')
            )
          : h('table', { class: 'table' },
              h('thead', null, h('tr', null,
                h('th', null, 'Codigo'),
                h('th', null, 'Titulo / Cliente'),
                h('th', null, 'Area'),
                h('th', null, 'Status'),
                h('th', null, 'Prioridade'),
                h('th', { class: 'text-right' }, 'Valor'),
                h('th', null, 'Prazo')
              )),
              h('tbody', null,
                ...list.map(c => {
                  const client = S.data.clients.find(cl => cl.id === c.client_id);
                  return h('tr', { onclick: () => go('case-detail', { id: c.id }) },
                    h('td', { class: 'small muted' }, c.code || '-'),
                    h('td', null,
                      h('div', { class: 'strong' }, c.title),
                      h('div', { class: 'small muted' }, client ? client.name : 'Sem cliente')
                    ),
                    h('td', null, badge(c.area, 'navy')),
                    h('td', null, statusBadge(c.status)),
                    h('td', null, priorityBadge(c.priority)),
                    h('td', { class: 'text-right strong' }, fmtBRL(c.value)),
                    h('td', { class: 'small' }, c.next_deadline ? fmtDateShort(c.next_deadline) : '-')
                  );
                })
              )
            )
      )
    );
  }

  function openCaseModal(c) {
    const isEdit = !!c;
    const m = h('form', { id: 'case-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        code: fd.get('code'),
        title: fd.get('title'),
        client_id: fd.get('client_id') || null,
        area: fd.get('area'),
        status: fd.get('status'),
        priority: fd.get('priority'),
        value: parseFloat(fd.get('value')) || 0,
        court: fd.get('court'),
        opposing_party: fd.get('opposing_party'),
        description: fd.get('description'),
        next_deadline: fd.get('next_deadline') || null,
        responsible_id: fd.get('responsible_id') || null,
        tags: fd.get('tags'),
      };
      try {
        if (isEdit) await API.put('/api/cases/' + c.id, data);
        else await API.post('/api/cases', data);
        await loadAll();
        closeModal();
        render();
        toast(isEdit ? 'Caso atualizado' : 'Caso criado', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-group' }, h('label', null, 'Titulo *'), h('input', { type: 'text', name: 'title', required: true, value: (c && c.title) || '' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Numero CNJ'), h('input', { type: 'text', name: 'code', value: (c && c.code) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Area *'), h('select', { name: 'area', required: true },
          h('option', { value: 'Civel', selected: !c || c.area === 'Civel' }, 'Civel'),
          h('option', { value: 'Trabalhista', selected: c && c.area === 'Trabalhista' }, 'Trabalhista'),
          h('option', { value: 'Familia', selected: c && c.area === 'Familia' }, 'Familia'),
          h('option', { value: 'Empresarial', selected: c && c.area === 'Empresarial' }, 'Empresarial'),
          h('option', { value: 'Sucessoes', selected: c && c.area === 'Sucessoes' }, 'Sucessoes'),
          h('option', { value: 'Tributario', selected: c && c.area === 'Tributario' }, 'Tributario'),
          h('option', { value: 'Penal', selected: c && c.area === 'Penal' }, 'Penal'),
          h('option', { value: 'Previdenciario', selected: c && c.area === 'Previdenciario' }, 'Previdenciario')
        ))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Cliente'), h('select', { name: 'client_id' },
          h('option', { value: '' }, 'Selecione...'),
          ...S.data.clients.map(cl => h('option', { value: cl.id, selected: c && c.client_id === cl.id }, cl.name))
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Responsavel'), h('select', { name: 'responsible_id' },
          h('option', { value: '' }, 'Selecione...'),
          ...S.data.users.map(u => h('option', { value: u.id, selected: c && c.responsible_id === u.id }, u.name))
        ))
      ),
      h('div', { class: 'form-row-3' },
        h('div', { class: 'form-group' }, h('label', null, 'Status'), h('select', { name: 'status' },
          h('option', { value: 'em_andamento', selected: !c || c.status === 'em_andamento' }, 'Em andamento'),
          h('option', { value: 'concluido', selected: c && c.status === 'concluido' }, 'Concluido'),
          h('option', { value: 'suspenso', selected: c && c.status === 'suspenso' }, 'Suspenso'),
          h('option', { value: 'cancelado', selected: c && c.status === 'cancelado' }, 'Cancelado')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Prioridade'), h('select', { name: 'priority' },
          h('option', { value: 'baixa', selected: c && c.priority === 'baixa' }, 'Baixa'),
          h('option', { value: 'media', selected: !c || c.priority === 'media' }, 'Media'),
          h('option', { value: 'alta', selected: c && c.priority === 'alta' }, 'Alta')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Valor (R$)'), h('input', { type: 'number', name: 'value', step: '0.01', value: (c && c.value) || 0 }))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Vara / Tribunal'), h('input', { type: 'text', name: 'court', value: (c && c.court) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Parte contraria'), h('input', { type: 'text', name: 'opposing_party', value: (c && c.opposing_party) || '' }))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Proximo prazo'), h('input', { type: 'date', name: 'next_deadline', value: (c && c.next_deadline) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Tags (separadas por virgula)'), h('input', { type: 'text', name: 'tags', value: (c && c.tags) || '' }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Descricao'), h('textarea', { name: 'description' }, (c && c.description) || ''))
    );

    S.modal = {
      title: isEdit ? 'Editar caso' : 'Novo caso',
      body: m,
      footer: h('span', null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'case-form' }, isEdit ? 'Salvar' : 'Criar caso')
      )
    };
    render();
  }

  // ---- CASE DETAIL ----
  async function CaseDetailPage() {
    const cid = S.params && S.params.id;
    let c;
    try { c = await API.get('/api/cases/' + cid); } catch (e) { return AppShell('Caso nao encontrado', h('p', null, 'O caso solicitado nao foi encontrado.')); }

  const onToggleMonitor = async () => {
    if (!S.user) return;
    try {
      // Verifica status atual
      const st = await API.req('GET', '/api/monitoring/status');
      const existing = (st.items || []).find(i => i.case_id === cid);
      const isActive = existing && existing.status === 'active';
      const interval = prompt('Intervalo em minutos (5-1440):', existing ? String(existing.interval_minutes) : '60');
      if (interval === null) return;
      const min = Math.max(5, Math.min(1440, parseInt(interval) || 60));
      const newStatus = isActive ? 'paused' : 'active';
      await API.req('POST', '/api/cases/' + cid + '/monitor', { status: newStatus, interval_minutes: min });
      toast(newStatus === 'active' ? 'Monitoramento ativado (checagem a cada ' + min + ' min)' : 'Monitoramento pausado', 'ok');
    } catch (e) {
      toast('Erro: ' + e.message, 'err');
    }
  };

    let updates = [];
    try { updates = await API.get('/api/cases/' + cid + '/updates'); } catch (e) {}
    const client = S.data.clients.find(cl => cl.id === c.client_id);
    const responsible = S.data.users.find(u => u.id === c.responsible_id);
    const caseTasks = S.data.tasks.filter(t => t.case_id === cid);
    const caseEvents = S.data.events.filter(e => e.case_id === cid);
    const caseDocs = S.data.documents.filter(d => d.case_id === cid);

    const onAddUpdate = () => {
      const m = h('form', { id: 'upd-form', onsubmit: async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          await API.post('/api/cases/' + cid + '/updates', {
            date: fd.get('date') || todayISO(),
            title: fd.get('title'),
            description: fd.get('description'),
            type: fd.get('type'),
          });
          closeModal();
          render();
          toast('Andamento registrado', 'success');
        } catch (err) { toast(err.message, 'error'); }
      }},
        h('div', { class: 'form-row' },
          h('div', { class: 'form-group' }, h('label', null, 'Data'), h('input', { type: 'date', name: 'date', value: todayISO() })),
          h('div', { class: 'form-group' }, h('label', null, 'Tipo'), h('select', { name: 'type' },
            h('option', { value: 'andamento' }, 'Andamento'),
            h('option', { value: 'audiencia' }, 'Audiencia'),
            h('option', { value: 'despacho' }, 'Despacho'),
            h('option', { value: 'peticao' }, 'Peticao')
          ))
        ),
        h('div', { class: 'form-group' }, h('label', null, 'Titulo *'), h('input', { type: 'text', name: 'title', required: true })),
        h('div', { class: 'form-group' }, h('label', null, 'Descricao'), h('textarea', { name: 'description' }))
      );
      S.modal = {
        title: 'Novo andamento',
        body: m,
        footer: h('span', null,
          h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
          h('button', { class: 'btn btn-primary', type: 'submit', form: 'upd-form' }, 'Adicionar')
        )
      };
      render();
    };

    const onDelete = async () => {
      if (!confirm('Tem certeza que deseja excluir este caso?')) return;
      try { await API.del('/api/cases/' + cid); await loadAll(); toast('Caso excluido', 'success'); go('cases'); }
      catch (err) { toast(err.message, 'error'); }
    };

    return AppShell(c.title,
      h('div', { class: 'mb-3' },
        h('button', { class: 'btn btn-ghost btn-sm', onclick: () => go('cases') }, '← Voltar para casos')
      ),
      h('div', { class: 'grid-2-1' },
        h('div', null,
          h('div', { class: 'card mb-3' },
            h('div', { class: 'card-header' },
              h('div', null, h('h3', null, 'Informacoes do caso'), h('div', { class: 'sub' }, c.code || 'Sem numero CNJ')),
              h('div', { class: 'flex gap-2' },
                h('button', { class: 'btn btn-sm btn-ghost', onclick: () => openCaseModal(c) }, '✏️ Editar'),
                h('button', { class: 'btn btn-sm btn-danger', onclick: onDelete }, '🗑 Excluir')
              )
            ),
            h('div', { class: 'grid-2' },
              h('div', null,
                h('div', { class: 'mb-2' }, h('label', null, 'Cliente'), h('div', { class: 'strong' }, client ? client.name : 'Sem cliente')),
                h('div', { class: 'mb-2' }, h('label', null, 'Area'), h('div', null, badge(c.area, 'navy'))),
                h('div', { class: 'mb-2' }, h('label', null, 'Status'), h('div', null, statusBadge(c.status))),
                h('div', { class: 'mb-2' }, h('label', null, 'Prioridade'), h('div', null, priorityBadge(c.priority))),
                h('div', { class: 'mb-2' }, h('label', null, 'Responsavel'), h('div', null, responsible ? responsible.name : 'Nao definido'))
              ),
              h('div', null,
                h('div', { class: 'mb-2' }, h('label', null, 'Vara / Tribunal'), h('div', null, c.court || '-')),
                h('div', { class: 'mb-2' }, h('label', null, 'Parte contraria'), h('div', null, c.opposing_party || '-')),
                h('div', { class: 'mb-2' }, h('label', null, 'Valor da causa'), h('div', { class: 'strong' }, fmtBRL(c.value))),
                h('div', { class: 'mb-2' }, h('label', null, 'Proximo prazo'), h('div', null, c.next_deadline ? fmtDate(c.next_deadline) : 'Sem prazo')),
                c.tags ? h('div', { class: 'mb-2' }, h('label', null, 'Tags'),
                  ...c.tags.split(',').map(t => h('span', { class: 'badge badge-neutral', style: { marginRight: '4px' } }, t.trim()))
                ) : null
              )
            ),
            c.description ? h('div', { style: { marginTop: '14px', paddingTop: '14px', borderTop: '1px solid var(--line-2)' } },
              h('label', null, 'Descricao'), h('p', { style: { color: 'var(--ink-2)', fontSize: '14px' } }, c.description)
            ) : null
          ),
          h('div', { class: 'card mb-3' },
            h('div', { class: 'card-header' },
              h('h3', null, 'Tarefas (' + caseTasks.length + ')'),
              h('button', { class: 'btn btn-sm btn-primary', onclick: () => openTaskModal(cid) }, '+ Tarefa')
            ),
            caseTasks.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nenhuma tarefa vinculada'))
              : h('div', null, ...caseTasks.map(t => h('div', { class: 'list-item' },
                h('div', { class: 'check ' + (t.status === 'concluida' ? 'done' : ''), onclick: () => toggleTask(t.id, t.status) }, t.status === 'concluida' ? '✓' : ''),
                h('div', { class: 'body' },
                  h('div', { class: 'title ' + (t.status === 'concluida' ? 'done' : '') }, t.title),
                  h('div', { class: 'meta' }, (t.due_date ? 'Vence ' + fmtDate(t.due_date) : 'Sem prazo') + ' • ' + (S.data.users.find(u => u.id === t.responsible_id) || {}).name || '')
                ),
                priorityBadge(t.priority)
              )))
          ),
          h('div', { class: 'card' },
            h('div', { class: 'card-header' },
              h('h3', null, 'Documentos (' + caseDocs.length + ')'),
              h('button', { class: 'btn btn-sm btn-primary', onclick: () => openDocumentModal(cid) }, '+ Documento')
            ),
            caseDocs.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nenhum documento anexado'))
              : h('table', { class: 'table' },
                  h('thead', null, h('tr', null, h('th', null, 'Documento'), h('th', null, 'Categoria'), h('th', null, 'Data'), h('th', null, 'Tam.'))),
                  h('tbody', null, ...caseDocs.map(d => h('tr', null,
                    h('td', null, h('div', { class: 'strong' }, d.title), d.notes ? h('div', { class: 'small muted' }, d.notes) : null),
                    h('td', null, badge(d.category || '-', 'gold')),
                    h('td', { class: 'small' }, fmtDate(d.date)),
                    h('td', { class: 'small muted' }, d.size || '-')
                  )))
                )
          )
        ),
        h('div', null,
          h('div', { class: 'card mb-3' },
            h('div', { class: 'card-header' },
              h('h3', null, 'Linha do tempo'),
              h('div', { style: 'display:flex;gap:6px' },
                h('button', { class: 'btn btn-sm btn-primary', onclick: onAddUpdate }, '+ Andamento'),
                h('button', { class: 'btn btn-sm', id: 'btn-monitor-case', onclick: onToggleMonitor, style: 'background:#f1f5f9;color:#1e293b;border:1px solid #cbd5e1' }, '🔔 Monitorar...')
              )
            ),
            updates.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nenhum andamento registrado'))
              : h('div', { class: 'timeline' }, ...updates.map(u => h('div', { class: 'timeline-item' },
                h('div', { class: 'timeline-date' }, fmtDate(u.date) + ' • ' + (u.type || 'andamento')),
                h('div', { class: 'timeline-title' }, u.title),
                u.description ? h('div', { class: 'timeline-desc' }, u.description) : null
              )))
          ),
          h('div', { class: 'card' },
            h('div', { class: 'card-header' }, h('h3', null, 'Eventos')),
            caseEvents.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nenhum evento'))
              : h('div', null, ...caseEvents.map(e => h('div', { class: 'list-item' },
                h('div', { class: 'body' },
                  h('div', { class: 'title' }, e.title),
                  h('div', { class: 'meta' }, fmtDate(e.date) + ' ' + (e.time || '') + ' • ' + (e.location || 'Sem local'))
                ),
                badge(e.type, e.type === 'audiencia' ? 'danger' : e.type === 'prazo' ? 'warning' : 'info')
              )))
          )
        )
      )
    );
  }

  function openTaskModal(caseId) {
    const m = h('form', { id: 'task-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        await API.post('/api/tasks', {
          title: fd.get('title'),
          description: fd.get('description'),
          case_id: caseId || fd.get('case_id') || null,
          responsible_id: fd.get('responsible_id') || null,
          priority: fd.get('priority'),
          status: 'pendente',
          due_date: fd.get('due_date') || null,
        });
        await loadAll(); closeModal(); render(); toast('Tarefa criada', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-group' }, h('label', null, 'Titulo *'), h('input', { type: 'text', name: 'title', required: true })),
      h('div', { class: 'form-group' }, h('label', null, 'Descricao'), h('textarea', { name: 'description' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Responsavel'), h('select', { name: 'responsible_id' },
          h('option', { value: '' }, 'Selecione...'),
          ...S.data.users.map(u => h('option', { value: u.id }, u.name))
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Prioridade'), h('select', { name: 'priority' },
          h('option', { value: 'baixa' }, 'Baixa'),
          h('option', { value: 'media', selected: true }, 'Media'),
          h('option', { value: 'alta' }, 'Alta')
        ))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Prazo'), h('input', { type: 'date', name: 'due_date' })),
        !caseId ? h('div', { class: 'form-group' }, h('label', null, 'Caso (opcional)'), h('select', { name: 'case_id' },
          h('option', { value: '' }, 'Sem caso'),
          ...S.data.cases.map(c => h('option', { value: c.id }, c.title))
        )) : h('div', null)
      )
    );
    S.modal = {
      title: 'Nova tarefa',
      body: m,
      footer: h('span', null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'task-form' }, 'Criar')
      )
    };
    render();
  }

  function openDocumentModal(caseId) {
    const m = h('form', { id: 'doc-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        await API.post('/api/documents', {
          title: fd.get('title'),
          case_id: caseId || fd.get('case_id') || null,
          category: fd.get('category'),
          type: fd.get('type'),
          size: fd.get('size'),
          date: fd.get('date') || todayISO(),
          responsible_id: S.user.id,
          notes: fd.get('notes'),
        });
        await loadAll(); closeModal(); render(); toast('Documento adicionado', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-group' }, h('label', null, 'Titulo *'), h('input', { type: 'text', name: 'title', required: true })),
      !caseId ? h('div', { class: 'form-group' }, h('label', null, 'Caso'), h('select', { name: 'case_id' },
        h('option', { value: '' }, 'Sem caso'),
        ...S.data.cases.map(c => h('option', { value: c.id }, c.title))
      )) : null,
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Categoria'), h('select', { name: 'category' },
          h('option', null, 'Peticao'), h('option', null, 'Contrato'),
          h('option', null, 'Procuração'), h('option', null, 'Parecer'),
          h('option', null, 'Documento')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Tipo'), h('select', { name: 'type' },
          h('option', null, 'PDF'), h('option', null, 'DOCX'),
          h('option', null, 'XLSX'), h('option', null, 'JPG'),
          h('option', null, 'PNG')
        ))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Tamanho'), h('input', { type: 'text', name: 'size', placeholder: 'Ex: 1.2 MB' })),
        h('div', { class: 'form-group' }, h('label', null, 'Data'), h('input', { type: 'date', name: 'date', value: todayISO() }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Observacoes'), h('textarea', { name: 'notes' }))
    );
    S.modal = {
      title: 'Novo documento',
      body: m,
      footer: h('span', null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'doc-form' }, 'Adicionar')
      )
    };
    render();
  }

  // ---- CLIENTS ----
  function ClientsPage() {
    const list = S.data.clients;
    const openNew = () => openClientModal();
    return AppShell('Clientes',
      h('div', { class: 'filters' },
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn btn-primary', onclick: openNew }, '+ Novo cliente')
      ),
      list.length === 0
        ? h('div', { class: 'card' },
            h('div', { class: 'empty' },
              h('div', { class: 'empty-icon' }, '👥'),
              h('h3', null, 'Nenhum cliente cadastrado'),
              h('p', null, 'Cadastre seu primeiro cliente para comecar.'),
              h('button', { class: 'btn btn-primary', onclick: openNew }, '+ Cadastrar cliente')
            )
          )
        : h('div', { class: 'grid-3' },
            ...list.map(cl => {
              const caseCount = S.data.cases.filter(c => c.client_id === cl.id).length;
              const totals = S.data.transactions.filter(t => t.client_id === cl.id);
              const paid = totals.filter(t => t.status === 'pago' && t.type === 'receita').reduce((s, t) => s + t.amount, 0);
              const pending = totals.filter(t => t.status === 'pendente' && t.type === 'receita').reduce((s, t) => s + t.amount, 0);
              return h('div', { class: 'client-card' },
                h('div', { class: 'client-avatar' }, initials(cl.name)),
                h('div', { class: 'client-name' }, cl.name),
                h('div', { class: 'client-meta' },
                  badge(cl.type === 'pf' ? 'Pessoa Fisica' : 'Pessoa Juridica', cl.type === 'pf' ? 'info' : 'gold'),
                  cl.document ? h('span', { style: { marginLeft: '6px' } }, cl.document) : null
                ),
                h('div', { class: 'small muted' }, cl.email || ''),
                h('div', { class: 'small muted' }, cl.phone || ''),
                h('div', { class: 'client-stats' },
                  h('div', { class: 'client-stat' }, h('div', { class: 'lbl' }, 'Casos'), h('div', { class: 'val' }, caseCount)),
                  h('div', { class: 'client-stat' }, h('div', { class: 'lbl' }, 'Pago'), h('div', { class: 'val' }, fmtBRL(paid))),
                  h('div', { class: 'client-stat' }, h('div', { class: 'lbl' }, 'A receber'), h('div', { class: 'val' }, fmtBRL(pending)))
                ),
                h('div', { style: { marginTop: '10px' } },
                  h('button', { class: 'btn btn-sm btn-ghost', onclick: () => openClientModal(cl) }, '✏️ Editar')
                )
              );
            })
          )
    );
  }

  function openClientModal(c) {
    const isEdit = !!c;
    const m = h('form', { id: 'client-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        type: fd.get('type'),
        name: fd.get('name'),
        document: fd.get('document'),
        email: fd.get('email'),
        phone: fd.get('phone'),
        address: fd.get('address'),
        notes: fd.get('notes'),
      };
      try {
        if (isEdit) await API.put('/api/clients/' + c.id, data);
        else await API.post('/api/clients', data);
        await loadAll(); closeModal(); render(); toast(isEdit ? 'Cliente atualizado' : 'Cliente criado', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Tipo'), h('select', { name: 'type' },
          h('option', { value: 'pf', selected: !c || c.type === 'pf' }, 'Pessoa Fisica'),
          h('option', { value: 'pj', selected: c && c.type === 'pj' }, 'Pessoa Juridica')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Documento (CPF/CNPJ)'), h('input', { type: 'text', name: 'document', value: (c && c.document) || '' }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Nome / Razao social *'), h('input', { type: 'text', name: 'name', required: true, value: (c && c.name) || '' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'E-mail'), h('input', { type: 'email', name: 'email', value: (c && c.email) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Telefone'), h('input', { type: 'text', name: 'phone', value: (c && c.phone) || '' }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Endereco'), h('input', { type: 'text', name: 'address', value: (c && c.address) || '' })),
      h('div', { class: 'form-group' }, h('label', null, 'Observacoes'), h('textarea', { name: 'notes' }, (c && c.notes) || ''))
    );
    S.modal = {
      title: isEdit ? 'Editar cliente' : 'Novo cliente',
      body: m,
      footer: h('span', null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'client-form' }, isEdit ? 'Salvar' : 'Criar cliente')
      )
    };
    render();
  }

  // ---- AGENDA ----
  function AgendaPage() {
    const now = new Date();
    if (!S.cal.year) {
      S.cal.year = now.getFullYear();
      S.cal.month = now.getMonth();
    }

    const prev = () => {
      S.cal.month--;
      if (S.cal.month < 0) { S.cal.month = 11; S.cal.year--; }
      render();
    };
    const next = () => {
      S.cal.month++;
      if (S.cal.month > 11) { S.cal.month = 0; S.cal.year++; }
      render();
    };
    const today = () => { S.cal = { year: now.getFullYear(), month: now.getMonth() }; render(); };

    const monthName = new Date(S.cal.year, S.cal.month, 1).toLocaleDateString('pt-BR', { month: 'long', year: 'numeric' });

    const firstDay = new Date(S.cal.year, S.cal.month, 1);
    const lastDay = new Date(S.cal.year, S.cal.month + 1, 0);
    const startOffset = firstDay.getDay();
    const totalDays = lastDay.getDate();

    const todayDate = now.getDate();
    const todayMonth = now.getMonth();
    const todayYear = now.getFullYear();

    const days = [];
    const prevMonthLast = new Date(S.cal.year, S.cal.month, 0).getDate();
    for (let i = startOffset - 1; i >= 0; i--) {
      days.push({ num: prevMonthLast - i, otherMonth: true });
    }
    for (let d = 1; d <= totalDays; d++) {
      const isToday = d === todayDate && S.cal.month === todayMonth && S.cal.year === todayYear;
      const iso = S.cal.year + '-' + String(S.cal.month + 1).padStart(2, '0') + '-' + String(d).padStart(2, '0');
      const events = S.data.events.filter(e => e.date === iso);
      const taskDues = S.data.tasks.filter(t => t.due_date === iso && t.status === 'pendente');
      days.push({ num: d, iso, isToday, events, taskDues });
    }
    while (days.length % 7 !== 0) days.push({ num: days.length - startOffset - totalDays + 1, otherMonth: true });

    const onDayClick = (day) => {
      if (!day.iso) return;
      openEventModal(day.iso);
    };

    const onNewEvent = () => openEventModal(todayISO());

    return AppShell('Agenda',
      h('div', { class: 'cal-month' },
        h('div', null, h('h2', null, monthName)),
        h('div', { class: 'flex gap-2' },
          h('button', { class: 'btn btn-ghost btn-sm', onclick: prev }, '← Anterior'),
          h('button', { class: 'btn btn-ghost btn-sm', onclick: today }, 'Hoje'),
          h('button', { class: 'btn btn-ghost btn-sm', onclick: next }, 'Proximo →'),
          h('button', { class: 'btn btn-primary btn-sm', onclick: onNewEvent, style: { marginLeft: '8px' } }, '+ Novo evento')
        )
      ),
      h('div', { class: 'card' },
        h('div', { class: 'cal-grid' },
          h('div', { class: 'cal-weekday' }, 'Dom'),
          h('div', { class: 'cal-weekday' }, 'Seg'),
          h('div', { class: 'cal-weekday' }, 'Ter'),
          h('div', { class: 'cal-weekday' }, 'Qua'),
          h('div', { class: 'cal-weekday' }, 'Qui'),
          h('div', { class: 'cal-weekday' }, 'Sex'),
          h('div', { class: 'cal-weekday' }, 'Sab')
        ),
        h('div', { class: 'cal-grid' },
          ...days.map(day => h('div', {
            class: 'cal-day' + (day.otherMonth ? ' other-month' : '') + (day.isToday ? ' today' : ''),
            onclick: () => onDayClick(day)
          },
            h('div', { class: 'cal-day-num' }, day.num),
            day.events ? day.events.slice(0, 3).map(e => h('div', { class: 'cal-event ' + e.type, title: e.title }, (e.time ? e.time + ' ' : '') + e.title)) : null,
            day.taskDues ? day.taskDues.slice(0, 2).map(t => h('div', { class: 'cal-event tarefa' }, '✓ ' + t.title)) : null
          ))
        )
      )
    );
  }

  function openEventModal(date, e) {
    const isEdit = !!e;
    const m = h('form', { id: 'event-form', onsubmit: async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const data = {
        title: fd.get('title'),
        type: fd.get('type'),
        date: fd.get('date'),
        time: fd.get('time') || null,
        duration: parseInt(fd.get('duration')) || null,
        case_id: fd.get('case_id') || null,
        location: fd.get('location'),
        notes: fd.get('notes'),
        responsible_id: fd.get('responsible_id') || null,
      };
      try {
        if (isEdit) await API.put('/api/events/' + e.id, data);
        else await API.post('/api/events', data);
        await loadAll(); closeModal(); render(); toast(isEdit ? 'Evento atualizado' : 'Evento criado', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-group' }, h('label', null, 'Titulo *'), h('input', { type: 'text', name: 'title', required: true, value: (e && e.title) || '' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Tipo'), h('select', { name: 'type' },
          h('option', { value: 'audiencia', selected: e && e.type === 'audiencia' }, 'Audiencia'),
          h('option', { value: 'reuniao', selected: !e || e.type === 'reuniao' }, 'Reuniao'),
          h('option', { value: 'prazo', selected: e && e.type === 'prazo' }, 'Prazo'),
          h('option', { value: 'tarefa', selected: e && e.type === 'tarefa' }, 'Tarefa')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Data *'), h('input', { type: 'date', name: 'date', required: true, value: (e && e.date) || date || todayISO() }))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Horario'), h('input', { type: 'time', name: 'time', value: (e && e.time) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Duracao (min)'), h('input', { type: 'number', name: 'duration', value: (e && e.duration) || 60 }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Caso (opcional)'), h('select', { name: 'case_id' },
        h('option', { value: '' }, 'Sem caso'),
        ...S.data.cases.map(c => h('option', { value: c.id, selected: e && e.case_id === c.id }, c.title))
      )),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Local'), h('input', { type: 'text', name: 'location', value: (e && e.location) || '' })),
        h('div', { class: 'form-group' }, h('label', null, 'Responsavel'), h('select', { name: 'responsible_id' },
          h('option', { value: '' }, 'Selecione...'),
          ...S.data.users.map(u => h('option', { value: u.id, selected: e && e.responsible_id === u.id }, u.name))
        ))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Observacoes'), h('textarea', { name: 'notes' }, (e && e.notes) || ''))
    );
    S.modal = {
      title: isEdit ? 'Editar evento' : 'Novo evento',
      body: m,
      footer: h('span', null,
        isEdit ? h('button', { class: 'btn btn-danger', onclick: async () => {
          if (!confirm('Excluir este evento?')) return;
          try { await API.del('/api/events/' + e.id); await loadAll(); closeModal(); render(); toast('Evento excluido', 'success'); }
          catch (err) { toast(err.message, 'error'); }
        }}, 'Excluir') : null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'event-form' }, isEdit ? 'Salvar' : 'Criar')
      )
    };
    render();
  }

  // ---- TASKS ----
  function TasksPage() {
    const filter = S.filters.tasks || 'pending';
    let list = S.data.tasks.slice();
    if (filter === 'pending') list = list.filter(t => t.status !== 'concluida');
    else if (filter === 'done') list = list.filter(t => t.status === 'concluida');
    list.sort((a, b) => (a.due_date || '9999').localeCompare(b.due_date || '9999'));

    return AppShell('Tarefas',
      h('div', { class: 'tabs' },
        h('div', { class: 'tab ' + (filter === 'pending' ? 'active' : ''), onclick: () => { S.filters.tasks = 'pending'; render(); } }, 'Pendentes'),
        h('div', { class: 'tab ' + (filter === 'done' ? 'active' : ''), onclick: () => { S.filters.tasks = 'done'; render(); } }, 'Concluidas'),
        h('div', { class: 'tab ' + (filter === 'all' ? 'active' : ''), onclick: () => { S.filters.tasks = 'all'; render(); } }, 'Todas')
      ),
      h('div', { class: 'card' },
        h('div', { class: 'card-header' },
          h('h3', null, list.length + ' tarefa' + (list.length !== 1 ? 's' : '')),
          h('button', { class: 'btn btn-primary', onclick: () => openTaskModal() }, '+ Nova tarefa')
        ),
        list.length === 0 ? h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, '✓'), h('h3', null, 'Nenhuma tarefa'))
          : h('div', null,
              ...list.map(t => {
                const responsible = S.data.users.find(u => u.id === t.responsible_id);
                const cs = S.data.cases.find(c => c.id === t.case_id);
                const isOverdue = t.status !== 'concluida' && t.due_date && t.due_date < todayISO();
                return h('div', { class: 'list-item' + (t.status === 'concluida' ? ' done' : '') },
                  h('div', { class: 'check ' + (t.status === 'concluida' ? 'done' : ''), onclick: () => toggleTask(t.id, t.status) }, t.status === 'concluida' ? '✓' : ''),
                  h('div', { class: 'body' },
                    h('div', { class: 'title' + (t.status === 'concluida' ? ' done' : '') }, t.title),
                    h('div', { class: 'meta' },
                      (t.due_date ? 'Vence ' + fmtDate(t.due_date) : 'Sem prazo') +
                      (responsible ? ' • ' + responsible.name : '') +
                      (cs ? ' • ' + cs.title : '')
                    )
                  ),
                  h('div', { class: 'right' },
                    isOverdue ? badge('Atrasada', 'danger') : null,
                    priorityBadge(t.priority)
                  )
                );
              })
            )
      )
    );
  }

  // ---- FINANCE ----
  function FinancePage() {
    const tab = S.filters.financeTab || 'receitas';
    const period = S.filters.financePeriod || 'all';
    const statusFilter = S.filters.financeStatus || 'all';

    // Filtrar por periodo
    const now = new Date();
    const cutoff = (() => {
      if (period === 'all') return null;
      const months = { '1m': 1, '3m': 3, '6m': 6, '12m': 12, 'ytd': null }[period];
      if (period === 'ytd') return new Date(now.getFullYear(), 0, 1);
      if (!months) return null;
      const d = new Date(now.getFullYear(), now.getMonth() - months + 1, 1);
      return d;
    })();

    const tx = S.data.transactions.slice()
      .filter(t => !cutoff || (t.date && new Date(t.date) >= cutoff))
      .sort((a, b) => (b.date || '').localeCompare(a.date || ''));

    // Filtrar por status
    const txByStatus = statusFilter === 'all' ? tx : tx.filter(t => t.status === statusFilter);
    const list = txByStatus.filter(t => t.type === tab);

    // KPIs
    const totalRec = tx.filter(t => t.type === 'receita' && t.status === 'pago').reduce((s, t) => s + t.amount, 0);
    const totalDes = tx.filter(t => t.type === 'despesa' && t.status === 'pago').reduce((s, t) => s + t.amount, 0);
    const pendRec = tx.filter(t => t.type === 'receita' && t.status === 'pendente').reduce((s, t) => s + t.amount, 0);
    const pendDes = tx.filter(t => t.type === 'despesa' && t.status === 'pendente').reduce((s, t) => s + t.amount, 0);
    const balance = totalRec - totalDes;

    // Resumo por categoria
    const byCategory = {};
    txByStatus.filter(t => t.type === tab).forEach(t => {
      const k = t.category || 'Sem categoria';
      byCategory[k] = (byCategory[k] || 0) + t.amount;
    });
    const categories = Object.entries(byCategory).sort((a, b) => b[1] - a[1]);
    const catTotal = categories.reduce((s, [, v]) => s + v, 0) || 1;

    // Grafico de evolucao mensal (ultimos 6 meses)
    const monthlyChart = (() => {
      const months = [];
      for (let i = 5; i >= 0; i--) {
        const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
        months.push({ key: d.toISOString().slice(0, 7), label: d.toLocaleDateString('pt-BR', { month: 'short' }).replace('.', '') });
      }
      const data = months.map(m => {
        const rec = tx.filter(t => t.type === 'receita' && t.status === 'pago' && (t.date || '').startsWith(m.key)).reduce((s, t) => s + t.amount, 0);
        const des = tx.filter(t => t.type === 'despesa' && t.status === 'pago' && (t.date || '').startsWith(m.key)).reduce((s, t) => s + t.amount, 0);
        return { ...m, rec, des };
      });
      const max = Math.max(...data.flatMap(d => [d.rec, d.des]), 1);
      return h('div', { class: 'chart-bar' },
        ...data.map(m => h('div', { class: 'chart-col' },
          h('div', { class: 'chart-bars' },
            h('div', { class: 'chart-bar-item rec', style: { height: (m.rec / max * 140) + 'px' }, title: 'Receita: ' + fmtBRL(m.rec) }),
            h('div', { class: 'chart-bar-item des', style: { height: (m.des / max * 140) + 'px' }, title: 'Despesa: ' + fmtBRL(m.des) })
          ),
          h('div', { class: 'chart-label' }, m.label)
        ))
      );
    })();

    const onToggleStatus = async (t) => {
      try { await API.put('/api/transactions/' + t.id, { status: t.status === 'pago' ? 'pendente' : 'pago' }); await loadAll(); render(); }
      catch (err) { toast(err.message, 'error'); }
    };
    const onEdit = (t) => openTransactionModal(t);
    const onDelete = async (t) => {
      if (!confirm('Excluir este lancamento? Esta acao nao pode ser desfeita.')) return;
      try { await API.del('/api/transactions/' + t.id); await loadAll(); render(); toast('Lancamento excluido', 'success'); }
      catch (err) { toast(err.message, 'error'); }
    };
    const onNew = () => openTransactionModal();

    return AppShell('Financeiro',
      // KPIs
      h('div', { class: 'kpi-grid' },
        h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, 'Receitas pagas'), h('div', { class: 'kpi-value', style: { color: 'var(--success)' } }, fmtBRL(totalRec))),
        h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, 'Despesas pagas'), h('div', { class: 'kpi-value', style: { color: 'var(--danger)' } }, fmtBRL(totalDes))),
        h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, 'Saldo'), h('div', { class: 'kpi-value', style: { color: balance >= 0 ? 'var(--success)' : 'var(--danger)' } }, fmtBRL(balance))),
        h('div', { class: 'kpi' }, h('div', { class: 'kpi-label' }, 'Pendente total'), h('div', { class: 'kpi-value' }, fmtBRL(pendRec + pendDes)))
      ),
      // Graficos
      h('div', { class: 'grid-2-1', style: { marginBottom: '18px' } },
        h('div', { class: 'card' },
          h('div', { class: 'card-header' },
            h('div', null, h('h3', null, 'Evolucao mensal'), h('div', { class: 'sub' }, 'Ultimos 6 meses'))
          ),
          monthlyChart,
          h('div', { class: 'chart-legend' },
            h('span', null, h('span', { class: 'legend-dot', style: { background: '#C9A96E' } }), 'Receitas'),
            h('span', null, h('span', { class: 'legend-dot', style: { background: '#2a3a73' } }), 'Despesas')
          )
        ),
        h('div', { class: 'card' },
          h('div', { class: 'card-header' }, h('h3', null, 'Por categoria')),
          categories.length === 0
            ? h('div', { class: 'empty' }, h('p', null, 'Sem dados'))
            : h('div', null,
                ...categories.slice(0, 8).map(([cat, val]) => h('div', { class: 'cat-row' },
                  h('div', { class: 'cat-name' }, cat),
                  h('div', { class: 'cat-bar-wrap' },
                    h('div', { class: 'cat-bar', style: { width: ((val / catTotal) * 100) + '%' } })
                  ),
                  h('div', { class: 'cat-val' }, fmtBRL(val))
                ))
              )
        )
      ),
      // Filtros
      h('div', { class: 'filters' },
        h('select', { onchange: (e) => { S.filters.financePeriod = e.target.value; render(); } },
          h('option', { value: 'all', selected: period === 'all' }, 'Todo o periodo'),
          h('option', { value: '1m', selected: period === '1m' }, 'Ultimo mes'),
          h('option', { value: '3m', selected: period === '3m' }, 'Ultimos 3 meses'),
          h('option', { value: '6m', selected: period === '6m' }, 'Ultimos 6 meses'),
          h('option', { value: '12m', selected: period === '12m' }, 'Ultimos 12 meses'),
          h('option', { value: 'ytd', selected: period === 'ytd' }, 'Este ano')
        ),
        h('select', { onchange: (e) => { S.filters.financeStatus = e.target.value; render(); } },
          h('option', { value: 'all', selected: statusFilter === 'all' }, 'Todos status'),
          h('option', { value: 'pago', selected: statusFilter === 'pago' }, 'Pagos'),
          h('option', { value: 'pendente', selected: statusFilter === 'pendente' }, 'Pendentes')
        ),
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn btn-primary', onclick: onNew }, '+ Novo lancamento')
      ),
      // Tabs
      h('div', { class: 'tabs' },
        h('div', { class: 'tab ' + (tab === 'receita' ? 'active' : ''), onclick: () => { S.filters.financeTab = 'receita'; render(); } }, 'Receitas'),
        h('div', { class: 'tab ' + (tab === 'despesa' ? 'active' : ''), onclick: () => { S.filters.financeTab = 'despesa'; render(); } }, 'Despesas')
      ),
      // Lista
      h('div', { class: 'card' },
        h('div', { class: 'card-header' },
          h('h3', null, tab === 'receitas' ? 'Receitas' : 'Despesas'),
          h('span', { class: 'small muted' }, list.length + ' lancamento' + (list.length !== 1 ? 's' : ''))
        ),
        list.length === 0 ? h('div', { class: 'empty' },
          h('div', { class: 'empty-icon' }, '💰'),
          h('h3', null, 'Nenhum lancamento'),
          h('p', null, statusFilter !== 'all' ? 'Nenhum lancamento com o status selecionado.' : 'Cadastre seu primeiro lancamento.'),
          h('button', { class: 'btn btn-primary', onclick: onNew }, '+ Novo lancamento')
        )
          : h('table', { class: 'table' },
              h('thead', null, h('tr', null,
                h('th', null, 'Descricao'),
                h('th', null, 'Categoria'),
                h('th', null, 'Data'),
                h('th', null, 'Vencimento'),
                h('th', null, 'Forma'),
                h('th', { class: 'text-right' }, 'Valor'),
                h('th', null, 'Status'),
                h('th', null, 'Acoes')
              )),
              h('tbody', null, ...list.map(t => {
                const cs = S.data.cases.find(c => c.id === t.case_id);
                const cl = S.data.clients.find(c => c.id === t.client_id);
                const isOverdue = t.status === 'pendente' && t.due_date && t.due_date < todayISO();
                return h('tr', null,
                  h('td', null,
                    h('div', { class: 'strong' }, t.description),
                    h('div', { class: 'small muted' },
                      (cs ? cs.title : '') + (cl ? (cs ? ' • ' : '') + cl.name : '')
                    )
                  ),
                  h('td', null, badge(t.category || 'Sem categoria', 'neutral')),
                  h('td', { class: 'small' }, fmtDate(t.date)),
                  h('td', { class: 'small ' + (isOverdue ? 'danger' : 'muted') },
                    t.due_date ? (fmtDate(t.due_date) + (isOverdue ? ' ⚠' : '')) : '-'
                  ),
                  h('td', { class: 'small muted' }, t.payment_method || '-'),
                  h('td', { class: 'text-right strong', style: { color: t.type === 'receita' ? 'var(--success)' : 'var(--danger)' } }, fmtBRL(t.amount)),
                  h('td', null, statusBadge(t.status)),
                  h('td', { class: 'actions' },
                    h('button', { class: 'btn btn-sm ' + (t.status === 'pago' ? 'btn-ghost' : 'btn-success'), title: t.status === 'pago' ? 'Reverter para pendente' : 'Marcar como pago', onclick: () => onToggleStatus(t) },
                      t.status === 'pago' ? '↶' : '✓'
                    ),
                    h('button', { class: 'btn btn-sm btn-ghost', title: 'Editar', onclick: () => onEdit(t) }, '✏'),
                    h('button', { class: 'btn btn-sm btn-danger', title: 'Excluir', onclick: () => onDelete(t) }, '🗑')
                  )
                );
              }))
            )
      )
    );
  }

  function openTransactionModal(t) {
    const isEdit = !!t;
    const tab = S.filters.financeTab || 'receitas';
    const m = h('form', { id: 'tx-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        type: fd.get('type'),
        description: fd.get('description'),
        amount: parseFloat(fd.get('amount')) || 0,
        date: fd.get('date'),
        due_date: fd.get('due_date') || null,
        status: fd.get('status'),
        category: fd.get('category'),
        case_id: fd.get('case_id') || null,
        client_id: fd.get('client_id') || null,
        payment_method: fd.get('payment_method'),
      };
      try {
        if (isEdit) await API.put('/api/transactions/' + t.id, data);
        else await API.post('/api/transactions', data);
        await loadAll(); closeModal(); render(); toast('Lancamento salvo', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Tipo *'), h('select', { name: 'type' },
          h('option', { value: 'receita', selected: !t || t.type === 'receita' }, 'Receita'),
          h('option', { value: 'despesa', selected: t && t.type === 'despesa' }, 'Despesa')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Status'), h('select', { name: 'status' },
          h('option', { value: 'pendente', selected: !t || t.status === 'pendente' }, 'Pendente'),
          h('option', { value: 'pago', selected: t && t.status === 'pago' }, 'Pago')
        ))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Descricao *'), h('input', { type: 'text', name: 'description', required: true, value: (t && t.description) || '' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Valor (R$) *'), h('input', { type: 'number', name: 'amount', step: '0.01', required: true, value: (t && t.amount) || 0 })),
        h('div', { class: 'form-group' }, h('label', null, 'Data *'), h('input', { type: 'date', name: 'date', required: true, value: (t && t.date) || todayISO() }))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Categoria'), h('input', { type: 'text', name: 'category', value: (t && t.category) || '', list: 'cat-list' }),
          h('datalist', { id: 'cat-list' },
            h('option', { value: 'Honorarios Advocaticios' }),
            h('option', { value: 'Consultoria' }),
            h('option', { value: 'Aluguel' }),
            h('option', { value: 'Folha' }),
            h('option', { value: 'Software' }),
            h('option', { value: 'Custas' }),
            h('option', { value: 'Material' })
          )
        ),
        h('div', { class: 'form-group' }, h('label', null, 'Forma de pagamento'), h('select', { name: 'payment_method' },
          h('option', { value: '' }, '-'),
          h('option', { value: 'Boleto' }, 'Boleto'),
          h('option', { value: 'PIX' }, 'PIX'),
          h('option', { value: 'Transferencia' }, 'Transferencia'),
          h('option', { value: 'Cartao de credito' }, 'Cartao de credito'),
          h('option', { value: 'Dinheiro' }, 'Dinheiro')
        ))
      ),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Caso (opcional)'), h('select', { name: 'case_id' },
          h('option', { value: '' }, 'Sem caso'),
          ...S.data.cases.map(c => h('option', { value: c.id, selected: t && t.case_id === c.id }, c.title))
        )),
        h('div', { class: 'form-group' }, h('label', null, 'Cliente (opcional)'), h('select', { name: 'client_id' },
          h('option', { value: '' }, 'Sem cliente'),
          ...S.data.clients.map(c => h('option', { value: c.id, selected: t && t.client_id === c.id }, c.name))
        ))
      )
    );
    S.modal = {
      title: isEdit ? 'Editar lancamento' : (tab === 'receita' ? 'Nova receita' : 'Nova despesa'),
      body: m,
      footer: h('span', null,
        isEdit ? h('button', { class: 'btn btn-danger', onclick: async () => {
          if (!confirm('Excluir este lancamento? Esta acao nao pode ser desfeita.')) return;
          try { await API.del('/api/transactions/' + t.id); await loadAll(); closeModal(); render(); toast('Lancamento excluido', 'success'); }
          catch (err) { toast(err.message, 'error'); }
        }}, 'Excluir') : null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'tx-form' }, isEdit ? 'Salvar alteracoes' : 'Salvar')
      )
    };
    render();
  }

  // ---- DOCUMENTS ----
  function DocumentsPage() {
    const search = S.filters.docSearch || '';
    let list = S.data.documents.slice();
    if (search) list = list.filter(d => (d.title + ' ' + (d.category || '') + ' ' + (d.notes || '')).toLowerCase().includes(search.toLowerCase()));

    return AppShell('Documentos',
      h('div', { class: 'filters' },
        h('input', { type: 'text', placeholder: 'Buscar por titulo, categoria...', value: search, oninput: (e) => { S.filters.docSearch = e.target.value; render(); } }),
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn btn-primary', onclick: () => openDocumentModal() }, '+ Novo documento')
      ),
      h('div', { class: 'card' },
        list.length === 0 ? h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, '📄'), h('h3', null, 'Nenhum documento'))
          : h('table', { class: 'table' },
              h('thead', null, h('tr', null, h('th', null, 'Documento'), h('th', null, 'Categoria'), h('th', null, 'Tipo'), h('th', null, 'Caso'), h('th', null, 'Data'), h('th', null, 'Tamanho'))),
              h('tbody', null, ...list.map(d => {
                const cs = S.data.cases.find(c => c.id === d.case_id);
                return h('tr', null,
                  h('td', null, h('div', { class: 'strong' }, d.title), d.notes ? h('div', { class: 'small muted' }, d.notes) : null),
                  h('td', null, badge(d.category || '-', 'gold')),
                  h('td', { class: 'small' }, d.type || '-'),
                  h('td', { class: 'small' }, cs ? cs.title : '-'),
                  h('td', { class: 'small' }, fmtDate(d.date)),
                  h('td', { class: 'small muted' }, d.size || '-')
                );
              }))
            )
      )
    );
  }

  // ---- TEAM ----
  function TeamPage() {
    return AppShell('Equipe',
      h('div', { class: 'filters' },
        h('div', { class: 'spacer' }),
        h('button', { class: 'btn btn-primary', onclick: () => openUserModal() }, '+ Adicionar membro')
      ),
      h('div', { class: 'grid-3' },
        ...S.data.users.map(u => {
          const caseCount = S.data.cases.filter(c => c.responsible_id === u.id).length;
          const taskCount = S.data.tasks.filter(t => t.responsible_id === u.id && t.status === 'pendente').length;
          return h('div', { class: 'client-card' },
            h('div', { class: 'client-avatar' }, initials(u.name)),
            h('div', { class: 'client-name' }, u.name),
            h('div', { class: 'client-meta' }, badge(u.role, u.role === 'Socio' ? 'gold' : 'info')),
            h('div', { class: 'small muted' }, u.oab || 'Sem OAB'),
            h('div', { class: 'small muted' }, u.email),
            h('div', { class: 'small muted' }, u.phone || ''),
            h('div', { class: 'client-stats' },
              h('div', { class: 'client-stat' }, h('div', { class: 'lbl' }, 'Casos'), h('div', { class: 'val' }, caseCount)),
              h('div', { class: 'client-stat' }, h('div', { class: 'lbl' }, 'Tarefas'), h('div', { class: 'val' }, taskCount))
            ),
            h('div', { style: { marginTop: '10px' } },
              h('button', { class: 'btn btn-sm btn-ghost', onclick: () => openUserModal(u) }, '✏️ Editar')
            )
          );
        })
      )
    );
  }

  function openUserModal(u) {
    const isEdit = !!u;
    const m = h('form', { id: 'user-form', onsubmit: async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        name: fd.get('name'),
        email: fd.get('email'),
        role: fd.get('role'),
        oab: fd.get('oab') || null,
        phone: fd.get('phone') || null,
      };
      if (fd.get('password')) data.password = fd.get('password');
      try {
        if (isEdit) await API.put('/api/users/' + u.id, data);
        else { data.password = data.password || '123456'; await API.post('/api/users', data); }
        await loadAll(); closeModal(); render(); toast(isEdit ? 'Membro atualizado' : 'Membro adicionado', 'success');
      } catch (err) { toast(err.message, 'error'); }
    }},
      h('div', { class: 'form-group' }, h('label', null, 'Nome *'), h('input', { type: 'text', name: 'name', required: true, value: (u && u.name) || '' })),
      h('div', { class: 'form-group' }, h('label', null, 'E-mail *'), h('input', { type: 'email', name: 'email', required: true, value: (u && u.email) || '' })),
      h('div', { class: 'form-row' },
        h('div', { class: 'form-group' }, h('label', null, 'Funcao *'), h('select', { name: 'role' },
          h('option', { value: 'Socio', selected: u && u.role === 'Socio' }, 'Socio'),
          h('option', { value: 'Advogado', selected: !u || u.role === 'Advogado' }, 'Advogado'),
          h('option', { value: 'Paralegal', selected: u && u.role === 'Paralegal' }, 'Paralegal'),
          h('option', { value: 'Estagiario', selected: u && u.role === 'Estagiario' }, 'Estagiario')
        )),
        h('div', { class: 'form-group' }, h('label', null, 'OAB'), h('input', { type: 'text', name: 'oab', value: (u && u.oab) || '' }))
      ),
      h('div', { class: 'form-group' }, h('label', null, 'Telefone'), h('input', { type: 'text', name: 'phone', value: (u && u.phone) || '' })),
      h('div', { class: 'form-group' }, h('label', null, isEdit ? 'Nova senha (deixe em branco para manter)' : 'Senha inicial'),
        h('input', { type: 'password', name: 'password', placeholder: isEdit ? 'Manter senha atual' : 'Padrao: 123456' })
      )
    );
    S.modal = {
      title: isEdit ? 'Editar membro' : 'Adicionar membro',
      body: m,
      footer: h('span', null,
        isEdit && u.id !== S.user.id ? h('button', { class: 'btn btn-danger', onclick: async () => {
          if (!confirm('Remover este membro da equipe?')) return;
          try { await API.del('/api/users/' + u.id); await loadAll(); closeModal(); render(); toast('Membro removido', 'success'); }
          catch (err) { toast(err.message, 'error'); }
        }}, 'Remover') : null,
        h('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancelar'),
        h('button', { class: 'btn btn-primary', type: 'submit', form: 'user-form' }, 'Salvar')
      )
    };
    render();
  }

  // ---- SETTINGS ----
  async function SettingsPage() {
    let settings = {};
    try { settings = await API.get('/api/settings'); } catch (e) {}
    const set = settings || {};
    const onSave = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const data = {
        firm_name: fd.get('firm_name'),
        cnpj: fd.get('cnpj'),
        address: fd.get('address'),
        phone: fd.get('phone'),
        email: fd.get('email'),
      };
      try { await API.post('/api/settings', data); toast('Configuracoes salvas', 'success'); }
      catch (err) { toast(err.message, 'error'); }
    };
    return AppShell('Configuracoes',
      h('div', { class: 'grid-2' },
        h('div', { class: 'card' },
          h('div', { class: 'card-header' }, h('h3', null, 'Dados do escritorio')),
          h('form', { onsubmit: onSave },
            h('div', { class: 'form-group' }, h('label', null, 'Nome do escritorio'), h('input', { type: 'text', name: 'firm_name', value: set.firm_name || '' })),
            h('div', { class: 'form-row' },
              h('div', { class: 'form-group' }, h('label', null, 'CNPJ'), h('input', { type: 'text', name: 'cnpj', value: set.cnpj || '' })),
              h('div', { class: 'form-group' }, h('label', null, 'Telefone'), h('input', { type: 'text', name: 'phone', value: set.phone || '' }))
            ),
            h('div', { class: 'form-group' }, h('label', null, 'E-mail'), h('input', { type: 'email', name: 'email', value: set.email || '' })),
            h('div', { class: 'form-group' }, h('label', null, 'Endereco'), h('input', { type: 'text', name: 'address', value: set.address || '' })),
            h('button', { type: 'submit', class: 'btn btn-primary' }, 'Salvar')
          )
        ),
        h('div', { class: 'card' },
          h('div', { class: 'card-header' }, h('h3', null, 'Dados e backup')),
          h('div', { class: 'mb-2' },
            h('p', { class: 'small muted', style: { marginBottom: '8px' } }, 'Exporte um backup completo do seu banco de dados em formato JSON. O arquivo contem todos os cadastros, casos, tarefas, eventos e transacoes.'),
            h('a', { class: 'btn btn-ghost', href: '/api/export', download: 'lexflow_backup.json', target: '_blank' }, '⬇ Baixar backup JSON')
          ),
          h('div', { class: 'mb-2' },
            h('p', { class: 'small muted', style: { marginBottom: '8px' } }, 'O banco de dados SQLite esta salvo em:'),
            h('code', { style: { background: 'var(--bg-3)', padding: '4px 8px', borderRadius: '4px', fontSize: '12px' } }, 'data/lexflow.db')
          ),
          h('div', { class: 'mb-2' },
            h('p', { class: 'small muted' }, 'Informacoes da sessao atual:'),
            h('p', { class: 'small' }, h('strong', null, 'Usuario: '), S.user.name + ' (' + S.user.email + ')'),
            h('p', { class: 'small' }, h('strong', null, 'Funcao: '), S.user.role)
          )
        )
      )
    );
  }

  // ---- MODAL ----
  function closeModal() {
    S.modal = null;
    render();
  }

  function Modal() {
    if (!S.modal) return null;
    return h('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target.classList.contains('modal-backdrop')) closeModal(); } },
      h('div', { class: 'modal lg' },
        h('div', { class: 'modal-header' },
          h('h3', null, S.modal.title),
          h('button', { class: 'close-btn', onclick: closeModal }, '×')
        ),
        h('div', { class: 'modal-body' }, S.modal.body),
        h('div', { class: 'modal-footer' }, S.modal.footer)
      )
    );
  }

  // ---- SEARCH / NOTIFICATIONS / THEME ----

  let _searchDebounce = null;
  function openSearch() {
    S.search.open = true;
    render();
    setTimeout(() => {
      const inp = document.getElementById('search-input');
      if (inp) inp.focus();
    }, 50);
  }
  function closeSearch() {
    S.search.open = false;
    S.search.q = '';
    S.search.results = [];
    render();
  }
  function onSearchInput(e) {
    S.search.q = e.target.value;
    if (_searchDebounce) clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(async () => {
      const q = S.search.q.trim();
      if (q.length < 2) { S.search.results = []; render(); return; }
      S.search.loading = true; render();
      try {
        const r = await API.get('/api/search?q=' + encodeURIComponent(q));
        S.search.results = r.items || [];
      } catch (err) { S.search.results = []; }
      S.search.loading = false; render();
    }, 200);
  }
  function goToResult(it) {
    closeSearch();
    if (it.params) go(it.link, it.params);
    else go(it.link);
  }
  function SearchModal() {
    if (!S.search.open) return null;
    return h('div', { class: 'modal-backdrop', onclick: (e) => { if (e.target.classList.contains('modal-backdrop')) closeSearch(); } },
      h('div', { class: 'search-modal' },
        h('div', { class: 'search-header' },
          h('span', { class: 'search-icon' }, '🔍'),
          h('input', { id: 'search-input', type: 'text', placeholder: 'Buscar casos, clientes, tarefas...', value: S.search.q, oninput: onSearchInput }),
          h('span', { class: 'kbd' }, 'ESC')
        ),
        h('div', { class: 'search-results' },
          S.search.loading ? h('div', { class: 'search-empty' }, 'Buscando...') :
            S.search.q.length < 2 ? h('div', { class: 'search-empty' }, 'Digite ao menos 2 caracteres') :
            S.search.results.length === 0 ? h('div', { class: 'search-empty' }, 'Nenhum resultado encontrado') :
            S.search.results.map(it => h('div', { class: 'search-result', onclick: () => goToResult(it) },
              h('span', { class: 'result-type type-' + it.type }, ({case: 'CASO', client: 'CLIENTE', task: 'TAREFA', update: 'ANDAMENTO'})[it.type] || it.type),
              h('div', { class: 'result-body' },
                h('div', { class: 'result-title' }, it.title),
                h('div', { class: 'result-sub' }, it.subtitle || '')
              )
            ))
        )
      )
    );
  }

  function toggleTheme() {
    const next = S.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    if (S.user) {
      API.post('/api/auth/theme', { theme: next }).catch(() => {});
    }
    render();
  }

  function toggleNotifications() {
    S.notifications.open = !S.notifications.open;
    render();
  }
  async function loadNotifications() {
    if (!S.token) return;
    try {
      const r = await API.get('/api/notifications');
      S.notifications.items = r.items || [];
    } catch (e) {}
  }
  function NotificationsPanel() {
    if (!S.notifications.open) return null;
    const items = S.notifications.items;
    return h('div', { class: 'notif-panel' },
      h('div', { class: 'notif-header' },
        h('h3', null, 'Notificacoes'),
        h('button', { class: 'btn btn-sm btn-ghost', onclick: () => { S.notifications.open = false; render(); } }, '×')
      ),
      items.length === 0
        ? h('div', { class: 'notif-empty' }, h('div', null, '✓'), h('p', null, 'Sem pendencias. Voce esta em dia!'))
        : h('div', { class: 'notif-list' },
            ...items.map(n => h('div', { class: 'notif-item level-' + n.level, onclick: () => { S.notifications.open = false; if (n.params) go(n.link, n.params); else go(n.link); render(); } },
              h('div', { class: 'notif-icon' }, ({task_overdue: '⚠', task_due_soon: '⏰', case_deadline: '📅', event_today: '📍'})[n.type] || '•'),
              h('div', { class: 'notif-body' },
                h('div', { class: 'notif-title' }, n.title),
                h('div', { class: 'notif-meta' }, n.meta || '')
              )
            ))
        )
    );
  }

  // ---- TRASH (LIXEIRA) ----
  async function TrashPage() {
    let trash = { clients: [], cases: [], tasks: [] };
    try { trash = await API.get('/api/trash'); } catch (e) {}
    const total = trash.clients.length + trash.cases.length + trash.tasks.length;
    const restore = async (kind, id) => {
      try {
        await API.post('/api/' + kind + '/' + id + '/restore');
        await loadAll();
        render();
        toast('Item restaurado', 'success');
      } catch (e) { toast(e.message, 'error'); }
    };
    const sec = (title, items, kind) => h('div', { class: 'card mb-3' },
      h('div', { class: 'card-header' }, h('h3', null, title + ' (' + items.length + ')')),
      items.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nada por aqui.')) :
        h('table', { class: 'table' },
          h('thead', null, h('tr', null, h('th', null, 'Item'), h('th', null, 'Excluido em'), h('th', null, 'Acoes'))),
          h('tbody', null, ...items.map(it => h('tr', null,
            h('td', null, h('div', { class: 'strong' }, it.name || it.title)),
            h('td', { class: 'small muted' }, it.deleted_at ? fmtDate(it.deleted_at) : '-'),
            h('td', null, h('button', { class: 'btn btn-sm btn-ghost', onclick: () => restore(kind, it.id) }, '↶ Restaurar'))
          )))
        )
    );
    return AppShell('Lixeira',
      h('div', { class: 'card mb-3' },
        h('p', { class: 'small muted' }, 'Itens excluidos vao para a lixeira. Voce pode restaura-los a qualquer momento.')
      ),
      total === 0 ? h('div', { class: 'card' }, h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, '🗑'), h('h3', null, 'Lixeira vazia'), h('p', null, 'Nada para restaurar.'))) :
        h('div', null,
          sec('Clientes', trash.clients, 'clients'),
          sec('Casos', trash.cases, 'cases'),
          sec('Tarefas', trash.tasks, 'tasks')
        )
    );
  }

  // ---- AUDIT LOG ----
  async function AuditPage() {
    let data = { items: [], total: 0 };
    try { data = await API.get('/api/audit?page=1&page_size=200'); } catch (e) {}
    return AppShell('Auditoria',
      h('div', { class: 'card mb-3' },
        h('p', { class: 'small muted' }, 'Registro de todas as acoes realizadas no sistema. Apenas socios e advogados tem acesso.')
      ),
      h('div', { class: 'card' },
        data.items.length === 0 ? h('div', { class: 'empty' }, h('p', null, 'Nenhum registro ainda.')) :
          h('table', { class: 'table' },
            h('thead', null, h('tr', null, h('th', null, 'Quando'), h('th', null, 'Usuario'), h('th', null, 'Acao'), h('th', null, 'Entidade'), h('th', null, 'Detalhes'))),
            h('tbody', null, ...data.items.map(a => h('tr', null,
              h('td', { class: 'small' }, a.created_at),
              h('td', null, a.user_name || '-'),
              h('td', null, badge(a.action, ({create: 'success', update: 'info', delete: 'danger', restore: 'warning', login: 'navy'})[a.action] || 'neutral')),
              h('td', { class: 'small' }, a.entity + (a.entity_id ? ' • ' + a.entity_id.slice(0, 8) : '')),
              h('td', { class: 'small muted', style: { maxWidth: '300px', overflow: 'hidden', textOverflow: 'ellipsis' } }, a.details || '-')
            )))
          )
      )
    );
  }

  // Atalhos globais
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      if (S.token) openSearch();
    }
    if (e.key === 'Escape') {
      if (S.search.open) closeSearch();
      else if (S.notifications.open) { S.notifications.open = false; render(); }
    }
  });

  // ---- RENDER ----
  // async para aguardar pages que sao async (DashboardPage, CasesPage, etc.)
  // _renderToken evita race conditions quando o usuario navega rapido
  async function render() {
    const token = ++_renderToken;
    const app = document.getElementById('app');
    app.innerHTML = '';
    let main;
    try {
      if (!S.token) {
        main = (S.view === 'auth') ? AuthPage() : LandingPage();
      } else {
        switch (S.view) {
          case 'dashboard':   main = await DashboardPage(); break;
          case 'cases':       main = await CasesPage(); break;
          case 'case-detail': main = await CaseDetailPage(); break;
          case 'clients':     main = ClientsPage(); break;
          case 'agenda':      main = AgendaPage(); break;
          case 'kanban':      main = await KanbanPage(); break;
          case 'tasks':       main = TasksPage(); break;
          case 'monitoring':  main = await MonitoringPage(); break;
          case 'finance':     main = FinancePage(); break;
          case 'documents':   main = DocumentsPage(); break;
          case 'team':        main = TeamPage(); break;
          case 'settings':    main = await SettingsPage(); break;
          case 'trash':       main = await TrashPage(); break;
          case 'audit':       main = await AuditPage(); break;
          default:            main = await DashboardPage();
        }
      }
    } catch (e) {
      console.error('render error', e);
      main = h('div', { class: 'card' }, h('p', null, 'Erro ao carregar: ' + e.message));
    }
    // Cancela se outro render comecou depois desse
    if (token !== _renderToken) return;
    if (main) app.appendChild(main);
    const m = Modal();
    if (m) app.appendChild(m);
    const s = SearchModal();
    if (s) app.appendChild(s);
  }

  // ---- BOOT ----
  (async function boot() {
    if (S.token) {
      try {
        const me = await API.get('/api/auth/me');
        S.user = me.user;
        S.csrf = me.csrf || S.csrf;
        if (S.csrf) localStorage.setItem('lexflow_csrf', S.csrf);
        if (me.user && me.user.theme) applyTheme(me.user.theme);
        await loadAll();
        await loadNotifications();
        if (S.view === 'landing' || S.view === 'auth') S.view = 'dashboard';
      } catch (e) {
        S.token = null; S.user = null; S.csrf = null;
        localStorage.removeItem('lexflow_token');
        localStorage.removeItem('lexflow_csrf');
      }
    }
    await render();
    // Polling de notificacoes a cada 60s
    setInterval(() => { if (S.token) loadNotifications(); }, 60000);
  })();



  // ============================================================================
  // KANBAN PAGE — tarefas agrupadas por urgencia/prazo
  // ============================================================================
  const KANBAN_COLUMNS = [
    { key: 'overdue',  title: 'Atrasado',   color: '#dc2626',  bg: '#fee2e2' },
    { key: 'today',    title: 'Hoje',       color: '#ea580c',  bg: '#ffedd5' },
    { key: 'week',     title: 'Esta semana',color: '#2563eb',  bg: '#dbeafe' },
    { key: 'later',    title: 'Mais tarde', color: '#64748b',  bg: '#f1f5f9' },
  ];

  function _kanbanBucket(task) {
    // Tarefas concluidas vao para "Mais tarde" (fechadas), exceto se o user quiser ver de outro jeito
    if (task.status === 'concluida') return 'later';
    if (!task.due_date) return 'later';
    const today = new Date();
    today.setHours(0,0,0,0);
    const d = new Date(task.due_date + 'T00:00:00');
    const diff = Math.floor((d - today) / 86400000);
    if (diff < 0) return 'overdue';
    if (diff === 0) return 'today';
    if (diff <= 7) return 'week';
    return 'later';
  }

  async function KanbanPage() {
    const tasks = (S.data.tasks || []).slice();
    const cases = S.data.cases || [];
    const users = S.data.users || [];
    const caseTitle = (id) => (cases.find(c => c.id === id) || {}).title || '—';
    const userName = (id) => (users.find(u => u.id === id) || {}).name || '—';

    const cols = {};
    for (const c of KANBAN_COLUMNS) cols[c.key] = [];
    for (const t of tasks) cols[_kanbanBucket(t)].push(t);
    // ordena por data
    for (const c of KANBAN_COLUMNS) cols[c.key].sort((a,b) => (a.due_date||'9999').localeCompare(b.due_date||'9999'));

    const card = (t) => {
      const priColor = { alta: '#dc2626', media: '#ea580c', baixa: '#64748b' }[t.priority] || '#64748b';
      return h('div', {
        class: 'kanban-card',
        draggable: 'true',
        ondragstart: (e) => { e.dataTransfer.setData('text/plain', t.id); e.dataTransfer.effectAllowed = 'move'; },
        onclick: () => { S.editing = { entity: 'tasks', id: t.id }; openModal(); },
      },
        h('div', { class: 'kanban-card-pri', style: `background:${priColor}` }),
        h('div', { class: 'kanban-card-title' }, t.title || '(sem titulo)'),
        h('div', { class: 'kanban-card-meta' },
          t.due_date ? h('span', { class: 'kanban-due' }, '📅 ' + t.due_date) : null,
          t.case_id ? h('span', { class: 'kanban-case' }, '📋 ' + (caseTitle(t.case_id) || '').slice(0, 30)) : null,
        ),
        t.responsible_id ? h('div', { class: 'kanban-resp' }, '👤 ' + userName(t.responsible_id)) : null,
      );
    };

    const column = (cfg) => h('div', {
      class: 'kanban-col',
      ondragover: (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; },
      ondrop: async (e) => {
        e.preventDefault();
        const taskId = e.dataTransfer.getData('text/plain');
        if (!taskId) return;
        // Move para a coluna alvo
        const newDate = (() => {
          const today = new Date();
          if (cfg.key === 'overdue') {
            const d = new Date(today); d.setDate(d.getDate() - 1); return d.toISOString().slice(0,10);
          }
          if (cfg.key === 'today') return today.toISOString().slice(0,10);
          if (cfg.key === 'week') { const d = new Date(today); d.setDate(d.getDate() + 3); return d.toISOString().slice(0,10); }
          if (cfg.key === 'later') { const d = new Date(today); d.setDate(d.getDate() + 30); return d.toISOString().slice(0,10); }
          return null;
        })();
        try {
          await API.req('PUT', '/api/tasks/' + taskId, {
            due_date: newDate,
            status: cfg.key === 'overdue' ? 'pendente' : (cfg.key === 'later' && !newDate ? 'pendente' : 'pendente')
          });
          toast('Tarefa movida para "' + cfg.title + '"', 'ok');
          await loadAll();
          render();
        } catch (err) {
          toast('Erro ao mover: ' + err.message, 'err');
        }
      },
    },
      h('div', { class: 'kanban-col-header', style: `background:${cfg.bg}; border-bottom: 3px solid ${cfg.color}` },
        h('span', { class: 'kanban-col-title' }, cfg.title),
        h('span', { class: 'kanban-col-count', style: `color:${cfg.color}` }, cols[cfg.key].length)
      ),
      h('div', { class: 'kanban-col-body' },
        cols[cfg.key].length === 0
          ? h('div', { class: 'kanban-empty' }, 'Nenhuma tarefa')
          : cols[cfg.key].map(card)
      )
    );

    return h('div', { class: 'kanban-page' },
      h('div', { class: 'page-header' },
        h('h1', null, '🎯 Kanban de Tarefas'),
        h('p', { class: 'page-subtitle' }, 'Arraste os cards entre as colunas para reagendar')
      ),
      h('div', { class: 'kanban-board' },
        KANBAN_COLUMNS.map(column)
      )
    );
  }


  // ============================================================================
  // MONITORING PAGE — Datajud + DJE
  // ============================================================================
  let _monitorPollingTimer = null;

  async function MonitoringPage() {
    if (!S.data.monitoring) {
      S.data.monitoring = { items: [], settings: {}, log: [] };
    }
    // Carregar dados
    try {
      const status = await API.req('GET', '/api/monitoring/status');
      const log = await API.req('GET', '/api/monitoring/log?limit=50');
      S.data.monitoring.items = status.items || [];
      S.data.monitoring.log = log.items || [];
    } catch (e) {
      // ignora
    }

    const items = S.data.monitoring.items || [];
    const log = S.data.monitoring.log || [];

    const statusBadge = (it) => {
      if (it.status !== 'active') return h('span', { class: 'mon-badge mon-badge-paused' }, '⏸ Pausado');
      if ((it.error_count || 0) > 0) return h('span', { class: 'mon-badge mon-badge-warn' }, '⚠ Com erro');
      return h('span', { class: 'mon-badge mon-badge-ok' }, '● Ativo');
    };

    const row = (it) => h('div', { class: 'mon-row' },
      h('div', { class: 'mon-row-main' },
        h('div', { class: 'mon-row-title' },
          it.case_title || '(caso removido)',
          (it.responsible_oab_uf && it.responsible_oab) ? h('span', { class: 'mon-oab-badge', title: 'OAB do advogado responsavel — publicacoes do Comunica PJE que citem essa OAB serao inseridas aqui' }, '🔎 OAB/' + it.responsible_oab_uf + ' ' + it.responsible_oab) : null
        ),
        h('div', { class: 'mon-row-meta' },
          h('span', null, it.cnj || 'sem CNJ'),
          it.tribunal ? h('span', { class: 'mon-trib' }, it.tribunal) : null,
          h('span', null, '⏱ a cada ' + (it.interval_minutes || 60) + ' min'),
          it.last_check_at ? h('span', null, '✓ checagem: ' + fmtDateTime(it.last_check_at)) : null,
        ),
        it.last_movement_title ? h('div', { class: 'mon-row-mov' },
          '🔔 Último: ' + it.last_movement_title + (it.last_movement_at ? ' (' + it.last_movement_at + ')' : '')
        ) : null,
        it.last_error ? h('div', { class: 'mon-row-err' }, '⚠ ' + it.last_error) : null,
      ),
      h('div', { class: 'mon-row-side' },
        statusBadge(it),
        h('button', { class: 'btn-mini', onclick: async () => {
          try {
            toast('Sincronizando...', 'info');
            const r = await API.req('POST', '/api/cases/' + it.case_id + '/monitor/run', {});
            const pubsFound = r.pubs_found || 0;
            const inserted = r.inserted || 0;
            const newCases = r.new_cases || 0;
            const samplePubs = r.pubs || [];
            let msg = 'Comunica PJE (' + (r.oab || '?') + '): ' + pubsFound + ' publicacoes encontradas, ' + inserted + ' novas';
            if (newCases > 0) msg += ', ' + newCases + ' caso(s) criado(s)';
            if (samplePubs.length && inserted > 0) msg += ' — ' + samplePubs[0].title;
            toast(msg, inserted > 0 ? 'ok' : 'info');
            if (samplePubs.length > 1 && inserted > 0) {
              openPjePubsModal(it, samplePubs);
            }
            await loadAll();
            render();
          } catch (e) {
            toast('Erro: ' + e.message, 'err');
          }
        } }, '🔄 Sincronizar'),
        h('button', { class: 'btn-mini', onclick: async () => {
          try {
            const newStatus = it.status === 'active' ? 'paused' : 'active';
            await API.req('POST', '/api/cases/' + it.case_id + '/monitor', { status: newStatus });
            toast(newStatus === 'active' ? 'Monitoramento ativado' : 'Monitoramento pausado', 'ok');
            await loadAll();
            render();
          } catch (e) {
            toast('Erro: ' + e.message, 'err');
          }
        } }, it.status === 'active' ? '⏸ Pausar' : '▶ Ativar')
      )
    );

    // Calcular estatisticas OAB
    const casesWithOAB = items.filter(it => it.responsible_oab).length;
    const casesNoOAB = items.filter(it => !it.responsible_oab).length;

    return AppShell('🔔 Monitoramento de Processos',
      h('div', { class: 'mon-page' },
      h('div', { class: 'page-header' },
        h('h1', null, '🔔 Monitoramento de Processos'),
        h('p', { class: 'page-subtitle' }, 'Datajud (CNJ, gratuito) + DJe dos seus tribunais + monitoramento por OAB'),
        h('div', { class: 'mon-header-actions' },
          h('button', { class: 'btn-secondary', onclick: () => openMonitoringSettings() }, '⚙ Configurações'),
          h('button', { class: 'btn-secondary', onclick: async () => {
            await loadAll(); render(); toast('Atualizado', 'ok');
          } }, '🔄 Atualizar')
        )
      ),
      h('div', { class: 'mon-oab-banner' },
        h('strong', null, '🔎 Monitoramento por OAB:'),
        ' ',
        casesWithOAB > 0
          ? (casesWithOAB + ' caso(s) tem OAB do responsavel cadastrada — publicacoes serao inseridas automaticamente.')
          : 'Nenhum caso com OAB do responsavel. Cadastre OAB em Equipe > Editar advogado para ativar busca automatica por OAB.',
        (casesNoOAB > 0 && casesWithOAB === 0)
          ? h('div', { class: 'mon-oab-banner-cta' },
              h('a', { href: '#', onclick: (e) => { e.preventDefault(); goTo('team'); } }, '→ Cadastrar OAB agora'))
          : null
      ),

      h('div', { class: 'mon-info' },
        h('div', null, '💡 Vá até a página de um caso, na aba "Movimentações", e clique em "🔔 Monitorar" para ligar o monitoramento.'),
        h('div', { class: 'mon-info-cases' },
          h('strong', null, 'Casos disponíveis:'),
          (S.data.cases || []).length,
          ' · ',
          h('strong', null, 'Monitorados:'),
          items.filter(i => i.status === 'active').length,
          ' · ',
          h('strong', null, 'Pausados:'),
          items.filter(i => i.status === 'paused').length
        )
      ),

      items.length === 0
        ? h('div', { class: 'mon-empty' },
            h('div', null, '📭 Nenhum caso está sendo monitorado ainda.'),
            h('div', { class: 'mon-empty-sub' }, 'Abra um caso e ative o monitoramento na aba de movimentações.')
          )
        : h('div', { class: 'mon-list' }, items.map(row)),

      h('div', { class: 'mon-log-section' },
        h('h2', null, '📜 Últimas checagens'),
        log.length === 0
          ? h('div', { class: 'mon-empty' }, 'Nenhuma checagem registrada.')
          : h('div', { class: 'mon-log' },
              log.map(l => h('div', { class: 'mon-log-row ' + (l.ok ? 'ok' : 'err') },
                h('span', { class: 'mon-log-time' }, fmtDateTime(l.checked_at)),
                h('span', { class: 'mon-log-src' }, l.source),
                h('span', { class: 'mon-log-case' }, l.case_title || '—'),
                h('span', { class: 'mon-log-msg' }, l.message || (l.ok ? 'OK' : 'falhou')),
                l.movements_found > 0 ? h('span', { class: 'mon-log-new' }, '+' + l.movements_found + ' novos') : null
              ))
            )
      )
    ));
  }

  function openPjePubsModal(it, pubs) {
    const overlay = h('div', { class: 'modal-overlay', onclick: (e) => { if (e.target === overlay) close(); } },
      h('div', { class: 'modal-card' },
        h('div', { class: 'modal-header' },
          h('h2', null, '📰 Publicacoes Comunica PJE encontradas'),
          h('button', { class: 'modal-close', onclick: () => close() }, 'x')
        ),
        h('div', { class: 'modal-body' },
          h('p', { style: 'color:#5a6478;margin-bottom:12px;' },
            'Caso: ', h('strong', null, it.case_title || it.cnj || ''),
            ' - ', pubs.length, ' publicacao(oes) ja vinculadas como andamento.'
          ),
          h('div', { class: 'dje-pubs-list' },
            ...pubs.map(p =>
              h('div', { class: 'dje-pub-item' },
                h('div', { class: 'dje-pub-date' }, p.date || '-'),
                h('div', { class: 'dje-pub-title' }, p.title || '(sem titulo)'),
                p.description ? h('div', { class: 'dje-pub-desc' }, p.description) : null,
                p.url ? h('a', { href: p.url, target: '_blank', class: 'dje-pub-link' }, 'Abrir publicacao') : null
              )
            )
          ),
          h('div', { style: 'margin-top:16px;display:flex;gap:8px;justify-content:flex-end;' },
            h('button', { class: 'btn-primary', onclick: () => { close(); S.route = 'case'; S.caseId = it.case_id; render(); } }, 'Ver no caso')
          )
        )
      )
    );
    document.body.appendChild(overlay);
  }

  function openMonitoringSettings() {
    const overlay = h('div', { class: 'modal-overlay', onclick: (e) => { if (e.target === overlay) close(); } },
      h('div', { class: 'modal-card modal-settings' },
        h('div', { class: 'modal-header' },
          h('h2', null, '⚙ Configurações de Monitoramento'),
          h('button', { class: 'modal-close', onclick: () => close() }, '×')
        ),
        h('div', { class: 'modal-body' },
          h('div', { class: 'modal-info' },
            h('strong', null, '🔎 Monitoramento por OAB (Comunica PJE):'),
            h('br'),
            'O sistema busca publicações automaticamente no ',
            h('a', { href: 'https://comunica.pje.jus.br', target: '_blank', rel: 'noopener' }, 'Comunica PJE'),
            ' usando a OAB do advogado responsável pelo caso (configurada em Equipe > Editar advogado). ',
            'Para cada publicação encontrada, se o CNJ já existir na base, o andamento é inserido no caso correspondente; ',
            'se não existir, um caso novo é criado automaticamente com a publicação como primeiro andamento. ',
            'A consulta é pública e não exige chave de API. ',
            'Exemplo: OAB/RJ 244.384 é pesquisada como ',
            h('a', { href: 'https://comunica.pje.jus.br/consulta?siglaTribunal=TJRJ&numeroOab=244384&ufOab=RJ', target: '_blank', rel: 'noopener' }, 'TJRJ + 244384 + RJ'),
            '.'
          ),
          h('div', { class: 'form-row' },
            h('label', null, 'Intervalo padrão (minutos)'),
            h('input', { type: 'number', id: 'mon-interval', min: '5', max: '1440', value: '60' })
          ),
          h('div', { class: 'form-row-inline' },
            h('label', null, h('input', { type: 'checkbox', id: 'mon-desktop' }), ' Notificação nativa do navegador'),
            h('label', null, h('input', { type: 'checkbox', id: 'mon-email' }), ' Notificar por e-mail'),
          ),
          h('div', { class: 'form-row', id: 'mon-email-row', style: 'display:none' },
            h('label', null, 'Endereço de e-mail'),
            h('input', { type: 'email', id: 'mon-email-addr', placeholder: 'advogado@escritorio.com.br' })
          )
        ),
        h('div', { class: 'modal-footer' },
          h('button', { class: 'btn-secondary', onclick: () => close() }, 'Cancelar'),
          h('button', { class: 'btn-primary', onclick: async () => { await saveMonSettings(); } }, 'Salvar')
        )
      )
    );
    function close() { overlay.remove(); }
    overlay.id = 'modal-mon-settings';
    document.body.appendChild(overlay);

    // carregar valores atuais
    API.req('GET', '/api/monitoring/settings').then(s => {
      document.getElementById('mon-interval').value = s['monitor.default_interval_minutes'] || '60';
      document.getElementById('mon-desktop').checked = s['monitor.notify_desktop'] === '1';
      document.getElementById('mon-email').checked = s['monitor.notify_email'] === '1';
      document.getElementById('mon-email-addr').value = s['monitor.notify_email_address'] || '';
      document.getElementById('mon-email-row').style.display = s['monitor.notify_email'] === '1' ? 'block' : 'none';
    });

    // toggle do campo de e-mail
    setTimeout(() => {
      const cb = document.getElementById('mon-email');
      if (cb) cb.addEventListener('change', () => {
        document.getElementById('mon-email-row').style.display = cb.checked ? 'block' : 'none';
      });
    }, 50);
  }

  async function saveMonSettings() {
    const body = {
      default_interval_minutes: parseInt(document.getElementById('mon-interval').value) || 60,
      notify_desktop: document.getElementById('mon-desktop').checked,
      notify_email: document.getElementById('mon-email').checked,
      notify_email_address: document.getElementById('mon-email-addr').value,
    };
    try {
      await API.req('POST', '/api/monitoring/settings', body);
      toast('Configurações salvas', 'ok');
      document.getElementById('modal-mon-settings').remove();
    } catch (e) {
      toast('Erro ao salvar: ' + e.message, 'err');
    }
  }


  // ============================================================================
  // NOTIFICAÇÃO NATIVA (KANBAN/MONITORAMENTO)
  // ============================================================================
  function _notifyBrowser(title, body) {
    if (!S.settings || S.settings['monitor.notify_desktop'] !== '1') return;
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') {
      new Notification(title, { body: body, icon: '/favicon.ico' });
    } else if (Notification.permission !== 'denied') {
      Notification.requestPermission().then(p => {
        if (p === 'granted') new Notification(title, { body: body });
      });
    }
  }

  function _checkForNewMovements(prev, current) {
    // Compara contagem de case_updates entre cargas. Se cresceu, dispara notif.
    if (!prev) return;
    const prevCount = (prev.updates_total || 0);
    const curCount = (current.updates_total || 0);
    if (curCount > prevCount) {
      const diff = curCount - prevCount;
      _notifyBrowser('🔔 LexFlow: ' + diff + ' andamento(s) novo(s)',
                     'Acesse o sistema para ver as últimas movimentações dos processos.');
    }
  }

})();