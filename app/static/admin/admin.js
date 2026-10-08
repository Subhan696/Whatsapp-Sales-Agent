/* Helio dashboard — client logic. Plain JS, no build step. */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allOrders = [], allCustomers = [], allProducts = [], allBookings = [], allSources = [];
let productsBySku = {};
let pendingReceipts = 0, pendingRefunds = 0, waStatus = null;
let currentPage = 'overview';
let currentProductSku = null;
let productFilter = 'all';
let intervalChoices = [15, 30, 60, 180, 360, 720, 1440];
let mappableFields = [];

const PAGES = {
  overview: ['Overview', 'How your WhatsApp store is doing'],
  orders: ['Orders', 'Every order your agent has taken'],
  customers: ['Customers', 'Everyone who has messaged you, by CRM stage'],
  products: ['Products', 'Your catalog — what the agent can sell'],
  bookings: ['Bookings', 'Appointments and consultations'],
  outbound: ['Broadcasts', 'Proactive WhatsApp campaigns'],
  receipts: ['Receipts', 'Bank-transfer payments awaiting review'],
  refunds: ['Refunds', 'Customer refund requests'],
  settings: ['Settings', 'WhatsApp, agent behaviour and store details'],
};
const PAGE_ALIASES = { inventory: 'products', analytics: 'overview', funnel: 'overview' };

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function jsArg(v) { return esc(JSON.stringify(String(v ?? ''))); }
function icon(name, cls = 'sm') { return `<svg class="i ${cls}"><use href="#i-${name}"/></svg>`; }

function parseDate(iso) {
  if (!iso) return null;
  const s = (iso.includes('+') || iso.endsWith('Z')) ? iso : iso + 'Z';
  const d = new Date(s);
  return isNaN(d) ? null : d;
}
function fmt_date(iso) {
  const d = parseDate(iso);
  if (!d) return '—';
  return d.toLocaleString('en-PK', { timeZone: 'Asia/Karachi', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
}
function fmt_relative(iso) {
  const d = parseDate(iso);
  if (!d) return '—';
  const diff = (Date.now() - d.getTime()) / 1000;
  const abs = Math.abs(diff);
  const unit = abs < 60 ? [Math.round(abs), 'second'] : abs < 3600 ? [Math.round(abs / 60), 'minute'] : abs < 86400 ? [Math.round(abs / 3600), 'hour'] : [Math.round(abs / 86400), 'day'];
  if (abs < 45) return diff >= 0 ? 'just now' : 'in a moment';
  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
  return rtf.format(diff >= 0 ? -unit[0] : unit[0], unit[1]);
}
function fmt_currency(v, cur) {
  const n = parseFloat(v || 0);
  return (cur || 'PKR') + ' ' + n.toLocaleString('en-PK', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}
function fmt_compact(v) {
  const n = parseFloat(v || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K';
  return Math.round(n).toLocaleString();
}
function badge(text, tone = '', plain = false) { return `<span class="badge ${tone}${plain ? ' plain' : ''}">${esc(text)}</span>`; }
function initials(name) { return (String(name || '').match(/[\p{L}\p{N}]+/gu) || []).slice(0, 2).map((s) => s[0]).join('').toUpperCase() || '?'; }
function emptyRow(cols, title, text, iconName = 'package') {
  return `<tr><td class="empty-cell" colspan="${cols}"><div class="empty-state"><div class="empty-icon">${icon(iconName, '')}</div><h4>${esc(title)}</h4>${text ? `<p>${esc(text)}</p>` : ''}</div></td></tr>`;
}
function loadingRow(cols) { return `<tr><td class="empty-cell" colspan="${cols}"><div class="empty-state"><span class="spinner"></span></div></td></tr>`; }

function status_badge(s) {
  const map = {
    draft: ['', 'Draft'],
    awaiting_payment: ['warning', 'Awaiting payment'],
    pending_delivery: ['info', 'Pending delivery'],
    paid: ['success', 'Paid'],
    cancelled: ['danger', 'Cancelled'],
    confirmed: ['success', 'Confirmed'],
    pending: ['warning', 'Pending'],
    expired: ['danger', 'Expired'],
  };
  const [tone, label] = map[s] || ['', s];
  return badge(label, tone);
}
function stage_badge(s) {
  const map = { lead: ['', 'Lead'], interested: ['info', 'Interested'], awaiting_payment: ['warning', 'Awaiting payment'], closed_won: ['success', 'Closed won'] };
  const [tone, label] = map[s] || ['', s];
  return badge(label, tone);
}
function pay_badge(p) { return p === 'cod' ? badge('Cash on delivery', 'violet', true) : badge('Bank transfer', '', true); }
function opt_badge(s) {
  if (s === 'opted_in') return badge('Opted in', 'success');
  if (s === 'opted_out') return badge('Opted out', 'danger');
  return badge('Pending', '');
}

// ---------------------------------------------------------------------------
// Toasts, dialogs, modals, tooltip
// ---------------------------------------------------------------------------
function toast(message, type = 'success', ms = 4200) {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  const ic = type === 'error' ? 'alert' : type === 'info' ? 'info' : 'check';
  el.innerHTML = `<span class="t-icon">${icon(ic, 'sm')}</span><div>${esc(message)}</div>`;
  $('toasts').appendChild(el);
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 220); }, ms);
}

function openModal(id) {
  $(id).classList.add('open');
  document.body.style.overflow = 'hidden';
  const first = $(id).querySelector('input:not([type=hidden]):not([disabled]), textarea, select');
  if (first && window.matchMedia('(min-width: 761px)').matches) setTimeout(() => first.focus(), 50);
}
function closeModal(id) {
  $(id).classList.remove('open');
  if (!document.querySelector('.modal-backdrop.open')) document.body.style.overflow = '';
}

let dialogResolve = null;
function dialog({ title, message = '', confirmText = 'Confirm', cancelText = 'Cancel', danger = false, input = null, checkbox = null }) {
  $('dialog-title').textContent = title;
  $('dialog-message').textContent = message;
  $('dialog-message').classList.toggle('hidden', !message);
  $('dialog-input-wrap').classList.toggle('hidden', !input);
  $('dialog-check-wrap').classList.toggle('hidden', !checkbox);
  if (input) { $('dialog-input-label').textContent = input.label || ''; $('dialog-input').value = input.value || ''; $('dialog-input').placeholder = input.placeholder || ''; }
  if (checkbox) { $('dialog-check-label').textContent = checkbox.label; $('dialog-check').checked = !!checkbox.checked; }
  const ok = $('dialog-ok');
  ok.textContent = confirmText;
  ok.className = 'btn ' + (danger ? 'danger' : 'primary');
  $('dialog-cancel').textContent = cancelText;
  openModal('dialog-modal');
  if (input) setTimeout(() => $('dialog-input').focus(), 50); else setTimeout(() => ok.focus(), 50);
  return new Promise((resolve) => { dialogResolve = resolve; });
}
function finishDialog(confirmed) {
  closeModal('dialog-modal');
  const r = dialogResolve; dialogResolve = null;
  if (r) r(confirmed ? { value: $('dialog-input').value, checked: $('dialog-check').checked } : null);
}
$('dialog-ok').addEventListener('click', () => finishDialog(true));
$('dialog-cancel').addEventListener('click', () => finishDialog(false));

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('dialog-modal').classList.contains('open')) return finishDialog(false);
  const open = [...document.querySelectorAll('.modal-backdrop.open')].pop();
  if (open) { open.id === 'demo-modal' ? closeDemoModal() : closeModal(open.id); return; }
  closeNav();
});
document.querySelectorAll('.modal-backdrop').forEach((bd) => bd.addEventListener('mousedown', (e) => {
  if (e.target !== bd) return;
  if (bd.id === 'dialog-modal') finishDialog(false);
  else if (bd.id === 'demo-modal') closeDemoModal();
  else closeModal(bd.id);
}));

const tip = $('tooltip');
document.addEventListener('mouseover', (e) => {
  const t = e.target.closest('[data-tip]');
  if (!t) { tip.classList.remove('show'); return; }
  tip.innerHTML = t.getAttribute('data-tip');
  const r = t.getBoundingClientRect();
  tip.classList.add('show');
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(window.innerWidth - tw - 8, r.left + r.width / 2 - tw / 2)) + 'px';
  tip.style.top = Math.max(8, r.top - th - 8) + 'px';
});

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
function openNav() { document.body.classList.add('nav-open'); }
function closeNav() { document.body.classList.remove('nav-open'); }

function showTab(name) {
  name = PAGE_ALIASES[name] || name;
  if (!PAGES[name]) name = 'overview';
  currentPage = name;
  document.querySelectorAll('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + name));
  document.querySelectorAll('[data-page]').forEach((b) => b.classList.toggle('active', b.dataset.page === name));
  const moreActive = !['overview', 'orders', 'products', 'customers'].includes(name);
  $('bottom-more').classList.toggle('active', moreActive);
  $('page-title').textContent = PAGES[name][0];
  $('page-subtitle').textContent = PAGES[name][1];
  document.title = PAGES[name][0] + ' · Helio';
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  closeNav();
  window.scrollTo({ top: 0 });

  if (name !== 'settings') stopWaPolling();
  if (!getAdminKey()) return;
  if (name === 'settings') { loadSettings(); refreshWaStatus(); }
  if (name === 'bookings') loadBookings();
  if (name === 'outbound') loadCampaigns();
  if (name === 'refunds') loadRefunds();
  if (name === 'receipts') loadReceipts();
  if (name === 'products') loadSources();
}
document.querySelectorAll('[data-page]').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.page)));
window.addEventListener('hashchange', () => { const h = location.hash.slice(1); if (h && h !== currentPage && PAGES[PAGE_ALIASES[h] || h]) showTab(h); });

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------
function effectiveTheme() {
  const t = document.documentElement.getAttribute('data-theme');
  if (t) return t;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}
function syncThemeIcons() {
  const dark = effectiveTheme() === 'dark';
  document.querySelectorAll('.theme-icon use').forEach((u) => u.setAttribute('href', dark ? '#i-sun' : '#i-moon'));
  document.querySelectorAll('.theme-label').forEach((l) => { l.textContent = dark ? 'Light mode' : 'Dark mode'; });
}
function toggleTheme() {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch (e) { }
  syncThemeIcons();
}

// ---------------------------------------------------------------------------
// Auth & fetch
// ---------------------------------------------------------------------------
let authMode = 'login';
function getAdminKey() { try { return localStorage.getItem('adminApiKey'); } catch (e) { return null; } }

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === 'signup';
  $('auth-title').textContent = signup ? 'Create your account' : 'Welcome back';
  $('auth-subtitle').textContent = signup ? 'Set up a new business workspace' : 'Sign in to your business dashboard';
  $('auth-btn').textContent = signup ? 'Create account' : 'Sign in';
  $('field-business').classList.toggle('hidden', !signup);
  $('auth-business').required = signup;
  $('auth-error').classList.remove('show');
}
function togglePw() { const i = $('auth-password'); i.type = i.type === 'password' ? 'text' : 'password'; }
function showAuth() { $('auth-overlay').classList.add('open'); setAuthMode('login'); }

async function handleAuth(e) {
  e.preventDefault();
  const btn = $('auth-btn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span> Signing in…';
  $('auth-error').classList.remove('show');
  const email = $('auth-email').value.trim();
  const password = $('auth-password').value;
  const business = $('auth-business').value.trim();
  const url = authMode === 'login' ? '/auth/login' : '/auth/signup';
  const payload = authMode === 'login' ? { email, password } : { email, password, business_name: business };
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(errorDetail(data, 'Authentication failed'));
    localStorage.setItem('adminApiKey', data.access_token);
    localStorage.setItem('userEmail', email);
    if (business) localStorage.setItem('userBusiness', business);
    updateTopbarUser();
    if (authMode === 'signup' && data.admin_api_key) {
      $('apikey-value').textContent = data.admin_api_key;
      $('auth-form-panel').classList.add('hidden');
      $('auth-apikey-panel').classList.remove('hidden');
    } else {
      $('auth-overlay').classList.remove('open');
      loadAll();
      showTab(currentPage);
    }
  } catch (err) {
    $('auth-error').textContent = err.message;
    $('auth-error').classList.add('show');
  }
  btn.disabled = false;
  btn.textContent = authMode === 'login' ? 'Sign in' : 'Create account';
}

function copyApiKey() {
  const val = $('apikey-value').textContent || '';
  const done = () => toast('API key copied');
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(val).then(done).catch(() => fallbackCopy(val, done));
  else fallbackCopy(val, done);
}
function fallbackCopy(text, onSuccess) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { }
  ta.remove();
  if (ok) onSuccess(); else window.prompt('Copy your API key:', text);
}
function dismissApiKeyPanel() {
  $('auth-overlay').classList.remove('open');
  $('auth-apikey-panel').classList.add('hidden');
  $('auth-form-panel').classList.remove('hidden');
  loadAll();
}
function logout() {
  ['adminApiKey', 'userEmail', 'userBusiness', '_impersonating'].forEach((k) => localStorage.removeItem(k));
  showAuth();
}
function exitImpersonation() { logout(); window.close(); }

function updateTopbarUser(nameOverride) {
  const business = nameOverride || localStorage.getItem('userBusiness') || '';
  const email = localStorage.getItem('userEmail') || '';
  $('topbar-user').textContent = business || email || 'Your business';
  $('account-email').textContent = business ? email : '';
  $('account-avatar').textContent = initials(business || email || 'H');
  $('ob-preview-sender-name').textContent = business || 'Your business';
}

async function adminFetch(url, opts) {
  opts = opts || {};
  const k = getAdminKey();
  if (!k) { showAuth(); throw new Error('Not signed in'); }
  opts.headers = Object.assign({}, opts.headers, { Authorization: 'Bearer ' + k });
  const r = await fetch(url, opts);
  if (r.status === 401) { localStorage.removeItem('adminApiKey'); showAuth(); throw new Error('Session expired — please sign in again'); }
  return r;
}
async function safeFetch(url) {
  const r = await adminFetch(url);
  if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
  return r.json();
}
/** FastAPI errors: detail is a string, or a list of validation errors. */
function errorDetail(data, fallback) {
  const d = data && data.detail;
  if (Array.isArray(d)) return d.map((x) => (x.loc ? x.loc[x.loc.length - 1] + ': ' : '') + String(x.msg || '').replace(/^Value error, /, '')).join('; ');
  return typeof d === 'string' && d ? d : fallback;
}
/** JSON request; throws Error(detail) on failure. */
async function api(url, method = 'GET', body) {
  const opts = { method };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
  const r = await adminFetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(errorDetail(data, 'Request failed (HTTP ' + r.status + ')'));
  return data;
}

function showBanner(msg) {
  const b = $('err-banner');
  b.textContent = msg || '';
  b.classList.toggle('hidden', !msg);
}

// ---------------------------------------------------------------------------
// Load everything
// ---------------------------------------------------------------------------
let loading = false;
async function loadAll(manual) {
  if (loading || !getAdminKey()) return;
  loading = true;
  $('last-updated').textContent = 'Refreshing…';
  $('refresh-icon').style.animation = 'spin 0.8s linear infinite';
  const errs = [];
  const tasks = {
    kpis: safeFetch('/analytics/kpis'),
    funnel: safeFetch('/analytics/funnel'),
    orders: safeFetch('/analytics/orders?per_page=200'),
    customers: safeFetch('/analytics/customers?per_page=200'),
    products: safeFetch('/analytics/products'),
    refunds: safeFetch('/admin/refund-requests'),
    receipts: safeFetch('/admin/payment-verifications'),
    bookings: safeFetch('/admin/bookings'),
    agent: safeFetch('/admin/settings/agent_active'),
    outreach: safeFetch('/admin/settings/outreach_enabled'),
    business: safeFetch('/admin/settings/business_name'),
    sources: safeFetch('/admin/catalog/sources'),
  };
  const keys = Object.keys(tasks);
  const results = await Promise.allSettled(Object.values(tasks));
  const res = {};
  keys.forEach((k, i) => {
    if (results[i].status === 'fulfilled') res[k] = results[i].value;
    else if (!['refunds', 'receipts', 'bookings', 'agent', 'outreach', 'business', 'sources'].includes(k)) errs.push(k + ': ' + results[i].reason.message);
  });

  if (res.kpis) renderKPIs(res.kpis);
  if (res.funnel) renderFunnel(res.funnel);
  if (res.orders) { allOrders = res.orders.orders || []; renderOrdersTable(); }
  else $('orders-body').innerHTML = emptyRow(8, 'Could not load orders', '', 'alert');
  if (res.customers) { allCustomers = res.customers.customers || []; renderCustomersTable(); }
  if (res.products) renderProducts(res.products);
  if (res.refunds) { pendingRefunds = (res.refunds.refunds || []).filter((r) => r.status === 'pending').length; setCount('refund-badge', pendingRefunds); }
  if (res.receipts) { pendingReceipts = (res.receipts.verifications || []).filter((v) => v.status === 'pending').length; setCount('receipt-badge', pendingReceipts); }
  if (res.bookings) { allBookings = res.bookings.bookings || []; applyBookingFilter(); }
  if (res.agent) updateAgentActiveUI(res.agent.value !== 'false');
  if (res.outreach) $('tab-btn-outbound').classList.toggle('hidden', (res.outreach.value || '').toLowerCase() !== 'true');
  if (res.business && res.business.value) updateTopbarUser(res.business.value);
  if (res.sources) { applySourcesResponse(res.sources); }

  refreshComputedKPIs();
  renderAnalytics();
  renderAttention();
  $('bottom-more-dot').classList.toggle('hidden', !(pendingReceipts || pendingRefunds));

  if (errs.length) { showBanner('Some data could not be loaded — ' + errs.join(' · ')); $('last-updated').textContent = errs.length + ' error(s)'; }
  else { showBanner(''); $('last-updated').textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); }
  if (manual && !errs.length) toast('Dashboard refreshed', 'info', 1800);
  $('refresh-icon').style.animation = '';
  loading = false;
}
function setCount(id, n) { const el = $(id); el.textContent = n; el.classList.toggle('hidden', !n); }

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
function renderKPIs(k) {
  $('k-cust').textContent = (k.total_customers || 0).toLocaleString();
  $('k-await').textContent = (k.orders_awaiting_payment || 0).toLocaleString();
  $('k-rev').textContent = fmt_currency(k.total_revenue);
  $('k-orders').textContent = (k.paid_orders_count || 0) + ' paid orders';
  $('k-conv').textContent = ((k.overall_conversion_rate || 0) * 100).toFixed(1) + '%';
}

function refreshComputedKPIs() {
  const today = new Date().toDateString();
  const weekAgo = Date.now() - 7 * 86400000;
  const todayOrders = allOrders.filter((o) => parseDate(o.created_at)?.toDateString() === today);
  const todayRev = todayOrders.reduce((s, o) => s + (o.status === 'paid' ? parseFloat(o.total || 0) : 0), 0);
  $('k-today').textContent = todayOrders.length;
  $('k-today-rev').textContent = fmt_currency(todayRev) + ' paid today';
  const paid = allOrders.filter((o) => o.status === 'paid');
  const avg = paid.length ? paid.reduce((s, o) => s + parseFloat(o.total || 0), 0) / paid.length : 0;
  $('k-avg').textContent = avg > 0 ? fmt_currency(avg) : '—';
  const cancelled = allOrders.filter((o) => o.status === 'cancelled').length;
  $('k-cancelled').textContent = cancelled;
  $('k-cancel-rate').textContent = (allOrders.length ? (cancelled / allOrders.length * 100).toFixed(1) : '0.0') + '% of all orders';
  const newWeek = allCustomers.filter((c) => (parseDate(c.first_seen_at)?.getTime() || 0) >= weekAgo).length;
  $('k-cust-new').textContent = '+' + newWeek + ' this week';
}

function last7Days() {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(Date.now() - (6 - i) * 86400000);
    return { key: d.toDateString(), label: d.toLocaleDateString('en', { weekday: 'short' }), full: d.toLocaleDateString('en', { weekday: 'long', month: 'short', day: 'numeric' }) };
  });
}

function colChart(barsId, labelsId, days, values, fmt) {
  const max = Math.max(...values, 0);
  const wrap = $(barsId);
  wrap.innerHTML = '<div class="gridline" style="top:22px"></div><div class="gridline" style="top:calc(22px + (100% - 22px) / 2)"></div>' +
    days.map((d, i) => {
      const pct = max > 0 ? Math.max((values[i] / max) * 100, values[i] > 0 ? 3 : 0) : 0;
      return `<div class="col" data-tip="${esc(d.full)}<strong>${esc(fmt(values[i]))}</strong>"><div class="bar ${values[i] ? '' : 'zero'}" style="height:${values[i] ? pct : 1}%"></div></div>`;
    }).join('');
  $(labelsId).innerHTML = days.map((d) => `<span>${esc(d.label)}</span>`).join('');
}

function hbars(id, rows, emptyText) {
  const max = Math.max(...rows.map((r) => r.value), 0);
  if (!rows.length || max === 0) { $(id).innerHTML = `<div class="empty-state" style="padding:24px 0"><p>${esc(emptyText)}</p></div>`; return; }
  $(id).innerHTML = rows.map((r) => `
    <div class="hbar" data-tip="${esc(r.label)}<strong>${esc(r.display)}</strong>">
      <div class="hbar-label">${r.swatch ? `<span class="swatch" style="background:${r.color}"></span>` : ''}${esc(r.label)}</div>
      <div class="hbar-track"><div class="hbar-fill" style="width:${max ? (r.value / max * 100) : 0}%;background:${r.color || 'var(--series-1)'}"></div></div>
      <div class="hbar-value">${esc(r.display)}</div>
    </div>`).join('');
}

function renderAnalytics() {
  const days = last7Days();
  const rev = Object.fromEntries(days.map((d) => [d.key, 0]));
  allOrders.filter((o) => o.status === 'paid').forEach((o) => { const k = parseDate(o.created_at)?.toDateString(); if (k in rev) rev[k] += parseFloat(o.total || 0); });
  const revVals = days.map((d) => rev[d.key]);
  const total7 = revVals.reduce((a, b) => a + b, 0);
  colChart('chart-rev-bars', 'chart-rev-labels', days, revVals, (v) => fmt_currency(v));
  $('chart-rev-7d').textContent = fmt_currency(total7);
  const prevStart = Date.now() - 14 * 86400000, prevEnd = Date.now() - 7 * 86400000;
  const prev = allOrders.filter((o) => { const t = parseDate(o.created_at)?.getTime() || 0; return o.status === 'paid' && t >= prevStart && t < prevEnd; }).reduce((s, o) => s + parseFloat(o.total || 0), 0);
  const delta = $('chart-rev-delta');
  if (prev > 0) {
    const pct = (total7 - prev) / prev * 100;
    delta.textContent = (pct >= 0 ? '▲ ' : '▼ ') + Math.abs(pct).toFixed(1) + '% vs previous week';
    delta.className = 'delta ' + (pct >= 0 ? 'up' : 'down');
  } else { delta.textContent = 'No sales the week before'; delta.className = 'delta muted'; }

  const cust = Object.fromEntries(days.map((d) => [d.key, 0]));
  allCustomers.forEach((c) => { const k = parseDate(c.first_seen_at)?.toDateString(); if (k in cust) cust[k]++; });
  const custVals = days.map((d) => cust[d.key]);
  colChart('chart-cust-bars', 'chart-cust-labels', days, custVals, (v) => v + ' new customer' + (v === 1 ? '' : 's'));
  $('chart-cust-7d').textContent = custVals.reduce((a, b) => a + b, 0);

  const statusMeta = [
    ['paid', 'Paid', 'var(--status-good)'], ['pending_delivery', 'Pending delivery', 'var(--series-1)'],
    ['awaiting_payment', 'Awaiting payment', 'var(--status-warning)'], ['cancelled', 'Cancelled', 'var(--status-critical)'],
  ];
  const counts = {};
  allOrders.forEach((o) => { counts[o.status] = (counts[o.status] || 0) + 1; });
  hbars('chart-status', statusMeta.map(([k, label, color]) => ({ label, value: counts[k] || 0, display: String(counts[k] || 0), color, swatch: true })), 'No orders yet');

  const payCount = {}, payRev = {};
  allOrders.filter((o) => o.status === 'paid').forEach((o) => { const m = o.payment_method || 'bank_transfer'; payCount[m] = (payCount[m] || 0) + 1; payRev[m] = (payRev[m] || 0) + parseFloat(o.total || 0); });
  const payMeta = [['cod', 'Cash on delivery', 'var(--series-1)'], ['bank_transfer', 'Bank transfer', 'var(--series-2)']];
  hbars('chart-payment', payMeta.map(([k, label, color]) => ({ label, value: payCount[k] || 0, display: (payCount[k] || 0) + ' orders', color, swatch: true })), 'No paid orders yet');
  $('chart-payment-stats').innerHTML = payMeta.filter(([k]) => payRev[k]).map(([k, label, color]) => `<span><span class="swatch" style="background:${color}"></span>${esc(label)}: <strong class="num">${esc(fmt_currency(payRev[k]))}</strong></span>`).join('');

  const prodRev = {};
  allOrders.filter((o) => o.status === 'paid').forEach((o) => (o.line_items || []).forEach((li) => {
    const n = li.name || li.sku || '?';
    prodRev[n] = (prodRev[n] || 0) + parseFloat(li.line_total || (parseFloat(li.unit_price || 0) * (li.quantity || 1)) || 0);
  }));
  const top = Object.entries(prodRev).sort((a, b) => b[1] - a[1]).slice(0, 6);
  hbars('chart-products', top.map(([label, v]) => ({ label, value: v, display: 'PKR ' + fmt_compact(v) })), 'No sales yet');

  const now = Date.now();
  const periods = [['Today', new Date().setHours(0, 0, 0, 0)], ['Last 7 days', now - 7 * 86400000], ['Last 30 days', now - 30 * 86400000]];
  $('chart-period').innerHTML = periods.map(([label, from]) => {
    const ords = allOrders.filter((o) => (parseDate(o.created_at)?.getTime() || 0) >= from);
    const r = ords.filter((o) => o.status === 'paid').reduce((s, o) => s + parseFloat(o.total || 0), 0);
    const n = allCustomers.filter((c) => (parseDate(c.first_seen_at)?.getTime() || 0) >= from).length;
    return `<div class="stat-row"><span class="text-2">${label}</span><span class="v">${esc(fmt_currency(r))}<div class="cell-sub">${ords.length} orders · ${n} new customers</div></span></div>`;
  }).join('');
}

function renderFunnel(data) {
  const wrap = $('funnel-wrap');
  const stages = data.stages || [];
  if (!stages.length) { wrap.innerHTML = '<div class="empty-state" style="padding:24px 0"><p>No customers yet</p></div>'; return; }
  const max = Math.max(...stages.map((s) => s.count), 1);
  const labels = { lead: 'Lead', interested: 'Interested', awaiting_payment: 'Awaiting payment', closed_won: 'Closed won' };
  const ramp = ['var(--seq-250)', 'var(--seq-350)', 'var(--seq-450)', 'var(--seq-550)'];
  wrap.innerHTML = stages.map((s, i) => {
    const rate = s.conversion_rate !== null && s.conversion_rate !== undefined ? (s.conversion_rate * 100).toFixed(0) + '% of previous' : '';
    return `<div data-tip="${esc(labels[s.stage] || s.stage)}<strong>${s.count} customers</strong>">
      <div class="funnel-row-top"><span>${esc(labels[s.stage] || s.stage)}</span><span><strong>${s.count}</strong> <span class="muted" style="font-size:12px">${rate}</span></span></div>
      <div class="funnel-track"><div class="funnel-fill" style="width:${s.count / max * 100}%;background:${ramp[i % ramp.length]}"></div></div>
    </div>`;
  }).join('');
}

function renderAttention() {
  const items = [];
  const add = (iconName, tone, title, text, page) => items.push({ iconName, tone, title, text, page });
  if (pendingReceipts) add('receipt', 'warning', `${pendingReceipts} receipt${pendingReceipts > 1 ? 's' : ''} to verify`, 'Customers are waiting for order confirmation', 'receipts');
  if (pendingRefunds) add('refund', 'danger', `${pendingRefunds} refund request${pendingRefunds > 1 ? 's' : ''}`, 'Approve or reject to notify the customer', 'refunds');
  const awaiting = allOrders.filter((o) => o.status === 'awaiting_payment').length;
  if (awaiting) add('clock', 'info', `${awaiting} order${awaiting > 1 ? 's' : ''} awaiting payment`, 'Bank transfers not yet received', 'orders');
  const failing = allSources.filter((s) => s.status === 'error');
  if (failing.length) add('alert', 'danger', 'Product sync failing', failing[0].last_error || 'Check your product source', 'products');
  const oos = allProducts.filter((p) => p.stock === 0).length;
  if (oos) add('package', 'warning', `${oos} product${oos > 1 ? 's' : ''} out of stock`, 'The agent won’t offer these', 'products');
  if (!allSources.length && !allProducts.length) add('globe', 'info', 'Add your products', 'Connect your website or database so the agent can sell', 'products');
  const upcoming = allBookings.filter((b) => b.status === 'confirmed').length;
  if (upcoming) add('calendar', 'info', `${upcoming} upcoming booking${upcoming > 1 ? 's' : ''}`, 'Confirmed appointments', 'bookings');

  const tones = { warning: ['var(--warning-bg)', 'var(--warning-fg)'], danger: ['var(--danger-bg)', 'var(--danger-fg)'], info: ['var(--info-bg)', 'var(--info-fg)'] };
  $('attention-list').innerHTML = items.length ? items.slice(0, 5).map((it) => `
    <button class="attention-item" onclick="showTab('${it.page}')">
      <span class="a-icon" style="background:${tones[it.tone][0]};color:${tones[it.tone][1]}">${icon(it.iconName)}</span>
      <span class="a-text"><strong>${esc(it.title)}</strong><span>${esc(it.text)}</span></span>
      <svg class="i sm muted" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>
    </button>`).join('')
    : `<div class="empty-state" style="padding:28px 0"><div class="empty-icon" style="background:var(--success-bg);color:var(--success-fg)">${icon('check', '')}</div><h4>All caught up</h4><p>Nothing needs your attention right now.</p></div>`;
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------
function applyOrderFilter() {
  const qs = new URLSearchParams({ per_page: 200 });
  const st = $('orders-status-filter').value, pay = $('orders-pay-filter').value;
  if (st) qs.set('status', st);
  if (pay) qs.set('payment_method', pay);
  $('orders-body').innerHTML = loadingRow(8);
  safeFetch('/analytics/orders?' + qs).then((d) => { allOrders = d.orders || []; renderOrdersTable(); }).catch((e) => toast(e.message, 'error'));
}
function renderOrders(data) { allOrders = data.orders || []; renderOrdersTable(); }

function renderOrdersTable() {
  const q = $('orders-search').value.trim().toLowerCase();
  const list = q ? allOrders.filter((o) => [o.order_ref, o.customer_name, o.customer_wa_id, o.delivery_address, ...(o.line_items || []).map((i) => i.name)].join(' ').toLowerCase().includes(q)) : allOrders;
  $('orders-count').textContent = list.length;
  if (!list.length) { $('orders-body').innerHTML = emptyRow(8, q ? 'No matching orders' : 'No orders yet', q ? 'Try a different search.' : 'Orders your agent takes on WhatsApp show up here.', 'orders'); return; }
  const cancellable = ['awaiting_payment', 'pending_delivery', 'paid'];
  $('orders-body').innerHTML = list.map((o) => {
    const items = (o.line_items || []).map((i) => `${esc(i.name || i.sku)} <span class="muted">×${esc(i.quantity)}</span>`).join('<br>') || '—';
    const action = cancellable.includes(o.status)
      ? `<button class="btn xs danger-ghost" onclick="adminCancelOrder(${jsArg(o.order_ref)}, ${o.status === 'paid'})">Cancel${o.status === 'paid' ? ' & refund' : ''}</button>` : '';
    return `<tr>
      <td class="primary"><div class="ref">${esc(o.order_ref)}</div><div class="cell-sub">${esc(o.delivery_address || 'No address')}</div></td>
      <td data-label="Customer"><div><div class="cell-main">${esc(o.customer_name || 'Unknown')}</div><div class="cell-sub mono">${esc(o.customer_wa_id)}</div></div></td>
      <td data-label="Items" class="cell-wrap"><div>${items}</div></td>
      <td data-label="Total" class="right num cell-main">${esc(fmt_currency(o.total))}</td>
      <td data-label="Payment">${pay_badge(o.payment_method)}</td>
      <td data-label="Status">${status_badge(o.status)}</td>
      <td data-label="Date" class="muted" style="white-space:nowrap">${esc(fmt_date(o.created_at))}</td>
      <td class="actions"><div class="row-actions">${action}</div></td>
    </tr>`;
  }).join('');
}

async function adminCancelOrder(orderRef, isPaid) {
  const res = await dialog({
    title: `Cancel order ${orderRef}?`,
    message: isPaid
      ? 'This order is paid. The customer will be told on WhatsApp that a full refund is coming, and a refund request will be created.'
      : 'Stock is restored and the customer’s CRM stage rolls back. The customer is notified on WhatsApp.',
    confirmText: 'Cancel order', cancelText: 'Keep order', danger: true,
    input: { label: 'Reason (sent to the customer, optional)', placeholder: 'e.g. Item no longer available' },
  });
  if (!res) return;
  try {
    const d = await api('/admin/orders/' + encodeURIComponent(orderRef) + '/cancel', 'POST', { reason: res.value.trim() || null });
    toast(`Order ${orderRef} cancelled` + (d.refund_created ? ' · refund request created' : '') + (d.customer_notified ? ' · customer notified' : ''));
    loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

// ---------------------------------------------------------------------------
// Customers
// ---------------------------------------------------------------------------
function applyCustomerFilter() {
  const qs = new URLSearchParams({ per_page: 200 });
  const st = $('cust-stage-filter').value;
  if (st) qs.set('stage', st);
  $('customers-body').innerHTML = loadingRow(6);
  safeFetch('/analytics/customers?' + qs).then((d) => { allCustomers = d.customers || []; renderCustomersTable(); }).catch((e) => toast(e.message, 'error'));
}
function renderCustomers(data) { allCustomers = data.customers || []; renderCustomersTable(); }

function renderCustomersTable() {
  const q = $('customers-search').value.trim().toLowerCase();
  const list = q ? allCustomers.filter((c) => [c.name, c.wa_id, c.delivery_address].join(' ').toLowerCase().includes(q)) : allCustomers;
  $('customers-count').textContent = list.length;
  if (!list.length) { $('customers-body').innerHTML = emptyRow(6, q ? 'No matching customers' : 'No customers yet', q ? '' : 'Anyone who messages your WhatsApp number appears here.', 'users'); return; }
  $('customers-body').innerHTML = list.map((c) => `<tr>
    <td class="primary"><div class="inline" style="gap:10px;flex-wrap:nowrap"><span class="avatar">${esc(initials(c.name || c.wa_id))}</span><div><div class="cell-main">${esc(c.name || 'Unknown')}</div><div class="cell-sub mono">${esc(c.wa_id)}</div></div></div></td>
    <td data-label="Stage">${stage_badge(c.crm_stage)}</td>
    <td data-label="Opt-in">${opt_badge(c.opt_in_status)}</td>
    <td data-label="Address" class="cell-wrap">${esc(c.delivery_address || '—')}</td>
    <td data-label="First seen" class="muted" style="white-space:nowrap">${esc(fmt_date(c.first_seen_at))}</td>
    <td data-label="Last message" class="muted" style="white-space:nowrap">${esc(fmt_relative(c.last_inbound_at))}</td>
  </tr>`).join('');
}

// ---------------------------------------------------------------------------
// Products
// ---------------------------------------------------------------------------
function renderProducts(data) {
  allProducts = data.products || [];
  productsBySku = {};
  allProducts.forEach((p) => { productsBySku[p.sku] = p; });
  $('k-oos').textContent = data.out_of_stock_count;
  $('k-lowstock').textContent = data.low_stock_count + ' running low (≤5)';
  setCount('nav-count-products', 0);
  renderProductGrid();
}

document.querySelectorAll('#product-filter button').forEach((b) => b.addEventListener('click', () => {
  productFilter = b.dataset.filter;
  document.querySelectorAll('#product-filter button').forEach((x) => x.classList.toggle('active', x === b));
  renderProductGrid();
}));

function sourceById(id) { return allSources.find((s) => s.id === id); }

function renderProductGrid() {
  const q = $('products-search').value.trim().toLowerCase();
  let list = allProducts.filter((p) => {
    if (productFilter === 'in' && p.stock <= 0) return false;
    if (productFilter === 'out' && p.stock > 0) return false;
    if (productFilter === 'synced' && !p.catalog_source_id) return false;
    if (productFilter === 'manual' && p.catalog_source_id) return false;
    if (!q) return true;
    return [p.name, p.sku, p.description, (p.tags || []).join(' '), JSON.stringify(p.options || {})].join(' ').toLowerCase().includes(q);
  });
  $('inventory-count').textContent = list.length === allProducts.length ? `${allProducts.length} products` : `${list.length} of ${allProducts.length} products`;
  const grid = $('inventory-body');
  if (!list.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-icon">${icon('package', '')}</div><h4>${allProducts.length ? 'No matching products' : 'No products yet'}</h4><p>${allProducts.length ? 'Try a different search or filter.' : 'Connect your website or database above, or add a product by hand.'}</p></div>`;
    return;
  }
  list = list.slice(0, 600);
  grid.innerHTML = list.map((p) => {
    const cur = p.currency || 'PKR';
    const imgs = (p.images && p.images.length) ? p.images : (p.image_url ? [p.image_url] : []);
    const placeholder = `<svg class="i ph"><use href="#i-${!imgs.length && p.video_url ? 'play' : 'image'}"/></svg>`;
    const media = placeholder + (imgs.length ? `<img src="${esc(imgs[0])}" alt="" loading="lazy" onerror="this.remove()">` : '');
    const tags = [];
    if (p.compare_at_price && parseFloat(p.compare_at_price) > parseFloat(p.price)) tags.push(badge('Sale', 'danger', true));
    if (p.catalog_source_id) { const s = sourceById(p.catalog_source_id); tags.push(badge(s && s.kind !== 'website' ? 'Database' : 'Website', 'brand', true)); }
    const stockCls = p.stock === 0 ? 'stock-out' : p.stock <= 5 ? 'stock-low' : 'stock-ok';
    const stockText = !p.active && p.stock > 0 ? 'Hidden' : p.stock === 0 ? 'Out of stock' : p.stock <= 5 ? `Only ${p.stock} left` : `${p.stock} in stock`;
    const opt = Object.entries(p.options || {})[0];
    const chips = opt ? `<div class="chips">${opt[1].slice(0, 5).map((v) => `<span class="chip">${esc(v)}</span>`).join('')}${opt[1].length > 5 ? `<span class="chip">+${opt[1].length - 5}</span>` : ''}</div>` : '';
    return `<article class="product" role="button" tabindex="0" onclick="openEditProduct(${jsArg(p.sku)})" onkeydown="if(event.key==='Enter')openEditProduct(${jsArg(p.sku)})">
      <div class="product-media">${media}<div class="tags">${tags.join('')}</div>${imgs.length > 1 ? `<span class="photo-count">${icon('image', 'sm')}${imgs.length}</span>` : ''}</div>
      <div class="product-body">
        <div class="product-name">${esc(p.name)}</div>
        <div class="product-sku">${esc(p.sku)}</div>
        <div class="price"><strong>${esc(fmt_currency(p.price, cur))}</strong>${p.compare_at_price && parseFloat(p.compare_at_price) > parseFloat(p.price) ? `<s>${esc(fmt_currency(p.compare_at_price, cur))}</s>` : ''}</div>
        ${chips}
        <div class="product-foot"><span class="stock-label ${stockCls}">${esc(stockText)}</span>${p.source_url ? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" class="btn ghost icon sm" title="Open product page">${icon('link')}</a>` : ''}</div>
      </div>
    </article>`;
  }).join('');
}

function resetProductModal() {
  $('product-form').reset();
  $('product-form-error').classList.remove('show');
  $('pm-synced-note').classList.add('hidden');
  $('pm-gallery').classList.add('hidden');
  $('pm-variants').classList.add('hidden');
  $('pm-sku').disabled = false;
  $('pm-delete').classList.add('hidden');
}

function openAddProduct() {
  currentProductSku = null;
  resetProductModal();
  $('product-modal-title').textContent = 'Add product';
  $('product-modal-sub').textContent = 'Products you add here are available to the agent immediately.';
  $('pm-submit').textContent = 'Create product';
  openModal('product-modal');
}

function openEditProduct(sku) {
  const p = productsBySku[sku];
  if (!p) return;
  currentProductSku = sku;
  resetProductModal();
  $('product-modal-title').textContent = p.name;
  $('product-modal-sub').textContent = p.last_synced_at ? 'Last synced ' + fmt_relative(p.last_synced_at) : 'Added manually';
  $('pm-sku').value = p.sku; $('pm-sku').disabled = true;
  $('pm-name').value = p.name || '';
  $('pm-price').value = parseFloat(p.price);
  $('pm-desc').value = p.description || '';
  $('pm-stock').value = p.stock;
  $('pm-tags').value = (p.tags || []).join(', ');
  $('pm-image').value = p.image_url || '';
  $('pm-video').value = p.video_url || '';
  $('pm-delete').classList.remove('hidden');
  $('pm-submit').textContent = 'Save changes';

  if (p.catalog_source_id) {
    const s = sourceById(p.catalog_source_id);
    const note = $('pm-synced-note');
    note.querySelector('span').innerHTML = `Synced from <strong>${esc(s ? s.url : 'a connected source')}</strong>. Name, price, description, photos and stock are refreshed on every sync, so edit them at the source. ${p.source_url ? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener">View product page</a>` : ''}`;
    note.classList.remove('hidden');
  }
  const imgs = p.images || [];
  if (imgs.length > 1) { $('pm-gallery').innerHTML = imgs.map((u) => `<img src="${esc(u)}" alt="" loading="lazy">`).join(''); $('pm-gallery').classList.remove('hidden'); }
  const opts = Object.entries(p.options || {});
  if (opts.length || (p.variants || []).length) {
    const cur = p.currency || 'PKR';
    let html = opts.map(([k, vals]) => `<div class="field"><span class="label">${esc(k)}</span><div class="chips">${vals.map((v) => `<span class="chip">${esc(v)}</span>`).join('')}</div></div>`).join('');
    if ((p.variants || []).length) {
      html += `<div class="table-wrap" style="margin-top:12px"><table class="variant-table"><thead><tr><th>Option</th><th>Price</th><th>Availability</th></tr></thead><tbody>${p.variants.slice(0, 50).map((v) => `<tr><td>${esc(v.name)}</td><td class="num">${v.price ? esc(fmt_currency(v.price, cur)) : '—'}</td><td>${v.available === false ? badge('Sold out', 'danger') : badge('Available', 'success')}</td></tr>`).join('')}</tbody></table></div>`;
    }
    $('pm-variants').innerHTML = `<div class="label" style="margin-bottom:8px">Options the agent can offer</div><div class="stack" style="gap:10px">${html}</div>`;
    $('pm-variants').classList.remove('hidden');
  }
  openModal('product-modal');
}

async function submitProduct(e) {
  e.preventDefault();
  const btn = $('pm-submit'), err = $('product-form-error');
  err.classList.remove('show');
  btn.disabled = true;
  const label = btn.textContent;
  btn.innerHTML = '<span class="spinner sm"></span> Saving…';
  const tags = $('pm-tags').value.split(',').map((t) => t.trim()).filter(Boolean);
  const name = $('pm-name').value.trim(), description = $('pm-desc').value.trim();
  const price = parseFloat($('pm-price').value), stock = parseInt($('pm-stock').value, 10) || 0;
  const image = $('pm-image').value.trim() || null, video = $('pm-video').value.trim() || null;
  try {
    let sku = currentProductSku;
    if (!sku) {
      const created = await api('/admin/products', 'POST', { sku: $('pm-sku').value.trim(), name, description, price, stock, tags });
      sku = created.sku;
    } else {
      const p = productsBySku[sku];
      await api('/admin/products/' + encodeURIComponent(sku), 'PATCH', { name, description, price, tags });
      if (stock !== p.stock) await api('/admin/products/' + encodeURIComponent(sku) + '/stock', 'PATCH', { stock });
    }
    const p = productsBySku[sku] || {};
    if (image !== (p.image_url || null) || video !== (p.video_url || null)) {
      await api('/admin/products/' + encodeURIComponent(sku) + '/media', 'PATCH', { image_url: image, video_url: video });
    }
    const file = $('pm-file').files[0];
    if (file) { const fd = new FormData(); fd.append('file', file); await api('/admin/products/' + encodeURIComponent(sku) + '/media/upload', 'POST', fd); }
    closeModal('product-modal');
    toast(currentProductSku ? 'Product updated' : 'Product created');
    loadAll();
  } catch (ex) {
    err.textContent = ex.message; err.classList.add('show');
  }
  btn.disabled = false; btn.textContent = label;
}

async function deleteProduct(sku) {
  const p = productsBySku[sku];
  const res = await dialog({
    title: 'Delete this product?',
    message: `“${p ? p.name : sku}” will be removed permanently. Past orders keep their record.` + (p && p.catalog_source_id ? '\n\nIt will come back on the next sync if it’s still on your source.' : ''),
    confirmText: 'Delete', danger: true,
  });
  if (!res) return;
  try {
    await api('/admin/products/' + encodeURIComponent(sku), 'DELETE');
    closeModal('product-modal');
    toast('Product deleted');
    loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

// ---------------------------------------------------------------------------
// Product sources (website / database sync)
// ---------------------------------------------------------------------------
let sourcePollTimer = null;
let sourceKind = 'website';
let editingSourceId = null;
let lastTestColumns = [];

function intervalLabel(m) {
  if (m < 60) return `${m} minutes`;
  if (m < 1440) return m === 60 ? 'hour' : `${m / 60} hours`;
  return 'day';
}

async function loadSources() {
  try { applySourcesResponse(await safeFetch('/admin/catalog/sources')); }
  catch (e) { $('sources-list').innerHTML = `<div class="notice danger">${icon('alert')}<span>${esc(e.message)}</span></div>`; }
}

function applySourcesResponse(data) {
  const prev = Object.fromEntries(allSources.map((s) => [s.id, s.status]));
  const prevSynced = Object.fromEntries(allSources.map((s) => [s.id, s.last_synced_at]));
  allSources = data.sources || [];
  if (data.interval_choices) intervalChoices = data.interval_choices;
  if (data.mappable_fields) mappableFields = data.mappable_fields;
  renderSources();
  let finished = false;
  allSources.forEach((s) => {
    if (prev[s.id] === 'syncing' && s.status !== 'syncing') {
      finished = true;
      if (s.status === 'ok') {
        const st = s.last_stats || {};
        toast(`Synced ${s.product_count} products from ${shortUrl(s)} · ${st.added || 0} new, ${st.updated || 0} updated, ${st.removed || 0} removed`);
      } else if (s.status === 'error') toast(`Sync failed for ${shortUrl(s)}: ${s.last_error || 'unknown error'}`, 'error', 8000);
    }
  });
  // Live (Realtime) syncs can start and finish between polls — refresh products
  // whenever a source reports a newer sync time.
  const resynced = allSources.some((s) => s.id in prevSynced && s.last_synced_at && s.last_synced_at !== prevSynced[s.id]);
  if (finished || resynced) safeFetch('/analytics/products').then(renderProducts).catch(() => { });
  clearTimeout(sourcePollTimer);
  if (allSources.some((s) => s.status === 'syncing')) sourcePollTimer = setTimeout(loadSources, 3000);
  else if (currentPage === 'products' && allSources.some((s) => s.realtime && ['live', 'connecting'].includes(s.realtime.state))) {
    sourcePollTimer = setTimeout(() => { if (!document.hidden && currentPage === 'products') loadSources(); }, 8000);
  }
}

function realtimeBadge(s) {
  const rt = s.realtime;
  if (!rt || !s.enabled) return '';
  if (rt.state === 'live') return `<span class="badge success live-badge" data-tip="Changes in Supabase sync within seconds${rt.last_event_at ? '<strong>Last change ' + esc(fmt_relative(rt.last_event_at)) + '</strong>' : ''}">Live</span>`;
  if (rt.state === 'connecting') return `<span class="badge info plain"><span class="spinner sm" style="width:10px;height:10px;border-width:1.5px"></span>Going live</span>`;
  if (rt.state === 'not_enabled') return badge('Live updates off', 'warning');
  if (rt.state === 'error') return badge('Live reconnecting', 'warning');
  return '';
}

function realtimeNotice(s) {
  const rt = s.realtime;
  if (!rt || !s.enabled || s.status === 'error') return '';
  if (rt.state === 'not_enabled') {
    return `<div class="notice warning source-error">${icon('info')}<span><strong>Turn on Realtime to get instant updates.</strong> ${esc(rt.detail)} Until then, products sync every ${esc(intervalLabel(s.sync_interval_minutes))}.</span></div>`;
  }
  if (rt.state === 'error' && rt.detail) return `<div class="notice warning source-error">${icon('info')}<span>${esc(rt.detail)} Scheduled syncs continue meanwhile.</span></div>`;
  return '';
}

function shortUrl(s) {
  if (s.kind === 'supabase') return 'Supabase · ' + ((s.config || {}).table || '');
  if (s.kind === 'postgres') return 'PostgreSQL · ' + ((s.config || {}).table || 'custom query');
  return s.url.replace(/^https?:\/\//, '');
}

function renderSources() {
  const list = $('sources-list');
  if (!allSources.length) {
    list.innerHTML = `<div class="empty-state" style="padding:20px 0"><div class="empty-icon">${icon('globe', '')}</div><h4>No product source connected</h4><p>Connect your online store, Supabase or PostgreSQL database. Every product, size, price and photo is imported and kept up to date.</p><button class="btn primary sm" style="margin-top:14px" onclick="openSourceModal()">${icon('plus')}Connect source</button></div>`;
    return;
  }
  const platformNames = { shopify: 'Shopify', woocommerce: 'WooCommerce', generic: 'Website', supabase: 'Supabase', postgres: 'PostgreSQL' };
  list.innerHTML = allSources.map((s) => {
    const syncing = s.status === 'syncing';
    let status;
    if (!s.enabled) status = badge('Paused', '');
    else if (syncing) status = `<span class="badge info plain"><span class="spinner sm" style="width:10px;height:10px;border-width:1.5px"></span>Syncing</span>`;
    else if (s.status === 'ok') status = badge('Synced', 'success');
    else if (s.status === 'error') status = badge('Sync failed', 'danger');
    else status = badge('Waiting', '');
    const st = s.last_stats || {};
    const meta = [
      s.platform ? esc(platformNames[s.platform] || s.platform) : null,
      `<span class="num">${s.product_count}</span> products`,
      s.last_synced_at ? 'Synced ' + esc(fmt_relative(s.last_synced_at)) : 'Not synced yet',
      s.realtime && s.realtime.state === 'live' ? 'Instant updates on' : (s.enabled && s.next_sync_at && !syncing ? 'Next ' + esc(fmt_relative(s.next_sync_at)) : null),
      st.duration_s ? `${st.duration_s}s` : null,
    ].filter(Boolean).map((m) => `<span>${m}</span>`).join('');
    const warnings = (st.warnings || []).length && s.status === 'ok' ? `<div class="notice warning source-error">${icon('info')}<span>${esc(st.warnings.join(' '))}</span></div>` : '';
    return `<div class="source">
      <div class="source-icon">${icon(s.kind === 'website' ? 'globe' : 'database', '')}</div>
      <div style="min-width:0">
        <div class="source-title"><span class="url" title="${esc(s.url)}">${esc(shortUrl(s))}</span>${status}${realtimeBadge(s)}</div>
        <div class="source-meta">${meta}</div>
      </div>
      <div class="source-controls">
        <select class="select" aria-label="Sync interval" onchange="updateSource(${s.id}, {sync_interval_minutes: parseInt(this.value, 10)})">
          ${intervalChoices.map((m) => `<option value="${m}" ${m === s.sync_interval_minutes ? 'selected' : ''}>Every ${intervalLabel(m)}</option>`).join('')}
        </select>
        <button class="btn sm" onclick="syncSourceNow(${s.id})" ${syncing ? 'disabled' : ''}>${icon('refresh')}<span class="hide-sm">Sync now</span></button>
        ${s.kind !== 'website' ? `<button class="btn sm icon" title="Edit column mapping" onclick="openSourceModal(${s.id})">${icon('edit')}</button>` : ''}
        <button class="btn sm icon" title="${s.enabled ? 'Pause syncing' : 'Resume syncing'}" onclick="updateSource(${s.id}, {enabled: ${!s.enabled}})">${icon(s.enabled ? 'pause' : 'play')}</button>
        <button class="btn sm icon danger-ghost" title="Disconnect" onclick="removeSource(${s.id})">${icon('trash')}</button>
      </div>
      ${s.status === 'error' && s.last_error ? `<div class="notice danger source-error">${icon('alert')}<span>${esc(s.last_error)}</span></div>` : warnings}
      ${realtimeNotice(s)}
    </div>`;
  }).join('');
}

async function updateSource(id, body) {
  try {
    await api('/admin/catalog/sources/' + id, 'PATCH', body);
    if ('enabled' in body) toast(body.enabled ? 'Syncing resumed' : 'Syncing paused');
    else if ('sync_interval_minutes' in body) toast('Sync schedule updated');
    loadSources();
  } catch (e) { toast(e.message, 'error'); }
}

async function syncSourceNow(id) {
  try {
    const d = await api('/admin/catalog/sources/' + id + '/sync', 'POST');
    toast(d.already_running ? 'A sync is already running' : 'Sync started — products update in a moment', 'info');
    const s = sourceById(id); if (s) s.status = 'syncing';
    renderSources();
    clearTimeout(sourcePollTimer); sourcePollTimer = setTimeout(loadSources, 2000);
  } catch (e) { toast(e.message, 'error'); }
}

async function removeSource(id) {
  const s = sourceById(id);
  const res = await dialog({
    title: 'Disconnect this source?',
    message: `${shortUrl(s)} will stop syncing. Its ${s.product_count} products are removed from the agent's catalog unless you keep them.`,
    confirmText: 'Disconnect', danger: true,
    checkbox: { label: 'Keep its products as manual products' },
  });
  if (!res) return;
  try {
    const d = await api('/admin/catalog/sources/' + id + '?keep_products=' + (res.checked ? 'true' : 'false'), 'DELETE');
    toast(`Disconnected · ${d.products_affected} products ${d.kept ? 'kept' : 'removed'}`);
    loadAll();
  } catch (e) { toast(e.message, 'error'); }
}

function setSourceKind(kind) {
  sourceKind = kind;
  document.querySelectorAll('#source-kind-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.kind === kind));
  document.querySelectorAll('[data-kind-panel]').forEach((p) => p.classList.toggle('hidden', p.dataset.kindPanel !== kind));
  $('src-db-extra').classList.toggle('hidden', kind === 'website');
  $('src-submit').textContent = editingSourceId ? 'Save changes' : (kind === 'website' ? 'Connect & import' : 'Connect & import');
}
document.querySelectorAll('#source-kind-tabs button').forEach((b) => b.addEventListener('click', () => { if (!editingSourceId) { setSourceKind(b.dataset.kind); resetTestResult(); } }));

function resetTestResult() {
  $('src-test-result').classList.add('hidden');
  $('src-test-summary').textContent = '';
  $('src-mapping').innerHTML = '';
  lastTestColumns = [];
}

function openSourceModal(editId) {
  editingSourceId = editId || null;
  ['src-url', 'src-sb-url', 'src-sb-key', 'src-sb-table', 'src-sb-select', 'src-pg-dsn', 'src-pg-table', 'src-pg-query', 'src-image-base', 'src-url-template'].forEach((id) => { $(id).value = ''; $(id).disabled = false; });
  $('source-form-error').classList.remove('show');
  resetTestResult();
  $('src-interval').innerHTML = intervalChoices.map((m) => `<option value="${m}" ${m === 60 ? 'selected' : ''}>Every ${intervalLabel(m)}</option>`).join('');
  const s = editingSourceId ? sourceById(editingSourceId) : null;
  $('source-kind-tabs').classList.toggle('hidden', !!s);
  $('source-modal-title').textContent = s ? 'Edit ' + shortUrl(s) : 'Connect a product source';
  if (s) {
    const c = s.config || {};
    $('src-interval').value = s.sync_interval_minutes;
    $('src-image-base').value = c.image_base_url || '';
    $('src-url-template').value = c.product_url_template || '';
    if (s.kind === 'supabase') { $('src-sb-url').value = c.project_url || ''; $('src-sb-url').disabled = true; $('src-sb-table').value = c.table || ''; $('src-sb-select').value = c.select || ''; $('src-sb-key').placeholder = 'Saved — leave blank to keep'; }
    if (s.kind === 'postgres') { $('src-pg-dsn').placeholder = 'Saved — leave blank to keep'; $('src-pg-table').value = c.table || ''; $('src-pg-query').value = c.query || ''; }
    setSourceKind(s.kind);
  } else {
    $('src-sb-key').placeholder = 'eyJhbGciOi…';
    $('src-pg-dsn').placeholder = 'postgresql://user:password@host:5432/database';
    setSourceKind('website');
  }
  openModal('source-modal');
  if (s) testSourceConnection();
}

function currentMapping() {
  const m = {};
  document.querySelectorAll('#src-mapping select').forEach((sel) => { m[sel.dataset.field] = sel.value || null; });
  return Object.keys(m).length ? m : null;
}

function sourcePayload() {
  const base = {
    kind: sourceKind,
    sync_interval_minutes: parseInt($('src-interval').value, 10),
    image_base_url: $('src-image-base').value.trim() || null,
    product_url_template: $('src-url-template').value.trim() || null,
    mapping: currentMapping(),
  };
  if (sourceKind === 'website') return { kind: 'website', url: $('src-url').value.trim(), sync_interval_minutes: base.sync_interval_minutes };
  if (sourceKind === 'supabase') return { ...base, url: $('src-sb-url').value.trim(), api_key: $('src-sb-key').value.trim() || null, table: $('src-sb-table').value.trim(), select: $('src-sb-select').value.trim() || null };
  return { ...base, connection_string: $('src-pg-dsn').value.trim() || null, table: $('src-pg-table').value.trim() || null, query: $('src-pg-query').value.trim() || null };
}

const FIELD_LABELS = {
  id: 'Unique ID', name: 'Name', price: 'Price', compare_at_price: 'Original price (sale)', description: 'Description', sku: 'SKU',
  images: 'Images', sizes: 'Sizes', colors: 'Colours', options: 'Other options (JSON)', variants: 'Variants', stock: 'Stock quantity',
  available: 'In stock / active', url: 'Product link or slug', category: 'Category', tags: 'Tags', currency: 'Currency',
};

async function testSourceConnection() {
  const btn = $('src-test-btn'), err = $('source-form-error');
  err.classList.remove('show');
  btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span> Testing…';
  $('src-test-summary').textContent = '';
  try {
    const body = sourcePayload();
    if (editingSourceId) body.source_id = editingSourceId;
    const d = await api('/admin/catalog/sources/test', 'POST', body);
    lastTestColumns = d.columns;
    $('src-test-summary').innerHTML = `<span class="badge success">Connected</span> Read <strong>${d.rows_read}</strong> rows · <strong>${d.products_found}</strong> sellable products`;
    $('src-preview').innerHTML = d.preview.length ? d.preview.map((p) => `<div class="preview-item">${p.image ? `<img src="${esc(p.image)}" alt="" loading="lazy">` : '<span class="ph"></span>'}<div><strong>${esc(p.name)}</strong><span>${p.price ? esc(fmt_currency(p.price, p.currency)) : 'No price'}${Object.keys(p.options || {}).length ? ' · ' + esc(Object.entries(p.options).map(([k, v]) => k + ': ' + v.slice(0, 4).join('/')).join(', ')) : ''}</span></div></div>`).join('') : '<p class="hint">No products could be built from the rows — check the mapping below.</p>';
    const fields = mappableFields.length ? mappableFields : Object.keys(FIELD_LABELS);
    $('src-mapping').innerHTML = fields.map((f) => `<div class="field"><label>${esc(FIELD_LABELS[f] || f)}${['name', 'price'].includes(f) ? ' *' : ''}</label><select class="select" data-field="${esc(f)}"><option value="">— not mapped —</option>${d.columns.map((c) => `<option value="${esc(c)}" ${d.mapping[f] === c ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></div>`).join('');
    $('src-test-result').classList.remove('hidden');
  } catch (e) {
    err.textContent = e.message; err.classList.add('show');
  }
  btn.disabled = false; btn.innerHTML = icon('bolt') + 'Test connection';
}

async function submitSource() {
  const btn = $('src-submit'), err = $('source-form-error');
  err.classList.remove('show');
  const label = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span> Connecting…';
  try {
    const body = sourcePayload();
    if (editingSourceId) {
      const patch = {
        sync_interval_minutes: body.sync_interval_minutes, mapping: body.mapping,
        image_base_url: body.image_base_url || '', product_url_template: body.product_url_template || '',
      };
      if (sourceKind === 'supabase') Object.assign(patch, { table: body.table, select: body.select || '', api_key: body.api_key });
      if (sourceKind === 'postgres') Object.assign(patch, { table: body.table, query: body.query || '', connection_string: body.connection_string });
      await api('/admin/catalog/sources/' + editingSourceId, 'PATCH', patch);
      await api('/admin/catalog/sources/' + editingSourceId + '/sync', 'POST');
      toast('Source updated — re-syncing now', 'info');
    } else {
      await api('/admin/catalog/sources', 'POST', body);
      toast('Connected! Importing your products…', 'info');
    }
    closeModal('source-modal');
    showTab('products');
  } catch (e) {
    err.textContent = e.message; err.classList.add('show');
  }
  btn.disabled = false; btn.textContent = label;
}

// ---------------------------------------------------------------------------
// Bookings
// ---------------------------------------------------------------------------
async function loadBookings() {
  try { allBookings = (await safeFetch('/admin/bookings')).bookings || []; applyBookingFilter(); }
  catch (e) { $('bookings-body').innerHTML = emptyRow(7, 'Could not load bookings', e.message, 'alert'); }
}
function applyBookingFilter() {
  const st = $('bookings-status-filter').value;
  renderBookings(st ? allBookings.filter((b) => b.status === st) : allBookings);
}
function renderBookings(list) {
  $('bookings-count').textContent = list.length;
  setCount('booking-badge', allBookings.filter((b) => b.status === 'confirmed').length);
  if (!list.length) { $('bookings-body').innerHTML = emptyRow(7, 'No bookings yet', 'Appointments your agent books appear here.', 'calendar'); return; }
  const tone = { confirmed: 'info', completed: 'success', cancelled: 'danger' };
  $('bookings-body').innerHTML = list.map((b) => {
    const acts = [];
    if (b.status !== 'completed') acts.push(`<button class="btn xs" onclick="updateBookingStatus(${b.id}, 'completed')">${icon('check')}Done</button>`);
    if (b.status !== 'cancelled') acts.push(`<button class="btn xs danger-ghost" onclick="updateBookingStatus(${b.id}, 'cancelled')">Cancel</button>`);
    if (b.status !== 'confirmed') acts.push(`<button class="btn xs" onclick="updateBookingStatus(${b.id}, 'confirmed')">Reopen</button>`);
    return `<tr>
      <td class="primary"><div class="cell-main">${esc(b.title || 'Consultation')}</div><div class="cell-sub ref">${esc(b.booking_ref)}</div></td>
      <td data-label="When" class="cell-main" style="white-space:nowrap">${esc(b.start_time)}</td>
      <td data-label="Format">${badge((b.meeting_type || 'call').replace(/_/g, ' '), '', true)}</td>
      <td data-label="Customer"><div><div>${esc(b.customer_name || 'Guest')}</div><div class="cell-sub mono">${esc(b.customer_phone || '')}</div></div></td>
      <td data-label="Status">${badge(b.status[0].toUpperCase() + b.status.slice(1), tone[b.status] || '')}</td>
      <td data-label="Notes" class="cell-wrap">${esc(b.notes || '—')}</td>
      <td class="actions"><div class="row-actions">${acts.join('')}</div></td>
    </tr>`;
  }).join('');
}
async function updateBookingStatus(id, status) {
  const res = await dialog({ title: `Mark booking as ${status}?`, confirmText: 'Update', danger: status === 'cancelled' });
  if (!res) return;
  try { await api('/admin/bookings/' + id + '/status', 'PATCH', { status }); toast('Booking updated'); loadBookings(); }
  catch (e) { toast(e.message, 'error'); }
}

// ---------------------------------------------------------------------------
// Receipts & refunds
// ---------------------------------------------------------------------------
async function loadReceipts() {
  $('receipts-body').innerHTML = loadingRow(9);
  try { renderReceipts(await safeFetch('/admin/payment-verifications')); }
  catch (e) { $('receipts-body').innerHTML = emptyRow(9, 'Could not load receipts', e.message, 'alert'); }
}
function renderReceipts(data) {
  const list = data.verifications || [];
  pendingReceipts = list.filter((v) => v.status === 'pending').length;
  setCount('receipt-badge', pendingReceipts);
  $('receipts-count').textContent = list.length + ' total';
  if (!list.length) { $('receipts-body').innerHTML = emptyRow(9, 'No receipts to review', 'Payment screenshots the agent can’t confirm land here.', 'receipt'); return; }
  const tone = { pending: ['warning', 'Pending'], approved: ['success', 'Approved'], resend_requested: ['info', 'Resend requested'] };
  $('receipts-body').innerHTML = list.map((v) => {
    const [t, l] = tone[v.status] || ['danger', v.status];
    const acts = v.status === 'pending' ? `<button class="btn xs primary" onclick="adminVerifyPayment(${v.id})">${icon('check')}Approve</button><button class="btn xs" onclick="adminRequestResend(${v.id})">Ask to resend</button>` : '';
    return `<tr>
      <td class="primary">${v.image_path ? `<a href="${esc(v.image_path)}" target="_blank" rel="noopener"><img class="thumb" src="${esc(v.image_path)}" alt="Receipt" loading="lazy"></a>` : '<span class="muted">No image</span>'}</td>
      <td data-label="Customer"><div><div class="cell-main">${esc(v.customer_name || 'Unknown')}</div><div class="cell-sub mono">${esc(v.customer_wa_id || '')}</div></div></td>
      <td data-label="Order" class="ref">${esc(v.order_ref)}</td>
      <td data-label="Receipt amount" class="right num">${v.ocr_amount ? esc(fmt_currency(v.ocr_amount)) : '—'}</td>
      <td data-label="Order total" class="right num cell-main">${v.order_total ? esc(fmt_currency(v.order_total)) : '—'}</td>
      <td data-label="Reason" class="cell-wrap">${esc(v.fail_reason || '—')}</td>
      <td data-label="Status">${badge(l, t)}</td>
      <td data-label="Date" class="muted" style="white-space:nowrap">${esc(fmt_date(v.created_at))}</td>
      <td class="actions"><div class="row-actions">${acts}</div></td>
    </tr>`;
  }).join('');
}
async function adminVerifyPayment(id) {
  const res = await dialog({ title: 'Approve this payment?', message: 'The order is marked paid and the customer is told on WhatsApp that it’s confirmed.', confirmText: 'Approve payment' });
  if (!res) return;
  try { const d = await api('/admin/payment-verifications/' + id + '/approve', 'PATCH'); toast('Payment approved' + (d.customer_notified ? ' · customer notified' : ' · customer outside 24h window')); loadAll(); loadReceipts(); }
  catch (e) { toast(e.message, 'error'); }
}
async function adminRequestResend(id) {
  const res = await dialog({ title: 'Ask for a clearer receipt?', message: 'The customer gets a WhatsApp message asking them to resend the payment screenshot.', confirmText: 'Send request' });
  if (!res) return;
  try { const d = await api('/admin/payment-verifications/' + id + '/request-resend', 'PATCH'); toast(d.customer_notified ? 'Request sent to customer' : 'Marked — customer is outside the 24h window', d.customer_notified ? 'success' : 'info'); loadAll(); loadReceipts(); }
  catch (e) { toast(e.message, 'error'); }
}

async function loadRefunds() {
  $('refunds-body').innerHTML = loadingRow(7);
  try { renderRefunds(await safeFetch('/admin/refund-requests')); }
  catch (e) { $('refunds-body').innerHTML = emptyRow(7, 'Could not load refunds', e.message, 'alert'); }
}
function renderRefunds(data) {
  const list = data.refunds || [];
  pendingRefunds = list.filter((r) => r.status === 'pending').length;
  setCount('refund-badge', pendingRefunds);
  $('refunds-count').textContent = list.length + ' total';
  if (!list.length) { $('refunds-body').innerHTML = emptyRow(7, 'No refund requests', '', 'refund'); return; }
  const tone = { pending: ['warning', 'Pending'], approved: ['success', 'Approved'], rejected: ['danger', 'Rejected'], resolved: ['', 'Resolved'] };
  $('refunds-body').innerHTML = list.map((r) => {
    const [t, l] = tone[r.status] || ['', r.status];
    const acts = r.status === 'pending' ? `<button class="btn xs primary" onclick="approveRefund(${r.id}, ${jsArg(r.order_ref || '')})">Approve</button><button class="btn xs danger-ghost" onclick="rejectRefund(${r.id}, ${jsArg(r.order_ref || '')})">Reject</button>` : '';
    return `<tr>
      <td class="primary"><div class="cell-main">${esc(r.customer_name || 'Unknown')}</div><div class="cell-sub mono">${esc(r.customer_wa_id || '')}</div></td>
      <td data-label="Order" class="ref">${esc(r.order_ref || '—')}</td>
      <td data-label="Amount" class="right num cell-main">${r.amount != null ? esc(fmt_currency(r.amount)) : '—'}</td>
      <td data-label="Reason" class="cell-wrap">${esc(r.reason || '—')}</td>
      <td data-label="Status">${badge(l, t)}</td>
      <td data-label="Date" class="muted" style="white-space:nowrap">${esc(fmt_date(r.created_at))}</td>
      <td class="actions"><div class="row-actions">${acts}</div></td>
    </tr>`;
  }).join('');
}
async function approveRefund(id, orderRef) {
  const res = await dialog({ title: `Approve refund${orderRef ? ' for ' + orderRef : ''}?`, message: 'The customer is told the payment will be reversed within 24 hours.', confirmText: 'Approve refund' });
  if (!res) return;
  try { const d = await api('/admin/refund-requests/' + id + '/approve', 'PATCH'); toast('Refund approved' + (d.customer_notified ? ' · customer notified' : '')); loadRefunds(); }
  catch (e) { toast(e.message, 'error'); }
}
async function rejectRefund(id, orderRef) {
  const res = await dialog({ title: `Reject refund${orderRef ? ' for ' + orderRef : ''}?`, confirmText: 'Reject', danger: true, input: { label: 'Reason for the customer (optional)', placeholder: 'e.g. Item was used' } });
  if (!res) return;
  try { const d = await api('/admin/refund-requests/' + id + '/reject', 'PATCH', { reason: res.value.trim() || null }); toast('Refund rejected' + (d.customer_notified ? ' · customer notified' : '')); loadRefunds(); }
  catch (e) { toast(e.message, 'error'); }
}

// ---------------------------------------------------------------------------
// WhatsApp connection
// ---------------------------------------------------------------------------
let waPollTimer = null, currentQrBlobUrl = null;

function setWaBadge(status, detail) {
  waStatus = status;
  const map = {
    ready: ['Connected', 'success'], qr_pending: ['Waiting for scan', 'warning'], disconnected: ['Reconnecting…', 'warning'],
    logged_out: ['Logged out', 'danger'], not_connected: ['Not connected', ''], starting: ['Starting…', 'info'],
    error: [detail ? 'Unavailable: ' + detail : 'Status unavailable', 'danger'],
  };
  const [text, tone] = map[status] || [status || 'Unknown', ''];
  const b = $('wa-status-badge');
  b.className = 'badge ' + tone;
  b.textContent = text;
  const connect = $('wa-connect-btn'), disconnect = $('wa-disconnect-btn');
  if (status === 'ready') { connect.classList.add('hidden'); disconnect.classList.remove('hidden'); $('wa-qr-wrap').classList.add('hidden'); stopWaPolling(); }
  else if (status === 'qr_pending') { connect.classList.remove('hidden'); connect.innerHTML = icon('refresh') + 'New QR code'; disconnect.classList.remove('hidden'); }
  else {
    connect.classList.remove('hidden'); connect.innerHTML = icon('phone') + 'Connect WhatsApp';
    disconnect.classList.toggle('hidden', !(status === 'disconnected' || status === 'starting'));
    $('wa-qr-wrap').classList.add('hidden');
  }
}
function startWaPolling() {
  if (waPollTimer) return;
  waPollTimer = setInterval(async () => {
    try {
      const r = await adminFetch('/admin/whatsapp/status');
      if (!r.ok) return;
      const st = (await r.json()).status || 'not_connected';
      setWaBadge(st);
      if (st === 'qr_pending') loadWaQr();
      else if (st === 'ready') toast('WhatsApp connected — your agent is live');
    } catch (e) { }
  }, 3000);
}
function stopWaPolling() { if (waPollTimer) { clearInterval(waPollTimer); waPollTimer = null; } }
async function refreshWaStatus() {
  try {
    const r = await adminFetch('/admin/whatsapp/status');
    if (!r.ok) { const e = await r.json().catch(() => ({})); setWaBadge('error', e.detail || 'HTTP ' + r.status); return 'error'; }
    const st = (await r.json()).status || 'not_connected';
    setWaBadge(st);
    if (st === 'qr_pending') { loadWaQr(); startWaPolling(); }
    return st;
  } catch (e) { setWaBadge('error', e.message); return 'error'; }
}
async function loadWaQr() {
  try {
    const r = await adminFetch('/admin/whatsapp/qr');
    if (r.status === 204 || !r.ok) return;
    const blob = await r.blob();
    if (currentQrBlobUrl) URL.revokeObjectURL(currentQrBlobUrl);
    currentQrBlobUrl = URL.createObjectURL(blob);
    $('wa-qr-img').src = currentQrBlobUrl;
    $('wa-qr-wrap').classList.remove('hidden');
  } catch (e) { }
}
async function connectWhatsapp() {
  const btn = $('wa-connect-btn');
  const force = waStatus === 'qr_pending';
  btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span> ' + (force ? 'Refreshing…' : 'Connecting…');
  try {
    await api('/admin/whatsapp/connect' + (force ? '?force=true' : ''), 'POST');
    startWaPolling();
    if (await refreshWaStatus() === 'qr_pending') loadWaQr();
  } catch (e) {
    toast('Could not reach the WhatsApp bridge: ' + e.message, 'error', 8000);
    setWaBadge('error', e.message);
  }
  btn.disabled = false;
}
async function disconnectWhatsapp() {
  const res = await dialog({ title: 'Disconnect WhatsApp?', message: 'The agent stops receiving and replying to messages. Reconnecting needs a new QR scan.', confirmText: 'Disconnect', danger: true });
  if (!res) return;
  try { await api('/admin/whatsapp/disconnect', 'POST'); toast('WhatsApp disconnected'); } catch (e) { toast(e.message, 'error'); }
  stopWaPolling();
  refreshWaStatus();
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
const URDU_DESCRIPTIONS = {
  auto: 'Replies in the customer’s own language — Urdu script, Roman Urdu or English.',
  roman_urdu: 'Replies mostly in everyday Roman Urdu (Latin letters).',
  urdu_script: 'Replies mostly in polite standard Urdu script.',
};
function updateUrduUI(enabled, mode) {
  $('urdu-options-box').style.opacity = enabled ? '1' : '0.5';
  const label = mode === 'roman_urdu' ? 'Roman Urdu' : mode === 'urdu_script' ? 'Urdu script' : 'Auto';
  const b = $('urdu-badge');
  b.textContent = enabled ? 'Urdu · ' + label : 'English only';
  b.className = 'badge plain ' + (enabled ? 'brand' : '');
  $('urdu-mode-description').textContent = URDU_DESCRIPTIONS[mode] || URDU_DESCRIPTIONS.auto;
}

async function loadSettings() {
  const keys = ['business_name', 'business_description', 'delivery_charge', 'delivery_estimate_days', 'bank_transfer_details', 'urdu_enabled', 'agent_language', 'agent_active', 'agent_mode', 'business_knowledge', 'services_offered', 'working_hours', 'meeting_types', 'custom_instructions', 'order_channel', 'website_url', 'shop_contact'];
  try {
    const results = await Promise.all(keys.map((k) => safeFetch('/admin/settings/' + k)));
    const m = {};
    keys.forEach((k, i) => { m[k] = results[i].value || ''; });
    $('setting-business-name').value = m.business_name;
    $('setting-business-description').value = m.business_description;
    $('setting-delivery-charge').value = m.delivery_charge || '0';
    $('setting-delivery-estimate').value = m.delivery_estimate_days;
    $('setting-bank-details').value = m.bank_transfer_details;
    updateAgentActiveUI(m.agent_active !== 'false');
    const urdu = m.urdu_enabled !== 'false';
    $('setting-urdu-toggle').checked = urdu;
    $('setting-agent-language').value = m.agent_language || 'auto';
    updateUrduUI(urdu, m.agent_language || 'auto');
    $('setting-agent-mode').value = m.agent_mode || 'booking_closer';
    $('setting-business-knowledge').value = m.business_knowledge;
    $('setting-services-offered').value = m.services_offered;
    $('setting-working-hours').value = m.working_hours;
    $('setting-meeting-types').value = m.meeting_types;
    $('setting-custom-instructions').value = m.custom_instructions;
    $('setting-website-url').value = m.website_url;
    $('setting-shop-contact').value = m.shop_contact;
    updateOrderChannelUI(m.order_channel === 'website_link');
  } catch (e) { toast('Could not load settings: ' + e.message, 'error'); }
}

let currentAgentActiveState = true;
function updateAgentActiveUI(active) {
  currentAgentActiveState = !!active;
  $('setting-agent-active-toggle').checked = currentAgentActiveState;
  const b = $('agent-active-badge');
  b.className = 'badge ' + (active ? 'success' : 'danger');
  b.textContent = active ? 'Agent is replying' : 'Agent is paused — manual mode';
  const pill = $('topbar-agent-pill');
  pill.className = 'pill-toggle ' + (active ? 'on' : 'off');
  $('topbar-agent-text').textContent = active ? 'Agent on' : 'Agent paused';
  pill.title = active ? 'AI agent is replying — click to pause' : 'AI agent is paused — click to resume';
}
async function toggleAgentActive(active) {
  updateAgentActiveUI(active);
  try {
    await api('/admin/settings/agent_active', 'PUT', { value: active ? 'true' : 'false' });
    toast(active ? 'Agent is replying again' : 'Agent paused — you’re in manual mode', active ? 'success' : 'info');
  } catch (e) { updateAgentActiveUI(!active); toast(e.message, 'error'); }
}
function toggleAgentActiveFromTopbar() { toggleAgentActive(!currentAgentActiveState); }

async function toggleUrduSupport(enabled) {
  try {
    await api('/admin/settings/urdu_enabled', 'PUT', { value: enabled ? 'true' : 'false' });
    updateUrduUI(enabled, $('setting-agent-language').value);
    toast(enabled ? 'Urdu replies enabled' : 'English only');
  } catch (e) { $('setting-urdu-toggle').checked = !enabled; toast(e.message, 'error'); }
}
async function saveLanguageMode(mode) {
  try { await api('/admin/settings/agent_language', 'PUT', { value: mode }); updateUrduUI($('setting-urdu-toggle').checked, mode); toast('Reply style saved'); }
  catch (e) { toast(e.message, 'error'); }
}
async function saveSetting(key, inputId, statusId) {
  const st = $(statusId);
  st.className = 'save-status'; st.textContent = 'Saving…';
  try {
    await api('/admin/settings/' + key, 'PUT', { value: $(inputId).value });
    st.textContent = 'Saved';
    setTimeout(() => { if (st.textContent === 'Saved') st.textContent = ''; }, 2500);
    if (key === 'business_name') updateTopbarUser($(inputId).value.trim());
  } catch (e) { st.className = 'save-status err'; st.textContent = e.message; }
}
function saveDeliveryCharge() { return saveSetting('delivery_charge', 'setting-delivery-charge', 'delivery-charge-status'); }
function saveBankDetails() { return saveSetting('bank_transfer_details', 'setting-bank-details', 'bank-details-status'); }

function updateOrderChannelUI(website) {
  $('setting-order-channel-toggle').checked = !!website;
  $('order-channel-note').classList.toggle('hidden', !website);
  const b = $('order-channel-badge');
  b.textContent = website ? 'On the website' : 'In WhatsApp';
  b.className = 'badge plain ' + (website ? 'brand' : '');
}
async function toggleOrderChannel(website) {
  if (website && !$('setting-website-url').value.trim()) {
    toast('Add your website address first, then switch this on.', 'info');
    updateOrderChannelUI(false);
    $('setting-website-url').focus();
    return;
  }
  updateOrderChannelUI(website);
  try {
    await api('/admin/settings/order_channel', 'PUT', { value: website ? 'website_link' : 'whatsapp' });
    toast(website ? 'Customers now order on your website' : 'Orders are taken in WhatsApp again');
  } catch (e) { updateOrderChannelUI(!website); toast(e.message, 'error'); }
}

// ---------------------------------------------------------------------------
// Broadcasts
// ---------------------------------------------------------------------------
let currentOutboundMode = 'ai_prompt';
function setOutboundMode(mode) {
  currentOutboundMode = mode;
  $('ob-mode-ai-btn').classList.toggle('active', mode === 'ai_prompt');
  $('ob-mode-tpl-btn').classList.toggle('active', mode === 'template');
  $('ob-section-ai').classList.toggle('hidden', mode !== 'ai_prompt');
  $('ob-section-tpl').classList.toggle('hidden', mode !== 'template');
}
const PRESETS = {
  consultation: 'Invite the customer to book a free 15-minute consultation this week. Keep it warm, polite and short.',
  offer: 'Announce a limited-time 20% discount for returning customers this month.',
  followup: "Politely follow up to see if they have any questions or would like to place an order.",
  urdu: 'Roman Urdu mein polite message bhejo: Salam! Follow up kar rahe hain — kya aap ko kisi product ki details ya order mein madad chahiye?',
};
function setPresetPrompt(type) { setOutboundMode('ai_prompt'); $('ob-ai-prompt').value = PRESETS[type] || ''; }
function insertTag(tag) {
  const el = $('ob-template-text');
  const s = el.selectionStart || 0, e = el.selectionEnd || 0;
  el.value = el.value.slice(0, s) + tag + el.value.slice(e);
  el.focus(); el.selectionStart = el.selectionEnd = s + tag.length;
}
function updateRecipientCount() {
  const seen = new Set();
  ($('ob-recipients').value || '').split(/[\r\n;]+/).forEach((line) => {
    const digits = (line.split(/[,:]/)[0] || '').replace(/\D/g, '');
    if (digits.length >= 7) seen.add(digits);
  });
  const n = seen.size;
  $('ob-recipient-count-badge').textContent = n + ' recipient' + (n === 1 ? '' : 's');
  $('ob-send-btn-text').textContent = n ? `Send to ${n} recipient${n === 1 ? '' : 's'}` : 'Send broadcast';
  return n;
}
function obError(msg) { const e = $('ob-form-error'); e.textContent = msg || ''; e.classList.toggle('show', !!msg); }

async function previewOutbound() {
  obError('');
  const text = (currentOutboundMode === 'ai_prompt' ? $('ob-ai-prompt') : $('ob-template-text')).value.trim();
  if (!text) return obError(currentOutboundMode === 'ai_prompt' ? 'Write a prompt first.' : 'Write the message first.');
  const btn = $('ob-preview-btn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner sm"></span> Generating…';
  try {
    const d = await api('/admin/outbound/preview', 'POST', { mode: currentOutboundMode, prompt_or_template: text, sample_name: 'Ali Khan' });
    $('ob-preview-bubble-text').textContent = d.preview || '';
    $('ob-preview-time').textContent = new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  } catch (e) { obError(e.message); }
  btn.disabled = false; btn.innerHTML = icon('eye') + 'Preview';
}

async function sendOutboundBroadcast() {
  obError('');
  const recipients = $('ob-recipients').value.trim();
  const prompt = $('ob-ai-prompt').value.trim(), template = $('ob-template-text').value.trim();
  if (!recipients) return obError('Add at least one phone number.');
  if (currentOutboundMode === 'ai_prompt' && !prompt) return obError('Write a prompt for the AI.');
  if (currentOutboundMode === 'template' && !template) return obError('Write the message to send.');
  const n = updateRecipientCount();
  const res = await dialog({ title: `Send to ${n} recipient${n === 1 ? '' : 's'}?`, message: 'Messages go out on WhatsApp now. Your agent will handle replies with full context.', confirmText: 'Send now' });
  if (!res) return;
  const btn = $('ob-send-btn');
  btn.disabled = true; $('ob-send-btn-text').textContent = 'Sending…';
  try {
    const d = await api('/admin/outbound/send', 'POST', { recipients, mode: currentOutboundMode, message_text: template, ai_prompt: prompt, campaign_name: $('ob-campaign-name').value.trim() || null });
    $('ob-results-section').classList.remove('hidden');
    $('ob-res-total').textContent = d.total || 0; $('ob-res-sent').textContent = d.sent || 0; $('ob-res-failed').textContent = d.failed || 0;
    $('ob-results-body').innerHTML = (d.results || []).map((r) => `<tr>
      <td class="primary mono">${esc(r.phone)}</td><td data-label="Name">${esc(r.name || '—')}</td>
      <td data-label="Status">${r.status === 'sent' ? badge('Sent', 'success') : badge('Failed', 'danger')}</td>
      <td data-label="Details" class="cell-wrap">${esc((r.message || r.error || '').slice(0, 160))}</td></tr>`).join('');
    toast(`Broadcast sent · ${d.sent} delivered, ${d.failed} failed`, d.failed ? 'info' : 'success');
    loadCampaigns();
  } catch (e) { obError(e.message); toast(e.message, 'error'); }
  btn.disabled = false; updateRecipientCount();
}

async function loadCampaigns() {
  try {
    const list = (await safeFetch('/admin/outbound/campaigns')).campaigns || [];
    $('outbound-campaigns-count').textContent = list.length + ' campaign' + (list.length === 1 ? '' : 's');
    if (!list.length) { $('ob-campaigns-body').innerHTML = emptyRow(7, 'No broadcasts yet', '', 'megaphone'); return; }
    const tone = { completed: ['success', 'Completed'], partially_failed: ['warning', 'Partial'], failed: ['danger', 'Failed'] };
    $('ob-campaigns-body').innerHTML = list.map((c) => {
      const [t, l] = tone[c.status] || ['', c.status];
      return `<tr>
        <td class="primary"><div class="cell-main">${esc(c.name || 'Broadcast')}</div><div class="cell-sub">${c.total_recipients} recipients</div></td>
        <td data-label="Mode">${badge(c.mode === 'ai_prompt' ? 'AI' : 'Template', 'violet', true)}</td>
        <td data-label="Message" class="cell-wrap" title="${esc(c.template_or_prompt)}">${esc((c.template_or_prompt || '').slice(0, 90))}${(c.template_or_prompt || '').length > 90 ? '…' : ''}</td>
        <td data-label="Sent" class="right num">${c.sent_count}</td>
        <td data-label="Failed" class="right num">${c.failed_count}</td>
        <td data-label="Status">${badge(l, t)}</td>
        <td data-label="Date" class="muted" style="white-space:nowrap">${esc(fmt_date(c.created_at))}</td></tr>`;
    }).join('');
  } catch (e) { $('ob-campaigns-body').innerHTML = emptyRow(7, 'Could not load broadcasts', e.message, 'alert'); }
}

// ---------------------------------------------------------------------------
// Demo video
// ---------------------------------------------------------------------------
function openDemoModal() { openModal('demo-modal'); $('demo-video').play().catch(() => { }); }
function closeDemoModal() { const v = $('demo-video'); v.pause(); v.currentTime = 0; closeModal('demo-modal'); }

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
window.addEventListener('error', (e) => { if (e.message) console.error(e); });

(function boot() {
  const h = location.hash;
  if (h.startsWith('#imp=')) {
    const parts = h.slice(5).split('|');
    localStorage.setItem('adminApiKey', decodeURIComponent(parts[0]));
    localStorage.setItem('_impersonating', parts.length > 1 ? decodeURIComponent(parts[1]) : 'Tenant');
    history.replaceState(null, '', '/admin');
  }
  syncThemeIcons();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncThemeIcons);
  updateTopbarUser();
  const start = location.hash.slice(1);
  showTab(PAGES[PAGE_ALIASES[start] || start] ? start : 'overview');
  $('orders-body').innerHTML = loadingRow(8);
  $('customers-body').innerHTML = loadingRow(6);
  if (!getAdminKey()) { showAuth(); }
  else {
    const imp = localStorage.getItem('_impersonating');
    if (imp) { $('imp-bar').classList.remove('hidden'); $('imp-bar-name').textContent = imp; }
    loadAll();
  }
  setInterval(() => { if (!document.hidden) loadAll(); }, 30000);
})();
