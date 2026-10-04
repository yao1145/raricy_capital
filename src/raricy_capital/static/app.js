"use strict";

// Operator console for the two funds. No external dependencies, no CDN, no
// credential persistence: the control token is sent once to /api/login and the
// browser only keeps the short-lived HttpOnly session cookie.

const MONEY_SCALE = 10000;      // 1e-4 fish-credit units
const SHARE_SCALE = 100000000;  // 1e-8 share atoms
const POLL_MS = 5000;

const el = (id) => document.getElementById(id);
const state = { authenticated: false, funds: [], filter: "", lastStatus: null };

//: Bumped whenever the operator signs in or out. An async load that captured an
//: older epoch must not paint results or re-open the console after a logout.
let sessionEpoch = 0;

//: Coalesces refreshes: never run two overlapping refresh cycles, but re-run once
//: when an action asked for fresher data while a cycle was already in flight.
let refreshPromise = null;
let refreshQueued = false;
let refreshQueuedFull = false;
let pollTimer = null;

const DEFAULT_NOTE = "管理会话已登录 · 数据每 5 秒刷新";

// ------------------------------------------------------------- formatting

function formatScaled(value, scale) {
  if (value === null || value === undefined || value === "") return "—";
  let big;
  try {
    big = BigInt(String(value).trim().split(".")[0]);
  } catch (err) {
    return "—";
  }
  const negative = big < 0n;
  if (negative) big = -big;
  const base = BigInt(scale);
  const whole = big / base;
  const frac = big % base;
  const digits = String(scale).length - 1;
  const fracText = frac.toString().padStart(digits, "0");
  return (negative ? "-" : "") + whole.toString() + "." + fracText;
}

const formatMoney = (units) => formatScaled(units, MONEY_SCALE);
const formatShares = (atoms) => formatScaled(atoms, SHARE_SCALE);

function formatNav(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  return /^-?\d+(\.\d+)?$/.test(text) ? text : "—";
}

function formatTime(ms) {
  if (!ms && ms !== 0) return "—";
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return "—";
  try {
    return new Date(n).toLocaleString("zh-CN", { hour12: false });
  } catch (err) {
    return String(ms);
  }
}

//: Reads the first present, non-empty field. Receipt, candidate and history
//: payloads use the documented names; the short alias lists only absorb older
//: rows written before the upstream transfer link was stored.
function pick(source, ...keys) {
  if (!source || typeof source !== "object") return undefined;
  for (const key of keys) {
    const value = source[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function textOf(value, fallback) {
  if (value === undefined || value === null || value === "") {
    return fallback === undefined ? "—" : fallback;
  }
  return String(value);
}

function stateBadge(fund) {
  // The operational state comes from the trader phase in the root status snapshot
  // (`fund.trader.phase`): halted / stopped / daily_pause / blocked / holding / flat.
  // The ledger `state` is only the fund's accounting lifecycle, so it is the fallback.
  const trader = fund.trader && typeof fund.trader === "object" ? fund.trader : null;
  if (trader) {
    const phase = String(trader.phase || "").toLowerCase();
    if (phase === "halted") return ["bad", "永久停机"];
    if (phase === "stopped") return ["warn", fund.logged_in && Number(fund.shares_atoms) > 0 ? "已暂停" : "未启用"];
    if (phase === "daily_pause" || phase.includes("pause")) return ["warn", "日内暂停"];
    if (phase === "blocked" || trader.blocked) return ["warn", "受限"];
    if (phase === "holding") return ["ok", "持仓中"];
    if (phase === "flat") return ["ok", "空仓待机"];
    if (trader.mark_failed) return ["warn", "标记失败"];
  }
  const raw = String(fund.state || fund.status_text || (fund.running ? "running" : "") || "").toLowerCase();
  if (raw.includes("perm") || raw.includes("halt") || raw.includes("停机")) return ["bad", "永久停机"];
  if (fund.running === true || raw === "running" || raw.includes("run") || raw.includes("运行")) return ["ok", "运行中"];
  if (fund.running === false || raw.includes("stop") || raw.includes("pause") || raw.includes("暂停")) return ["warn", "已暂停"];
  return ["unknown", "状态未知"];
}

//: Per-fund account connection state reported by the service
//: (`fund.account.state`): logged_in / relogin_required / retrying / no_credentials.
function accountStatusLine(fund) {
  const account = fund.account && typeof fund.account === "object" ? fund.account : null;
  const code = account && account.error ? String(account.error) : "";
  if (account) {
    const accountState = String(account.state || "").toLowerCase();
    if (accountState === "logged_in") return ["ok", "站点账户：已登录"];
    if (accountState === "relogin_required") {
      return ["bad", `站点账户：需人工重新登录${code ? `（${code}）` : ""}`];
    }
    if (accountState === "retrying") {
      const bits = ["站点账户：连接失败，等待自动重试"];
      if (code) bits.push(`原因 ${code}`);
      const attempts = Number(account.attempts);
      if (Number.isFinite(attempts) && attempts > 0) bits.push(`已尝试 ${attempts} 次`);
      const waitMs = Number(account.retry_in_ms);
      if (Number.isFinite(waitMs) && waitMs > 0) bits.push(`约 ${Math.ceil(waitMs / 1000)} 秒后重试`);
      else if (account.retry_in_ms === null) bits.push("已停止自动重试");
      return ["warn", bits.join(" · ")];
    }
    if (accountState === "no_credentials") return ["warn", "站点账户：未配置凭据，等待人工登录"];
    if (accountState) return ["unknown", `站点账户：未知状态（${accountState}）`];
  }
  if (fund.logged_in === true) return ["ok", "站点账户：已登录"];
  if (fund.logged_in === false) return ["warn", "站点账户：未登录"];
  return ["unknown", "站点账户：状态未知"];
}

//: Settlement view from `fund.settlement` ({hold, period, reason, pending[]}).
function settlementStatusLine(fund) {
  const settlement = fund.settlement && typeof fund.settlement === "object" ? fund.settlement : null;
  if (!settlement) return ["unknown", "结算：状态未知"];
  const pending = Array.isArray(settlement.pending)
    ? settlement.pending.filter((row) => row && row.period)
    : [];
  const pendingText = pending
    .map((row) => `${row.period}（${row.status || "未结算"}）`)
    .join("、");
  if (settlement.hold) {
    const bits = [settlement.period ? `${settlement.period} 估值未完成` : "估值未完成"];
    if (settlement.reason) bits.push(`原因 ${settlement.reason}`);
    if (pendingText) bits.push(`待处理 ${pendingText}`);
    return ["warn", `结算暂停：${bits.join(" · ")}`];
  }
  if (pendingText) return ["warn", `待处理结算：${pendingText}`];
  return ["ok", "结算：无待处理期间"];
}

function setStatusLine(node, [tone, text]) {
  if (!node) return;
  node.dataset.tone = tone;
  if (node.textContent !== text) node.textContent = text;
}

// --------------------------------------------------------------- api layer

async function api(path, options = {}) {
  const requestEpoch = sessionEpoch;
  const opts = {
    method: options.method || "GET",
    credentials: "same-origin",
    headers: {},
  };
  if (options.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, opts);
  let data = null;
  const text = await response.text();
  if (text) {
    try { data = JSON.parse(text); } catch (err) { data = null; }
  }
  if (response.status === 401) {
    if (requestEpoch === sessionEpoch) showLogin();
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const message = (data && (data.message || data.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return data || {};
}

// ---------------------------------------------------------------- login ui

function setHidden(id, hidden) {
  const node = el(id);
  if (node) node.hidden = hidden;
}

function clearConsoleData() {
  const funds = el("funds");
  if (funds) funds.replaceChildren();
  for (const id of ["events-list", "orders-list"]) {
    const list = el(id);
    if (list) list.replaceChildren();
  }
  const banner = el("status-banner");
  if (banner) {
    banner.replaceChildren();
    banner.classList.add("empty");
  }
  state.funds = [];
  state.lastStatus = null;
  updateFilterOptions([]);
  resetUnclaimedPanel();
}

function showLogin() {
  sessionEpoch += 1;
  state.authenticated = false;
  refreshQueued = false;
  refreshQueuedFull = false;
  stopPolling();
  clearConsoleData();
  setHidden("login-panel", false);
  setHidden("console", true);
  setHidden("logout-btn", true);
  setHidden("refresh-btn", true);
  setHidden("backup-btn", true);
  setHidden("status-pills", true);
}

function showConsole() {
  state.authenticated = true;
  setHidden("login-panel", true);
  setHidden("console", false);
  setHidden("logout-btn", false);
  setHidden("refresh-btn", false);
  setHidden("backup-btn", false);
  setHidden("status-pills", false);
  startPolling();
}

function startPolling() {
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(() => {
    if (!state.authenticated || document.hidden) return;
    refreshAll();
  }, POLL_MS);
}

function stopPolling() {
  if (pollTimer === null) return;
  window.clearInterval(pollTimer);
  pollTimer = null;
}

function flashNote(text, tone) {
  const note = el("session-note");
  if (!note) return;
  note.dataset.tone = tone || "info";
  note.textContent = text;
  window.setTimeout(() => {
    if (note.textContent === text) {
      note.textContent = DEFAULT_NOTE;
      delete note.dataset.tone;
    }
  }, 6000);
}

async function attemptLogin(token) {
  await api("/api/login", { method: "POST", body: { token } });
  sessionEpoch += 1;  // a fresh authenticated generation; stale loads are dropped
  const epoch = sessionEpoch;
  // Load status directly instead of through refreshAll(): the sign-in path must
  // not depend on coalescing with a background cycle that started before login.
  const status = await api("/api/status");
  if (epoch !== sessionEpoch) return;  // a 401 already returned us to the login screen
  showConsole();
  applyStatus(status, { full: true });
  await loadPanels(epoch);
  if (!state.authenticated) throw new Error("unauthorized");
}

// -------------------------------------------------------------- rendering

function metricTone(value) {
  if (value === null || value === undefined || value === "") return null;
  let n;
  try { n = BigInt(String(value).trim().split(".")[0]); } catch (err) { return null; }
  if (n > 0n) return "pos";
  if (n < 0n) return "neg";
  return null;
}

function addMetric(list, label, value, tone) {
  const wrap = document.createElement("div");
  wrap.className = "metric";
  if (tone) wrap.dataset.tone = tone;
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  wrap.append(dt, dd);
  list.append(wrap);
}

//: Headline figures stay visible; the accounting/reconciliation tail is the
//: collapsed detail below. Every field the service publishes is still shown.
function renderMetrics(primary, secondary, fund) {
  if (primary) primary.replaceChildren();
  if (secondary) secondary.replaceChildren();
  addMetric(primary, "基金净资产", formatMoney(fund.equity_units), metricTone(fund.equity_units));
  addMetric(primary, "在外份额", formatShares(fund.shares_atoms));
  addMetric(primary, "可用现金", formatMoney(fund.available_cash_units));
  addMetric(primary, "已实现利润", formatMoney(fund.realized_profit_units), metricTone(fund.realized_profit_units));
  addMetric(secondary, "交易桶现金", formatMoney(fund.wallet_units));
  addMetric(secondary, "持仓价值", formatMoney(fund.position_value_units));
  addMetric(secondary, "费用余额", formatMoney(fund.fee_balance_units));
  addMetric(secondary, "待确认收款", formatMoney(fund.pending_receipts_units));
  addMetric(secondary, "应付款", formatMoney(fund.liabilities_units));
  addMetric(secondary, "未认领款", formatMoney(fund.unclaimed_units));
  addMetric(secondary, "资本流入", formatMoney(fund.capital_flows_units), metricTone(fund.capital_flows_units));
  if (fund.benchmark_nav !== undefined) addMetric(secondary, "基准净值", formatNav(fund.benchmark_nav));
  if (fund.user_shares_atoms !== undefined) addMetric(secondary, "我的份额", formatShares(fund.user_shares_atoms));
  if (fund.user_value_units !== undefined) addMetric(secondary, "我的价值", formatMoney(fund.user_value_units));
}

function emptyListItem(text) {
  const li = document.createElement("li");
  li.className = "empty";
  li.textContent = text;
  return li;
}

function renderHolders(container, holders, message) {
  container.replaceChildren();
  if (!Array.isArray(holders) || !holders.length) {
    container.append(emptyListItem(message || "暂无持有人记录"));
    return;
  }
  for (const holder of holders) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = holder.user_id || holder.id || "未命名";
    const value = document.createElement("span");
    value.textContent = formatShares(holder.shares_atoms !== undefined ? holder.shares_atoms : holder.shares);
    li.append(name, value);
    container.append(li);
  }
}

function feedback(card, message, tone) {
  const node = card.querySelector('[data-field="feedback"]');
  if (!node) return;
  node.hidden = false;
  node.dataset.tone = tone;
  node.textContent = message;
  if (tone === "ok") {
    window.setTimeout(() => { node.hidden = true; }, 4000);
  }
}

// ------------------------------------------------------------ nav chart

function parseHistory(history) {
  const points = [];
  if (!Array.isArray(history)) return points;
  for (const row of history) {
    if (!row || typeof row !== "object") continue;
    const value = Number(row.nav);
    if (!Number.isFinite(value)) continue;
    const rawTime = row.updated_ms !== undefined ? row.updated_ms
      : row.t_ms !== undefined ? row.t_ms
      : row.t !== undefined ? row.t : row.ms;
    const t = Number(rawTime);
    points.push({ t: Number.isFinite(t) ? t : points.length, nav: value });
  }
  return points;
}

function chartColors(canvas) {
  // Read the light-theme palette from CSS so the chart follows the card accent
  // (emerald for the first fund, amber for the second) without hard-coded hex.
  const fallback = {
    accent: "#0f9d7a",
    accentSoft: "rgba(15, 157, 122, 0.16)",
    grid: "#dfe5ec",
    label: "#5b6f83",
  };
  let styles = null;
  try { styles = window.getComputedStyle(canvas); } catch (err) { styles = null; }
  if (!styles) return fallback;
  const read = (name, fallbackValue) => {
    const value = styles.getPropertyValue(name).trim();
    return value || fallbackValue;
  };
  return {
    accent: read("--accent", fallback.accent),
    accentSoft: read("--accent-soft", fallback.accentSoft),
    grid: read("--line", fallback.grid),
    label: read("--muted", fallback.label),
  };
}

function drawNav(canvas, history, emptyText) {
  const points = parseHistory(history);
  canvas.__history = history;
  canvas.hidden = points.length === 0;
  const empty = canvas.parentElement.querySelector('[data-field="nav-empty"]');
  if (empty && emptyText) empty.textContent = emptyText;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(canvas.clientWidth || canvas.getBoundingClientRect().width || 320, 200);
  const cssHeight = 150;
  canvas.width = Math.floor(cssWidth * dpr);
  canvas.height = Math.floor(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  const colors = chartColors(canvas);

  if (points.length < 2) {
    if (empty) empty.hidden = false;
    if (points.length === 1) {
      ctx.fillStyle = colors.accent;
      ctx.beginPath();
      ctx.arc(cssWidth / 2, cssHeight / 2, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  if (empty) empty.hidden = true;

  const pad = { top: 12, right: 12, bottom: 20, left: 52 };
  const plotW = cssWidth - pad.left - pad.right;
  const plotH = cssHeight - pad.top - pad.bottom;
  let min = Infinity;
  let max = -Infinity;
  for (const point of points) {
    if (point.nav < min) min = point.nav;
    if (point.nav > max) max = point.nav;
  }
  if (min === max) { min -= 0.01; max += 0.01; }
  const span = max - min;
  const times = points.map((p) => p.t);
  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const tSpan = tMax - tMin || 1;

  const xOf = (p) => pad.left + ((p.t - tMin) / tSpan) * plotW;
  const yOf = (p) => pad.top + (1 - (p.nav - min) / span) * plotH;

  ctx.strokeStyle = colors.grid;
  ctx.lineWidth = 1;
  ctx.font = "10px Consolas, monospace";
  ctx.fillStyle = colors.label;
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + (plotH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.stroke();
    const label = (max - (span * i) / 4).toFixed(4);
    ctx.fillText(label, 4, y + 3);
  }

  const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  gradient.addColorStop(0, colors.accentSoft);
  gradient.addColorStop(1, "rgba(255, 255, 255, 0)");

  ctx.beginPath();
  points.forEach((p, i) => {
    const x = xOf(p);
    const y = yOf(p);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  const last = points[points.length - 1];
  ctx.lineTo(xOf(last), pad.top + plotH);
  ctx.lineTo(xOf(points[0]), pad.top + plotH);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  points.forEach((p, i) => {
    const x = xOf(p);
    const y = yOf(p);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = colors.accent;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(xOf(last), yOf(last), 3, 0, Math.PI * 2);
  ctx.fillStyle = colors.accent;
  ctx.fill();
}

// ----------------------------------------------------------- fund cards

function findCard(container, fundId) {
  for (const child of container.children) {
    if (child.classList && child.classList.contains("fund-card") && child.dataset.fundId === fundId) {
      return child;
    }
  }
  return null;
}

//: Updates a card in place. Only text nodes and the metric lists are rewritten,
//: so open <details>, typed input values and focus survive a 5-second poll.
function applyFund(card, fund, options = {}) {
  const full = options.full === true;
  card.dataset.fundId = fund.fund_id || "";
  card.dataset.accent = options.accent || "emerald";

  const setText = (field, text) => {
    const node = card.querySelector(`[data-field="${field}"]`);
    if (node && node.textContent !== text) node.textContent = text;
  };

  setText("label", fund.label || fund.fund_id || "未命名基金");
  setText("fund_id", fund.fund_id || "");
  setText("nav", formatNav(fund.nav));
  setText("updated_ms", formatTime(fund.updated_ms));

  const badge = card.querySelector('[data-field="state"]');
  const [tone, text] = stateBadge(fund);
  if (badge) {
    badge.dataset.state = tone;
    if (badge.textContent !== text) badge.textContent = text;
  }

  setStatusLine(card.querySelector('[data-field="account-status"]'), accountStatusLine(fund));
  setStatusLine(card.querySelector('[data-field="settlement-status"]'), settlementStatusLine(fund));

  renderMetrics(
    card.querySelector('[data-field="metrics"]'),
    card.querySelector('[data-field="metrics-secondary"]'),
    fund,
  );

  // The chart only needs a redraw when the snapshot actually moved.
  const stamp = `${fund.updated_ms || ""}:${fund.nav || ""}`;
  if (full || card.dataset.navStamp !== stamp) {
    card.dataset.navStamp = stamp;
    loadNavHistory(card, fund.fund_id, full);
  }
  if (full) loadHolders(card, fund.fund_id);
}

function normalizeFund(fund) {
  if (fund && typeof fund.status === "object" && fund.status !== null) {
    return Object.assign({}, fund, fund.status);
  }
  return fund || {};
}

function renderFunds(rawFunds, options = {}) {
  const container = el("funds");
  const template = el("fund-card-template");
  const list = Array.isArray(rawFunds)
    ? rawFunds.map(normalizeFund).filter((fund) => fund && typeof fund === "object")
    : [];

  if (!list.length) {
    container.replaceChildren();
    const card = document.createElement("article");
    card.className = "card fund-card";
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无基金状态数据";
    card.append(empty);
    container.append(card);
    return;
  }

  // Drop the "no data" placeholder, keep every live card.
  for (const child of Array.from(container.children)) {
    if (child.classList.contains("fund-card") && child.dataset.fundId === undefined) child.remove();
  }

  const seen = new Set();
  list.forEach((fund, index) => {
    const fundId = String(fund.fund_id || "");
    seen.add(fundId);
    let card = findCard(container, fundId);
    const created = card === null;
    if (created) {
      const fragment = template.content.cloneNode(true);
      card = fragment.querySelector(".fund-card");
      card.dataset.fundId = fundId;
      container.append(fragment);
      wireControls(card);
    }
    applyFund(card, fund, {
      full: created || options.full === true,
      accent: index === 0 ? "emerald" : "amber",
    });
  });

  for (const child of Array.from(container.children)) {
    if (child.classList.contains("fund-card") && child.dataset.fundId !== undefined
        && !seen.has(child.dataset.fundId)) {
      child.remove();
    }
  }
}

async function loadNavHistory(card, fundId, full) {
  const canvas = card.querySelector('[data-field="nav-chart"]');
  if (!canvas) return;
  if (!fundId) {
    drawNav(canvas, [], "暂无净值记录");
    return;
  }
  const epoch = sessionEpoch;
  try {
    const data = await api(`/api/funds/${encodeURIComponent(fundId)}/nav-history`);
    if (epoch !== sessionEpoch) return;
    drawNav(canvas, data.history || [], "暂无净值记录");
  } catch (err) {
    if (epoch !== sessionEpoch || (err && err.message === "unauthorized")) return;
    drawNav(canvas, [], `净值曲线加载失败：${err.message || err}`);
    if (full) flashNote(`净值曲线加载失败：${err.message || err}`, "bad");
  }
}

async function loadHolders(card, fundId) {
  const list = card.querySelector('[data-field="holders"]');
  if (!list) return;
  if (!fundId) {
    renderHolders(list, [], "暂无持有人记录");
    return;
  }
  const epoch = sessionEpoch;
  try {
    const data = await api(`/api/holders?fund_id=${encodeURIComponent(fundId)}`);
    if (epoch !== sessionEpoch) return;
    renderHolders(list, data.holders || [], "暂无持有人记录");
  } catch (err) {
    if (epoch !== sessionEpoch || (err && err.message === "unauthorized")) return;
    renderHolders(list, [], `持有人加载失败：${err.message || err}`);
  }
}

// --------------------------------------------------------------- controls

function wireControls(card) {
  const fundId = () => card.dataset.fundId;

  async function run(label, fn) {
    try {
      await fn();
      feedback(card, `${label}成功`, "ok");
      await refreshAll({ full: true });
    } catch (err) {
      if (err && err.message === "unauthorized") return;
      feedback(card, `${label}失败：${err.message || err}`, "bad");
    }
  }

  card.querySelector('[data-action="login"]').addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const username = form.username.value.trim();
    const password = form.password.value;
    run("站点登录", async () => {
      await api(`/api/funds/${encodeURIComponent(fundId())}/login`, {
        method: "POST",
        body: { username, password },
      });
      form.password.value = "";  // never keep the site password in the field
    });
  });

  card.querySelector('[data-action="seed"]').addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    run("登记出资", () => api(`/api/funds/${encodeURIComponent(fundId())}/seed`, {
      method: "POST",
      body: { user_id: form.user_id.value.trim(), principal: form.principal.value.trim() },
    }));
  });

  card.querySelector('[data-action="start"]').addEventListener("click", () =>
    run("启动交易", () => api(`/api/funds/${encodeURIComponent(fundId())}/running`, {
      method: "POST", body: { enabled: true },
    })));

  card.querySelector('[data-action="pause"]').addEventListener("click", () =>
    run("暂停交易", () => api(`/api/funds/${encodeURIComponent(fundId())}/running`, {
      method: "POST", body: { enabled: false },
    })));

  card.querySelector('[data-action="stop"]').addEventListener("click", () => {
    if (!window.confirm("确认永久停机？该操作不会因追加本金自动解除。")) return;
    run("永久停机", () => api(`/api/funds/${encodeURIComponent(fundId())}/stop`, {
      method: "POST", body: {},
    }));
  });

  card.querySelector('[data-action="settle"]').addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const body = { kind: "month" };
    const period = form.period.value.trim();
    if (period) body.period = period;
    run("月度结算", () => api(`/api/funds/${encodeURIComponent(fundId())}/settle`, { method: "POST", body }));
  });

  card.querySelector('[data-action="settle-emergency"]').addEventListener("click", () =>
    run("紧急结算", () => api(`/api/funds/${encodeURIComponent(fundId())}/settle`, {
      method: "POST", body: { kind: "emergency" },
    })));

  card.querySelector('[data-action="dividend"]').addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    run("分红选择", () => api("/api/dividend-choice", {
      method: "POST",
      body: {
        fund_id: fundId(),
        user_id: form.user_id.value.trim(),
        reinvest_fraction: form.fraction.value.trim(),
      },
    }));
  });
}

// ------------------------------------------------------------- panels

function renderEvents(events) {
  const list = el("events-list");
  list.replaceChildren();
  if (!Array.isArray(events) || !events.length) {
    list.append(emptyListItem("暂无事件"));
    return;
  }
  for (const event of events) {
    const li = document.createElement("li");
    if (event.level) li.dataset.level = event.level;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = formatTime(event.created_ms);
    const body = document.createElement("span");
    body.className = "log-detail";
    const eventName = document.createElement("span");
    eventName.className = "log-event";
    eventName.textContent = `${event.fund_id ? event.fund_id + " · " : ""}${event.event || "事件"}`;
    body.append(eventName);
    if (event.details && Object.keys(event.details).length) {
      const detail = document.createElement("span");
      detail.className = "log-detail";
      detail.textContent = " " + JSON.stringify(event.details);
      body.append(detail);
    }
    li.append(time, body);
    list.append(li);
  }
}

function renderOrders(orders) {
  const list = el("orders-list");
  list.replaceChildren();
  if (!Array.isArray(orders) || !orders.length) {
    list.append(emptyListItem("暂无订单"));
    return;
  }
  for (const order of orders) {
    const li = document.createElement("li");
    const left = document.createElement("span");
    left.textContent = `${order.fund_id || ""} · ${order.user_id || order.id || ""}`;
    const right = document.createElement("span");
    right.className = "log-detail";
    const amount = order.amount_units !== undefined ? formatMoney(order.amount_units) : "";
    right.textContent = `${amount} ${order.status || order.kind || ""}`.trim();
    li.append(left, right);
    list.append(li);
  }
}

function renderListMessage(listId, message) {
  const list = el(listId);
  if (!list) return;
  list.replaceChildren(emptyListItem(message));
}

function bannerEntries(status) {
  const entries = [];
  const network = status.network || {};
  const service = status.status || {};
  const lastTick = Number(status.last_tick_ms);
  // `live` gates external writes (trading and transfers); it is not a real-money
  // "实盘" flag, so it is labelled as enabled writes vs a read-only preview.
  if (status.live !== undefined) entries.push(["运行模式", status.live ? "交易与转账已启用" : "只读预览"]);
  if (network.state !== undefined) entries.push(["网络", ({unknown: "尚未检测", up: "已连接", down: "连接中断", degraded: "连接不稳定"})[network.state] || String(network.state)]);
  if (service.detail !== undefined) entries.push(["服务状态", String(service.detail)]);
  if (Number.isFinite(lastTick) && lastTick > 0) entries.push(["最近心跳", formatTime(lastTick)]);
  else entries.push(["最近心跳", "尚无记录"]);
  if (service.last_health_ms !== undefined) entries.push([network.state === "unknown" ? "最近状态更新" : "最近健康检查", formatTime(service.last_health_ms)]);
  if (status.last_error) entries.push(["最近错误", String(status.last_error)]);
  return entries;
}

function renderBanner(status) {
  const banner = el("status-banner");
  if (!banner) return;
  banner.replaceChildren();
  const entries = bannerEntries(status);
  if (!entries.length) {
    banner.classList.add("empty");
    return;
  }
  banner.classList.remove("empty");
  for (const [label, value] of entries) {
    const span = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = label + "：";
    span.append(strong, document.createTextNode(value));
    banner.append(span);
  }
}

//: Badge text is evidence-based: a healthy network is only claimed after the
//: service has actually completed a monitoring tick.
function updateBadges(status) {
  const liveBadge = el("live-badge");
  if (liveBadge) {
    if (status.live === true) { liveBadge.dataset.state = "warn"; liveBadge.textContent = "交易与转账已启用"; }
    else if (status.live === false) { liveBadge.dataset.state = "ok"; liveBadge.textContent = "只读预览"; }
    else { liveBadge.dataset.state = "unknown"; liveBadge.textContent = "模式未知"; }
  }

  // The unclaimed panel repeats the gate where it matters: with live=false the
  // operator can verify and preview, but the final resolve is disabled.
  const unclaimedBadge = el("unclaimed-mode");
  if (unclaimedBadge) {
    if (status.live === true) { unclaimedBadge.dataset.state = "warn"; unclaimedBadge.textContent = "处理已启用"; }
    else if (status.live === false) { unclaimedBadge.dataset.state = "ok"; unclaimedBadge.textContent = "只读预览 · 仅核对"; }
    else { unclaimedBadge.dataset.state = "unknown"; unclaimedBadge.textContent = "模式未知"; }
  }

  const netBadge = el("net-badge");
  if (!netBadge) return;
  const network = status.network || {};
  const lastTick = Number(status.last_tick_ms);
  const checked = Number.isFinite(lastTick) && lastTick > 0 && network.state !== undefined && network.state !== "unknown";
  if (!checked) { netBadge.dataset.state = "unknown"; netBadge.textContent = "网络未知"; }
  else if (network.ok === true) { netBadge.dataset.state = "ok"; netBadge.textContent = "网络正常"; }
  else if (network.ok === false) { netBadge.dataset.state = "bad"; netBadge.textContent = "网络异常"; }
  else { netBadge.dataset.state = "unknown"; netBadge.textContent = "网络未知"; }
}

function updateFilterOptions(funds) {
  const select = el("event-filter");
  if (!select) return;
  const wanted = funds.map((fund) => [fund.fund_id || "", fund.label || fund.fund_id || ""]);
  const current = Array.from(select.options).slice(1).map((option) => [option.value, option.textContent]);
  if (JSON.stringify(wanted) === JSON.stringify(current)) return;  // keep focus selection
  const previous = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部基金";
  select.append(all);
  for (const [value, label] of wanted) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = previous;
  if (select.value !== previous) select.value = "";
}

// ------------------------------------------------------- unclaimed review
//
// Receipts that could not be matched to a subscription wait here for a human
// decision: link the money to the one existing subscription it really belongs
// to, or refund it in full to the original payer. The console only uses the
// documented endpoints:
//   GET  /api/unclaimed?fund_id=&status=
//   GET  /api/funds/{fund_id}/unclaimed/{id}
//   POST /api/funds/{fund_id}/unclaimed/{id}/preview  {action, subscription_id?}
//   POST /api/funds/{fund_id}/unclaimed/{id}/resolve  {action, subscription_id?, version, reason}
// The server re-reads the authoritative upstream receipt and commits with an
// atomic version check, so this client holds no editing lease: "reviewing" is
// transient local state, and a version conflict means the record changed under
// us and has to be read and confirmed again. Actor identity is derived by the
// server from the session; the payload never carries it.

const UNCLAIMED_ACTIONS = { link: "关联已有申购单", refund: "原路全额退款" };

//: List rows are compared through a joined signature. The separators are control
//: characters built from their code points so the source stays plain text and a
//: row field can never produce the separator itself.
const UNCLAIMED_FIELD_SEP = String.fromCharCode(1);
const UNCLAIMED_ROW_SEP = String.fromCharCode(2);

const unclaimed = {
  fundId: "",
  status: "",
  rows: [],
  listSignature: null,
  selected: null,       // {fundId, id}
  detail: null,         // receipt detail from GET .../unclaimed/{id}
  preview: null,        // {preview, live} from POST .../preview
  subscriptionId: "",   // operator's candidate choice (never inferred)
  busy: false,
  note: "",
};

//: Operational wording matters here: nothing at this stage is a completed
//: payout, so a queued refund is never shown as paid, and an unknown payout
//: result is shown as "needs reconciliation" instead of success.
function unclaimedStatusInfo(record) {
  const raw = String(pick(record, "resolution_status", "status") || "").toLowerCase();
  if (raw === "unclaimed" || raw === "unresolved" || raw === "") return { tone: "warn", text: "待核对" };
  if (raw === "linked") return { tone: "ok", text: "已关联申购单" };
  if (raw === "refund_queued") {
    const waiting = unclaimedWaitingReason(record);
    return { tone: "warn", text: waiting ? `退款已排队 · ${waiting}` : "退款已排队 · 尚未付款" };
  }
  if (raw === "refund_unknown") return { tone: "bad", text: "退款结果待确认（未知，需先对账）" };
  if (raw === "refunded") return { tone: "ok", text: "已退款" };
  return { tone: "unknown", text: `状态未知（${raw}）` };
}

function unclaimedWaitingReason(record) {
  const payout = record && typeof record.payout === "object" && record.payout ? record.payout : {};
  const raw = pick(record, "waiting_reason", "hold_reason", "payout_reason")
    || pick(payout, "waiting_reason", "hold_reason", "reason", "status_reason");
  if (raw === undefined) return "";
  const text = String(raw);
  if (/cash|现金|insufficient|balance/i.test(text)) return `等待可用现金（${text}）`;
  return `等待付款（${text}）`;
}

function isUnresolvedRow(row) {
  const raw = String(pick(row, "resolution_status", "status") || "unclaimed").toLowerCase();
  return raw === "unclaimed" || raw === "unresolved";
}

function unclaimedAmountUnits(record) {
  return pick(record, "amount_units", "amount");
}

function unclaimedPayer(record) {
  return pick(record, "from_user_id", "payer_user_id", "payer_id");
}

function unclaimedArrival(record) {
  return pick(record, "occurred_ms", "arrived_ms", "occurred_at_ms");
}

function unclaimedNote(record) {
  const note = pick(record, "note_original", "note", "memo");
  if (note === undefined) return "（无附言）";
  const text = String(note);
  return text ? text : "（空附言）";
}

function unclaimedVersion(record) {
  const value = Number(pick(record, "version"));
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

function sumUnits(rows) {
  let total = 0n;
  for (const row of rows) {
    try {
      total += BigInt(String(unclaimedAmountUnits(row)).trim().split(".")[0]);
    } catch (err) { /* unparsable row amounts are simply not summed */ }
  }
  return total.toString();
}

//: Same in-place discipline as the fund cards: rows are rebuilt only when their
//: content really changed, so a 5-second poll keeps focus, hover and scroll.
function unclaimedListSignature() {
  const parts = unclaimed.rows.map((row) => [
    textOf(pick(row, "id", "unclaimed_id")),
    textOf(pick(row, "fund_id")),
    textOf(unclaimedAmountUnits(row)),
    textOf(unclaimedArrival(row)),
    textOf(unclaimedPayer(row)),
    textOf(pick(row, "note", "memo"), ""),
    unclaimedStatusInfo(row).text,
  ].join(UNCLAIMED_FIELD_SEP));
  parts.push(unclaimed.selected ? `${unclaimed.selected.fundId}/${unclaimed.selected.id}` : "");
  return parts.join(UNCLAIMED_ROW_SEP);
}

function renderUnclaimedSummary(data) {
  const summary = el("unclaimed-summary");
  if (!summary) return;
  const rows = unclaimed.rows;
  const parsed = Number(data && data.count);
  const count = Number.isFinite(parsed) ? parsed : rows.length;
  const totalUnits = data && data.total_units !== undefined ? formatMoney(data.total_units) : formatMoney(sumUnits(rows));
  const unresolved = rows.filter(isUnresolvedRow);

  const strong = (text) => {
    const node = document.createElement("strong");
    node.textContent = text;
    return node;
  };
  summary.replaceChildren();
  summary.append(
    document.createTextNode("筛选结果 "), strong(String(count)),
    document.createTextNode(" 笔 · 合计 "), strong(totalUnits),
    document.createTextNode(" 小鱼干 · 待核对 "), strong(String(unresolved.length)),
    document.createTextNode(" 笔 · "), strong(formatMoney(sumUnits(unresolved))),
    document.createTextNode(" 小鱼干"),
  );
  if (unclaimed.status === "linked" || unclaimed.status === "refund_queued") {
    summary.append(document.createTextNode("（待核对数量只统计当前筛选结果）"));
  }
  if (count !== rows.length) summary.append(document.createTextNode(`（已显示 ${rows.length} 笔）`));
}

function renderUnclaimedMessage(message) {
  const summary = el("unclaimed-summary");
  if (summary) summary.textContent = message;
}

function renderUnclaimedList() {
  const list = el("unclaimed-list");
  if (!list) return;
  const signature = unclaimedListSignature();
  if (signature === unclaimed.listSignature) return;  // nothing moved: keep the DOM
  unclaimed.listSignature = signature;
  list.replaceChildren();

  if (!unclaimed.rows.length) {
    list.append(emptyListItem("没有符合条件的未认领款"));
    return;
  }

  for (const row of unclaimed.rows) {
    const fundId = textOf(pick(row, "fund_id"), "");
    const id = textOf(pick(row, "id", "unclaimed_id"), "");
    const info = unclaimedStatusInfo(row);

    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "unclaimed-row";
    if (unclaimed.selected && unclaimed.selected.id === id && unclaimed.selected.fundId === fundId) {
      button.dataset.selected = "true";
      button.setAttribute("aria-current", "true");
    }

    const meta = document.createElement("span");
    meta.className = "row-meta";
    meta.textContent = `${formatTime(unclaimedArrival(row))} · ${fundId || "未知基金"}`;

    const amount = document.createElement("span");
    amount.className = "row-amount";
    amount.textContent = `${formatMoney(unclaimedAmountUnits(row))} 小鱼干`;

    const note = document.createElement("span");
    note.className = "row-note";
    note.textContent = `付款人 ${textOf(unclaimedPayer(row))} · ${unclaimedNote(row)}`;

    const badge = document.createElement("span");
    badge.className = "badge row-status";
    badge.dataset.state = info.tone;
    badge.textContent = info.text;

    button.append(meta, amount, note, badge);
    button.addEventListener("click", () => selectUnclaimed(fundId, id));
    li.append(button);
    list.append(li);
  }
}

function renderUnclaimedFacts(container, record) {
  if (!container) return;
  container.replaceChildren();
  if (!record) return;
  for (const [label, value] of unclaimedFacts(record)) {
    addMetric(container, label, value);
  }
}

function unclaimedFacts(record) {
  const facts = [
    ["流水 ID", textOf(pick(record, "id", "unclaimed_id"))],
    ["上游流水号", textOf(pick(record, "transfer_id"), "未记录")],
    ["交易记录行", textOf(pick(record, "transaction_row_id"), "未记录")],
    ["基金", textOf(pick(record, "fund_id"))],
    ["付款人 ID", textOf(unclaimedPayer(record))],
    ["金额（小鱼干，不可修改）", formatMoney(unclaimedAmountUnits(record))],
    ["权威到账时间", formatTime(unclaimedArrival(record))],
    ["登记时间", formatTime(pick(record, "created_ms", "created_at_ms"))],
    ["原始附言（保留原文）", unclaimedNote(record)],
    ["记录版本", String(unclaimedVersion(record))],
  ];
  const subscriptionId = pick(record, "subscription_id");
  if (subscriptionId !== undefined) facts.push(["已关联申购单", textOf(subscriptionId)]);
  const payoutId = pick(record, "payout_id", "refund_payout_id", "payment_id");
  if (payoutId !== undefined) facts.push(["退款付款单", textOf(payoutId)]);
  const resolutionReason = pick(record, "resolution_reason", "reason");
  if (resolutionReason !== undefined) facts.push(["处理理由", textOf(resolutionReason)]);
  const resolvedMs = pick(record, "resolved_ms", "resolved_at_ms");
  if (resolvedMs !== undefined) facts.push(["处理时间", formatTime(resolvedMs)]);
  return facts;
}

function candidateId(candidate) {
  return textOf(pick(candidate, "subscription_id", "id", "order_id"), "");
}

function candidateReasons(candidate) {
  const raw = pick(candidate, "reasons", "errors", "blocked_reasons", "mismatch_reasons");
  if (raw === undefined) return [];
  const list = Array.isArray(raw) ? raw : [raw];
  return list.map((item) => textOf(item, "")).filter((item) => item !== "");
}

//: Conservative by default: a candidate is selectable only when it says so
//: explicitly, or when nothing is said at all. Anything flagged or explained is
//: left for the refund / hold decision instead of being linked on a guess.
function candidateEligible(candidate) {
  const blocked = candidateReasons(candidate).length > 0;
  return candidate.eligible === true || (candidate.eligible === undefined && !blocked);
}

function renderUnclaimedCandidates() {
  const container = el("unclaimed-candidates");
  if (!container) return;
  container.replaceChildren();
  const candidates = unclaimed.detail && Array.isArray(unclaimed.detail.candidates)
    ? unclaimed.detail.candidates.filter((item) => item && typeof item === "object")
    : [];
  if (!candidates.length) {
    container.append(emptyListItem("没有候选申购单：请选择原路全额退款，或保持待核对等待更多证据"));
    return;
  }
  // A resolved receipt keeps its candidates visible as history, but nothing can
  // be picked any more.
  const locked = !unclaimed.detail || !isUnresolvedRow(unclaimed.detail);
  candidates.forEach((candidate, index) => {
    const id = candidateId(candidate);
    const eligible = candidateEligible(candidate) && id !== "";
    const reasons = candidateReasons(candidate);

    const li = document.createElement("li");
    li.dataset.eligible = eligible ? "true" : "false";

    const head = document.createElement("div");
    head.className = "candidate-head";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "unclaimed-candidate";
    radio.id = `unclaimed-candidate-${index}`;
    radio.value = id;
    radio.disabled = !eligible || locked;
    const label = document.createElement("label");
    label.htmlFor = radio.id;
    const tag = locked ? " · 已处理" : eligible ? " · 可关联" : " · 不可关联";
    label.textContent = `${id ? `申购单 ${id}` : "缺少申购单 ID"}${tag}`;
    head.append(radio, label);
    li.append(head);

    const meta = document.createElement("p");
    meta.className = "candidate-meta";
    const metaBits = [
      `申购人 ${textOf(pick(candidate, "user_id", "from_user_id"))}`,
      `订单金额 ${formatMoney(pick(candidate, "total_units", "amount_units", "paid_units", "amount"))} 小鱼干`,
      `订单状态 ${textOf(pick(candidate, "status", "state"))}`,
      `订单创建 ${formatTime(pick(candidate, "created_ms", "created_at_ms"))}`,
      `有效期至 ${formatTime(pick(candidate, "expires_ms", "expires_at_ms"))}`,
    ];
    meta.textContent = metaBits.join(" · ");
    li.append(meta);

    if (reasons.length) {
      const list = document.createElement("ul");
      list.className = "unclaimed-reasons";
      for (const reason of reasons) {
        const item = document.createElement("li");
        item.textContent = reason;
        list.append(item);
      }
      li.append(list);
    }
    container.append(li);
  });
  restoreUnclaimedCandidateSelection();
}

function restoreUnclaimedCandidateSelection() {
  const container = el("unclaimed-candidates");
  if (!container) return;
  for (const input of container.querySelectorAll('input[name="unclaimed-candidate"]')) {
    input.checked = !input.disabled && input.value !== "" && input.value === unclaimed.subscriptionId;
  }
}

function renderUnclaimedHistory() {
  const container = el("unclaimed-history");
  if (!container) return;
  container.replaceChildren();
  const history = unclaimed.detail && Array.isArray(unclaimed.detail.history)
    ? unclaimed.detail.history.filter((item) => item && typeof item === "object")
    : [];
  if (!history.length) {
    container.append(emptyListItem("暂无核对或处理记录"));
    return;
  }
  for (const entry of history) {
    const li = document.createElement("li");
    const head = document.createElement("div");
    head.className = "history-head";
    const time = document.createElement("span");
    time.className = "history-time";
    time.textContent = formatTime(pick(entry, "created_ms", "time_ms", "t_ms", "at_ms", "occurred_ms"));
    const action = document.createElement("span");
    action.className = "history-action";
    action.textContent = textOf(pick(entry, "action", "kind", "event", "resolution", "status"), "记录");
    head.append(time, action);
    li.append(head);

    const reason = pick(entry, "reason", "note", "detail");
    if (reason !== undefined) {
      const node = document.createElement("span");
      node.className = "history-reason";
      node.textContent = `理由：${textOf(reason, "（未记录）")}`;
      li.append(node);
    }

    const metaBits = [];
    const from = pick(entry, "from_status", "previous_status");
    const to = pick(entry, "to_status", "new_status", "resolution_status");
    if (from !== undefined || to !== undefined) metaBits.push(`${textOf(from, "?")} → ${textOf(to, "?")}`);
    const subscriptionId = pick(entry, "subscription_id");
    if (subscriptionId !== undefined) metaBits.push(`申购单 ${textOf(subscriptionId)}`);
    const payoutId = pick(entry, "payout_id", "payout_business_key", "payment_id");
    if (payoutId !== undefined) metaBits.push(`付款单 ${textOf(payoutId)}`);
    const version = pick(entry, "version");
    if (version !== undefined) metaBits.push(`版本 ${textOf(version)}`);
    // The actor is an opaque server-derived session hash, never a person and
    // never a credential: it is shown so two operators can tell entries apart.
    const actor = pick(entry, "actor", "actor_hash", "actor_id", "operator");
    if (actor !== undefined) metaBits.push(`管理会话 ${textOf(actor)}`);
    if (metaBits.length) {
      const meta = document.createElement("span");
      meta.className = "history-meta";
      meta.textContent = metaBits.join(" · ");
      li.append(meta);
    }
    container.append(li);
  }
}

function currentUnclaimedAction() {
  const refund = el("unclaimed-action-refund");
  return refund && refund.checked ? "refund" : "link";
}

function currentUnclaimedCandidate() {
  const container = el("unclaimed-candidates");
  if (container) {
    const checked = container.querySelector('input[name="unclaimed-candidate"]:checked');
    if (checked && checked.value) return checked.value;
  }
  return unclaimed.subscriptionId || "";
}

function unclaimedDirty() {
  const reason = el("unclaimed-reason");
  return unclaimed.preview !== null || (reason !== null && reason.value.trim() !== "");
}

//: While the operator is typing or has a preview on screen, polling must not
//: rewrite the detail: the draft stays exactly as typed and re-render waits.
function unclaimedReviewActive() {
  if (unclaimed.detail && !isUnresolvedRow(unclaimed.detail)) return false;
  if (unclaimedDirty()) return true;
  const detail = el("unclaimed-detail");
  const active = document.activeElement;
  return !!(detail && active && active !== document.body && detail.contains(active));
}

function showUnclaimedError(message) {
  const node = el("unclaimed-error");
  if (!node) return;
  node.hidden = false;
  node.textContent = message;
}

function clearUnclaimedError() {
  const node = el("unclaimed-error");
  if (!node) return;
  node.hidden = true;
  node.textContent = "";
}

function setUnclaimedNote(text) {
  unclaimed.note = text || "";
  const node = el("unclaimed-draft-note");
  if (!node) return;
  node.hidden = !unclaimed.note;
  node.textContent = unclaimed.note;
}

function setUnclaimedBusy(busy, label) {
  unclaimed.busy = busy;
  const button = el("unclaimed-preview-btn");
  if (button) {
    button.disabled = busy;
    button.textContent = busy ? (label || "正在核对…") : "核对预览";
  }
  updateUnclaimedSubmitState();
}

function resetUnclaimedForm() {
  const reason = el("unclaimed-reason");
  if (reason) reason.value = "";
  const check = el("unclaimed-confirm-check");
  if (check) check.checked = false;
  unclaimed.preview = null;
  unclaimed.subscriptionId = "";
  unclaimed.note = "";
  clearUnclaimedError();
  setUnclaimedNote("");
  restoreUnclaimedCandidateSelection();
  renderUnclaimedPreview();
}

function closeUnclaimedDetail() {
  unclaimed.selected = null;
  unclaimed.detail = null;
  resetUnclaimedForm();
  const detail = el("unclaimed-detail");
  if (detail) detail.hidden = true;
  unclaimed.listSignature = null;
  renderUnclaimedList();
}

//: Everything that can identify a receipt, a draft or a preview is dropped when
//: the console closes: nothing about an operator's review survives a logout.
function resetUnclaimedPanel() {
  unclaimed.fundId = "";
  unclaimed.status = "";
  unclaimed.rows = [];
  unclaimed.listSignature = null;
  unclaimed.selected = null;
  unclaimed.detail = null;
  unclaimed.preview = null;
  unclaimed.subscriptionId = "";
  unclaimed.busy = false;
  unclaimed.note = "";
  unclaimedReviewInputs(false);
  const list = el("unclaimed-list");
  if (list) list.replaceChildren();
  const detail = el("unclaimed-detail");
  if (detail) detail.hidden = true;
  for (const id of ["unclaimed-fields", "unclaimed-candidates", "unclaimed-history",
    "unclaimed-preview-fields", "unclaimed-preview-errors", "unclaimed-confirm-list"]) {
    const node = el(id);
    if (node) node.replaceChildren();
  }
  const preview = el("unclaimed-preview");
  if (preview) preview.hidden = true;
  const reason = el("unclaimed-reason");
  if (reason) reason.value = "";
  const check = el("unclaimed-confirm-check");
  if (check) {
    check.checked = false;
    check.disabled = true;
  }
  const button = el("unclaimed-resolve-btn");
  if (button) {
    button.disabled = true;
    button.textContent = "确认提交处理";
  }
  const liveHint = el("unclaimed-live-hint");
  if (liveHint) liveHint.hidden = true;
  const mode = el("unclaimed-mode");
  if (mode) {
    mode.dataset.state = "unknown";
    mode.textContent = "模式未知";
  }
  const statusFilter = el("unclaimed-status-filter");
  if (statusFilter) statusFilter.value = "";
  const fundFilter = el("unclaimed-fund-filter");
  if (fundFilter) fundFilter.value = "";
  clearUnclaimedError();
  setUnclaimedNote("");
  renderUnclaimedMessage("尚未加载未认领款。");
  updateUnclaimedFundOptions([]);
}

function unclaimedReviewInputs(enabled) {
  for (const id of ["unclaimed-preview-btn", "unclaimed-action-link", "unclaimed-action-refund"]) {
    const node = el(id);
    if (node) node.disabled = enabled === false;
  }
  const reason = el("unclaimed-reason");
  if (reason) reason.disabled = enabled === false;
}

function selectUnclaimed(fundId, id) {
  if (!fundId || !id) return;
  unclaimed.selected = { fundId, id };
  unclaimed.detail = null;
  unclaimed.preview = null;
  unclaimed.subscriptionId = "";
  const reason = el("unclaimed-reason");
  if (reason) reason.value = "";
  const check = el("unclaimed-confirm-check");
  if (check) check.checked = false;
  clearUnclaimedError();
  setUnclaimedNote("");
  unclaimed.listSignature = null;
  renderUnclaimedList();
  const epoch = sessionEpoch;
  loadUnclaimedDetail(epoch, unclaimed.selected, { resetForm: true });
}

function renderUnclaimedDetailMessage(message) {
  const detail = el("unclaimed-detail");
  if (!detail) return;
  detail.hidden = false;
  const title = el("unclaimed-detail-title");
  if (title) title.textContent = "未认领流水详情";
  const sub = el("unclaimed-detail-sub");
  if (sub) sub.textContent = message;
  const badge = el("unclaimed-status-badge");
  if (badge) {
    badge.dataset.state = "unknown";
    badge.textContent = "状态未知";
  }
  renderUnclaimedFacts(el("unclaimed-fields"), null);
  const candidates = el("unclaimed-candidates");
  if (candidates) {
    candidates.replaceChildren(emptyListItem("详情未加载：处理入口已停用"));
  }
  const history = el("unclaimed-history");
  if (history) history.replaceChildren(emptyListItem("详情未加载"));
  unclaimedReviewInputs(false);
  const preview = el("unclaimed-preview");
  if (preview) preview.hidden = true;
}

function renderUnclaimedDetail(options = {}) {
  const record = unclaimed.detail;
  if (!record) return;
  const detail = el("unclaimed-detail");
  if (detail) detail.hidden = false;
  // One receipt gets exactly one decision: a resolved row shows its outcome and
  // history, but the processing form stays disabled instead of offering a retry
  // the server would reject anyway.
  const resolved = !isUnresolvedRow(record);
  const reviewForm = el("unclaimed-form");
  if (reviewForm) reviewForm.hidden = resolved;
  unclaimedReviewInputs(!resolved);

  const info = unclaimedStatusInfo(record);
  const title = el("unclaimed-detail-title");
  if (title) title.textContent = `未认领流水 ${textOf(pick(record, "id", "unclaimed_id"))}`;
  const sub = el("unclaimed-detail-sub");
  if (sub) {
    sub.textContent = [
      `基金 ${textOf(pick(record, "fund_id"))}`,
      `付款人 ${textOf(unclaimedPayer(record))}`,
      `金额 ${formatMoney(unclaimedAmountUnits(record))} 小鱼干`,
      `权威到账 ${formatTime(unclaimedArrival(record))}`,
    ].join(" · ");
  }
  const badge = el("unclaimed-status-badge");
  if (badge) {
    badge.dataset.state = info.tone;
    badge.textContent = info.text;
  }

  renderUnclaimedFacts(el("unclaimed-fields"), record);
  renderUnclaimedCandidates();
  renderUnclaimedHistory();

  const candidates = Array.isArray(unclaimed.detail.candidates)
    ? unclaimed.detail.candidates.filter((item) => item && typeof item === "object")
    : [];
  const eligibleCount = candidates.filter(candidateEligible).length;
  const hint = el("unclaimed-candidate-hint");
  if (hint) {
    if (resolved) {
      hint.textContent = `该流水已处理（${info.text}），不能重复处理：一笔到账只产生一个结果。需要更正时走对账与审计流程，不要覆盖历史结论。`;
    } else if (currentUnclaimedAction() === "refund") {
      hint.textContent = "原路全额退款：无需选择申购单，只退给原付款人，金额与到账记录一致；实际进度在本流水详情跟踪。";
    } else if (eligibleCount) {
      hint.textContent = `关联处理：请在上方候选申购单中选中要关联的那一单（当前 ${eligibleCount} 单可关联）。金额、到账时间和付款人都不能修改；附言填错可以人工关联，但原始附言会原样保留并写入理由。`;
    } else {
      hint.textContent = "没有可关联的候选申购单：只能选择原路全额退款，或保持待核对等待更多证据。系统不会新建、补写或倒签申购单。";
    }
  }

  if (options.resetForm === true) {
    const link = el("unclaimed-action-link");
    const refund = el("unclaimed-action-refund");
    if (link && refund) (eligibleCount ? link : refund).checked = true;
    resetUnclaimedForm();
  }
  restoreUnclaimedCandidateSelection();
  renderUnclaimedPreview();
}

function updateUnclaimedSubmitState() {
  const check = el("unclaimed-confirm-check");
  const button = el("unclaimed-resolve-btn");
  const hint = el("unclaimed-live-hint");
  if (!check || !button) return;
  const preview = unclaimed.preview;
  const live = !!(preview && preview.live === true);
  const eligible = !!(preview && preview.preview && preview.preview.eligible === true);
  let blockedReason = "";
  if (unclaimed.busy) blockedReason = "正在提交处理…";
  else if (!preview) blockedReason = "请先执行核对预览，再提交处理。";
  else if (!live) blockedReason = "当前为只读预览：可以核对，但不能提交处理；账务与退款队列不会发生变化。";
  else if (!eligible) blockedReason = "预览未通过：处理条件不满足，请修正后重新预览，或改用其它处理方式。";

  const blocked = blockedReason !== "" && (unclaimed.busy || !preview || !live || !eligible);
  check.disabled = blocked;
  if (blocked) check.checked = false;
  button.disabled = blocked || !check.checked;
  button.textContent = unclaimed.busy ? "正在提交…" : "确认提交处理";
  if (hint) {
    const text = blockedReason && preview ? blockedReason : "";
    hint.hidden = text === "";
    hint.textContent = text;
  }
}

//: A draft in the form is never thrown away by a background refresh; the note
//: tells the operator why the detail stopped auto-updating.
function unclaimedDraftNote() {
  if (unclaimed.preview) return "";
  const reason = el("unclaimed-reason");
  if (!reason || reason.value.trim() === "") return "";
  return "草稿已保留：自动刷新不会覆盖正在填写的内容；提交前需要先执行核对预览。";
}

function invalidateUnclaimedPreview(message) {
  const hadPreview = unclaimed.preview !== null;
  unclaimed.preview = null;
  if (hadPreview) renderUnclaimedPreview();
  setUnclaimedNote(hadPreview && message ? message : unclaimedDraftNote());
}

function renderPreviewFacts() {
  const container = el("unclaimed-preview-fields");
  if (!container) return;
  container.replaceChildren();
  const preview = unclaimed.preview ? unclaimed.preview.preview : null;
  if (!preview) return;
  const record = preview.record && typeof preview.record === "object" ? preview.record : (unclaimed.detail || {});
  for (const [label, value] of unclaimedFacts(record)) addMetric(container, label, value);
  const subscription = preview.subscription && typeof preview.subscription === "object" ? preview.subscription : null;
  if (subscription) {
    addMetric(container, "预览匹配申购单", textOf(pick(subscription, "subscription_id", "id")));
    addMetric(container, "申购人", textOf(pick(subscription, "user_id", "from_user_id")));
    addMetric(container, "订单金额（小鱼干）", formatMoney(pick(subscription, "total_units", "amount_units", "paid_units", "amount")));
    addMetric(container, "订单状态", textOf(pick(subscription, "status", "state")));
    addMetric(container, "订单创建", formatTime(pick(subscription, "created_ms", "created_at_ms")));
    addMetric(container, "有效期至", formatTime(pick(subscription, "expires_ms", "expires_at_ms")));
  }
}

function renderConfirmRows(reason) {
  const container = el("unclaimed-confirm-list");
  if (!container) return;
  container.replaceChildren();
  const preview = unclaimed.preview ? unclaimed.preview.preview : null;
  if (!preview) return;
  const detail = unclaimed.detail || {};
  const record = preview.record && typeof preview.record === "object" ? preview.record : detail;
  const action = currentUnclaimedAction();
  const amount = `${formatMoney(unclaimedAmountUnits(record))} 小鱼干`;
  const version = unclaimedVersion(pick(preview.record, "version") !== undefined ? preview.record : detail);
  const rows = [
    ["处理方式", UNCLAIMED_ACTIONS[action] || action],
    ["基金 / 流水", `${textOf(pick(record, "fund_id"))} / ${textOf(pick(record, "id", "unclaimed_id"))}`],
    ["付款人（原始）", textOf(unclaimedPayer(record))],
    ["金额（不可修改）", amount],
    ["权威到账时间", formatTime(unclaimedArrival(record))],
    ["原始附言（保留）", unclaimedNote(record)],
  ];
  if (action === "link") {
    const subscription = preview.subscription && typeof preview.subscription === "object"
      ? preview.subscription : null;
    rows.push(["关联申购单", textOf(pick(subscription || {}, "subscription_id", "id"), currentUnclaimedCandidate() || "未选择")]);
    rows.push(["处理结果", "转为待确认申购；份额与资本流入仍由既有结算批次发行，净值不受影响"]);
  } else {
    rows.push(["退款去向", `只退回原付款人 ${textOf(unclaimedPayer(record))}，金额 ${amount}，不能分期或改额`]);
    rows.push(["处理结果", "生成一笔付款队列（排队不等于已付款）；实际结果在本流水详情跟踪，未知结果需先对账"]);
  }
  rows.push(["处理理由", reason]);
  rows.push(["记录版本", `${version}（提交时按该版本做原子校验，期间被他人处理会要求重新核对）`]);

  for (const [label, value] of rows) {
    const li = document.createElement("li");
    const left = document.createElement("span");
    left.className = "confirm-label";
    left.textContent = label;
    const right = document.createElement("span");
    right.className = "confirm-value";
    right.textContent = value;
    li.append(left, right);
    container.append(li);
  }
}

function renderUnclaimedPreview(reasonOverride) {
  const section = el("unclaimed-preview");
  if (!section) return;
  const preview = unclaimed.preview ? unclaimed.preview.preview : null;
  if (!preview) {
    section.hidden = true;
    updateUnclaimedSubmitState();
    return;
  }
  section.hidden = false;

  const record = unclaimed.detail || {};
  const reason = reasonOverride !== undefined
    ? reasonOverride
    : (el("unclaimed-reason") ? el("unclaimed-reason").value.trim() : "");
  const eligible = preview.eligible === true;
  setStatusLine(el("unclaimed-preview-verdict"), eligible
    ? ["ok", "预览通过：服务端已重新读取权威到账记录，条件满足，可以进入最终确认。"]
    : ["bad", "预览未通过：服务端重新校验后条件不满足，请勿提交。"]);

  const errors = el("unclaimed-preview-errors");
  if (errors) {
    errors.replaceChildren();
    const list = Array.isArray(preview.errors) ? preview.errors.filter((item) => item !== null && item !== undefined && String(item) !== "") : [];
    if (list.length) {
      for (const item of list) {
        const li = document.createElement("li");
        li.textContent = String(item);
        errors.append(li);
      }
    } else if (!eligible) {
      errors.append(emptyListItem("服务端未返回具体原因，请刷新详情或改用其它处理方式"));
    } else {
      errors.append(emptyListItem("没有问题项"));
    }
  }

  const action = currentUnclaimedAction();
  if (preview.action !== undefined && preview.action !== action) {
    showUnclaimedError("处理方式已变化，请重新预览后再提交。");
  }

  renderPreviewFacts();
  renderConfirmRows(reason);
  updateUnclaimedSubmitState();
}

async function loadUnclaimed(epoch, options = {}) {
  const query = new URLSearchParams();
  if (unclaimed.fundId) query.set("fund_id", unclaimed.fundId);
  if (unclaimed.status) query.set("status", unclaimed.status);
  const suffix = query.toString();
  try {
    const data = await api(`/api/unclaimed${suffix ? `?${suffix}` : ""}`);
    if (epoch !== sessionEpoch) return;
    const rows = Array.isArray(data.unclaimed) ? data.unclaimed.filter((row) => row && typeof row === "object") : [];
    unclaimed.rows = rows;
    renderUnclaimedSummary(data);
    renderUnclaimedList();
    if (!unclaimed.selected) return;
    if (unclaimedReviewActive() && options.force !== true) return;  // never clobber a live draft
    await loadUnclaimedDetail(epoch, unclaimed.selected, {});
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    renderUnclaimedMessage(`未认领款加载失败：${err.message || err}`);
  }
}

async function loadUnclaimedDetail(epoch, target, options = {}) {
  if (!target) return;
  try {
    const data = await api(`/api/funds/${encodeURIComponent(target.fundId)}/unclaimed/${encodeURIComponent(target.id)}`);
    if (epoch !== sessionEpoch) return;
    if (!unclaimed.selected || unclaimed.selected.id !== target.id || unclaimed.selected.fundId !== target.fundId) return;
    const record = data && typeof data.record === "object" && data.record ? data.record : data;
    if (!record || typeof record !== "object") throw new Error("详情格式无效");

    const previous = unclaimed.detail;
    const previousVersion = previous ? unclaimedVersion(previous) : null;
    unclaimed.detail = record;
    if (previousVersion !== null && unclaimedVersion(record) !== previousVersion && unclaimed.preview) {
      // Someone else moved the record while this operator was reviewing: the
      // preview belongs to the old version and must be redone.
      unclaimed.preview = null;
      setUnclaimedNote("记录已被其他操作更新，原预览已失效：请重新核对并再次预览。");
    }
    renderUnclaimedDetail({ resetForm: options.resetForm === true });
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    if (!unclaimed.selected || unclaimed.selected.id !== target.id) return;
    renderUnclaimedDetailMessage(`详情加载失败：${err.message || err}`);
  }
}

function unclaimedInputSignature() {
  const target = unclaimed.selected || {};
  return JSON.stringify([target.fundId, target.id, currentUnclaimedAction(),
    currentUnclaimedCandidate(), (el("unclaimed-reason")?.value || "").trim()]);
}

async function previewUnclaimed(event) {
  if (event) event.preventDefault();
  if (unclaimed.busy) return;
  const target = unclaimed.selected;
  if (!target || !unclaimed.detail) {
    showUnclaimedError("请先在列表中选择一条未认领流水。");
    return;
  }
  const action = currentUnclaimedAction();
  const reasonNode = el("unclaimed-reason");
  const reason = reasonNode ? reasonNode.value.trim() : "";
  if (!reason) {
    showUnclaimedError("处理理由必填：请写明核对依据（付款人、到账时间与订单的对应关系）。");
    if (reasonNode) reasonNode.focus();
    return;
  }
  if (reason.length > 500) {
    showUnclaimedError("处理理由最多 500 字。");
    return;
  }
  const subscriptionId = action === "link" ? currentUnclaimedCandidate() : "";
  if (action === "link" && !subscriptionId) {
    showUnclaimedError("关联处理必须先在上方的候选申购单中选中一单；没有合适候选时请改用原路退款或保持待核对。");
    return;
  }

  clearUnclaimedError();
  setUnclaimedNote("");
  const epoch = sessionEpoch;
  const signature = unclaimedInputSignature();
  setUnclaimedBusy(true, "正在核对…");
  try {
    const body = { action };
    if (action === "link") body.subscription_id = subscriptionId;
    const data = await api(`/api/funds/${encodeURIComponent(target.fundId)}/unclaimed/${encodeURIComponent(target.id)}/preview`, {
      method: "POST",
      body,
    });
    if (epoch !== sessionEpoch) return;
    if (!unclaimed.selected || unclaimed.selected.id !== target.id) return;
    const preview = data && typeof data.preview === "object" && data.preview ? data.preview : {};
    if (signature !== unclaimedInputSignature()) {
      invalidateUnclaimedPreview("核对期间输入已改变，请重新预览。");
      return;
    }
    unclaimed.preview = { preview, live: data.live === true, signature };
    unclaimed.subscriptionId = subscriptionId;
    restoreUnclaimedCandidateSelection();
    renderUnclaimedPreview(reason);
    if (data.live !== true) {
      setUnclaimedNote("只读预览模式（live=false）：以下为校验结果，提交处理会被拒绝。");
    }
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    unclaimed.preview = null;
    renderUnclaimedPreview();
    showUnclaimedError(`预览失败：${err.message || err}`);
  } finally {
    if (epoch === sessionEpoch) setUnclaimedBusy(false);
  }
}

async function resolveUnclaimed() {
  const previewState = unclaimed.preview;
  const target = unclaimed.selected;
  if (!previewState || !target || unclaimed.busy) return;
  if (previewState.live !== true || previewState.preview.eligible !== true) return;
  if (!el("unclaimed-confirm-check")?.checked || previewState.signature !== unclaimedInputSignature()) return;
  const action = currentUnclaimedAction();
  const reasonNode = el("unclaimed-reason");
  const reason = reasonNode ? reasonNode.value.trim() : "";
  if (!reason) {
    showUnclaimedError("处理理由必填。");
    return;
  }
  const previewRecord = previewState.preview.record && typeof previewState.preview.record === "object"
    ? previewState.preview.record : null;
  const version = unclaimedVersion(pick(previewRecord, "version") !== undefined ? previewRecord : unclaimed.detail);
  const body = { action, version, reason };
  if (action === "link") {
    body.subscription_id = currentUnclaimedCandidate();
    if (!body.subscription_id) {
      showUnclaimedError("关联处理必须先选中候选申购单。");
      return;
    }
  }

  clearUnclaimedError();
  const epoch = sessionEpoch;
  setUnclaimedBusy(true, "正在提交…");
  try {
    const data = await api(`/api/funds/${encodeURIComponent(target.fundId)}/unclaimed/${encodeURIComponent(target.id)}/resolve`, {
      method: "POST",
      body,
    });
    if (epoch !== sessionEpoch) return;
    const record = data && typeof data.record === "object" && data.record ? data.record : {};
    const info = unclaimedStatusInfo(record);
    flashNote(`处理已提交：${UNCLAIMED_ACTIONS[action] || action} · ${info.text}`, "ok");
    unclaimed.preview = null;
    unclaimed.subscriptionId = "";
    unclaimed.detail = record && Object.keys(record).length ? record : null;
    if (reasonNode) reasonNode.value = "";
    const check = el("unclaimed-confirm-check");
    if (check) check.checked = false;
    setUnclaimedNote("");
    renderUnclaimedPreview();
    await refreshAll({ full: true });
    if (epoch !== sessionEpoch) return;
    await loadUnclaimed(epoch, { force: true });
    await loadUnclaimedDetail(epoch, target, { resetForm: true });
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    const code = String((err && err.message) || err);
    if (/version|stale|conflict/i.test(code)) {
      showUnclaimedError(`提交被拒绝：该记录已被其他操作更新（${code}）。已重新加载详情，请重新核对后再提交；同一笔流水只会产生一个处理结果。`);
      unclaimed.preview = null;
      renderUnclaimedPreview();
      await loadUnclaimed(epoch, { force: true });
      await loadUnclaimedDetail(epoch, target, {});
    } else if (/live/i.test(code)) {
      showUnclaimedError(`提交被拒绝：当前 live=false 为只读预览模式（${code}）。未生成付款队列，也未改动任何账务。`);
      unclaimed.preview = null;
      renderUnclaimedPreview();
    } else {
      showUnclaimedError(`提交失败：${code}`);
    }
  } finally {
    if (epoch === sessionEpoch) {
      setUnclaimedBusy(false);
      updateUnclaimedSubmitState();
    }
  }
}

function updateUnclaimedFundOptions(funds) {
  const select = el("unclaimed-fund-filter");
  if (!select) return;
  const wanted = funds.map((fund) => [fund.fund_id || "", fund.label || fund.fund_id || ""]);
  const current = Array.from(select.options).slice(1).map((option) => [option.value, option.textContent]);
  if (JSON.stringify(wanted) === JSON.stringify(current)) return;  // keep the operator's choice
  const previous = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部基金";
  select.append(all);
  for (const [value, label] of wanted) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }
  select.value = previous;
  if (select.value !== previous) select.value = "";
  unclaimed.fundId = select.value;
}

// ------------------------------------------------------------------ load

function applyStatus(status, options = {}) {
  state.lastStatus = status;
  const funds = Array.isArray(status.funds) ? status.funds : [];
  state.funds = funds.map(normalizeFund);
  renderBanner(status);
  updateBadges(status);
  renderFunds(funds, { full: options.full === true });
  updateFilterOptions(state.funds);
  updateUnclaimedFundOptions(state.funds);
}

async function loadEvents(epoch) {
  const query = new URLSearchParams({ limit: "100" });
  const filter = el("event-filter").value;
  if (filter) query.set("fund_id", filter);
  try {
    const data = await api(`/api/events?${query.toString()}`);
    if (epoch !== sessionEpoch) return;
    renderEvents(data.events || []);
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    renderListMessage("events-list", `事件加载失败：${err.message || err}`);
  }
}

async function loadOrders(epoch) {
  const query = new URLSearchParams({ limit: "100" });
  const filter = el("event-filter").value;
  if (filter) query.set("fund_id", filter);
  try {
    const data = await api(`/api/orders?${query.toString()}`);
    if (epoch !== sessionEpoch) return;
    renderOrders(data.orders || []);
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    renderListMessage("orders-list", `订单加载失败：${err.message || err}`);
  }
}

async function loadPanels(epoch) {
  await Promise.all([loadEvents(epoch), loadOrders(epoch), loadUnclaimed(epoch)]);
}

async function doRefresh(options = {}) {
  const epoch = sessionEpoch;
  try {
    const status = await api("/api/status");
    if (epoch !== sessionEpoch) return;  // signed out (or re-signed in) while loading
    showConsole();
    applyStatus(status, { full: options.full === true });
    await loadPanels(epoch);
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    if (epoch !== sessionEpoch) return;
    const banner = el("status-banner");
    if (banner) {
      banner.classList.remove("empty");
      banner.replaceChildren();
      banner.append(document.createTextNode(`状态加载失败：${err.message || err}`));
    }
  }
}

//: Single-flight refresh: concurrent callers share one cycle, and a request that
//: arrives mid-cycle triggers exactly one follow-up instead of a parallel load.
function refreshAll(options = {}) {
  if (refreshPromise) {
    refreshQueued = true;
    refreshQueuedFull = refreshQueuedFull || options.full === true;
    return refreshPromise;
  }
  refreshPromise = doRefresh(options).finally(() => {
    refreshPromise = null;
    if (refreshQueued && state.authenticated) {
      refreshQueued = false;
      const full = refreshQueuedFull;
      refreshQueuedFull = false;
      refreshAll({ full });
    }
  });
  return refreshPromise;
}

// ------------------------------------------------------------------ wiring

function wireStaticControls() {
  el("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = el("login-token");
    const error = el("login-error");
    error.hidden = true;
    const button = event.currentTarget.querySelector('button[type="submit"]');
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "正在登录…";
    try {
      await attemptLogin(input.value);
      input.value = "";  // the control token never lingers in the field
    } catch (err) {
      error.hidden = false;
      error.textContent = err.message === "unauthorized" ? "令牌无效" : `登录失败：${err.message || err}`;
    } finally {
      button.disabled = false;
      button.textContent = "进入控制台";
    }
  });

  el("logout-btn").addEventListener("click", async () => {
    showLogin();
    try { await api("/api/logout", { method: "POST", body: {} }); } catch (err) { /* ignore */ }
    showLogin();
  });

  el("refresh-btn").addEventListener("click", () => refreshAll({ full: true }));
  el("events-refresh").addEventListener("click", () => loadEvents(sessionEpoch));
  el("orders-refresh").addEventListener("click", () => loadOrders(sessionEpoch));
  el("event-filter").addEventListener("change", () => {
    const epoch = sessionEpoch;
    loadEvents(epoch);
    loadOrders(epoch);
  });

  // ------------------------------------------------- unclaimed review wiring

  el("unclaimed-refresh").addEventListener("click", () => loadUnclaimed(sessionEpoch, { force: true }));

  el("unclaimed-fund-filter").addEventListener("change", () => {
    unclaimed.fundId = el("unclaimed-fund-filter").value;
    loadUnclaimed(sessionEpoch, { force: true });
  });

  el("unclaimed-status-filter").addEventListener("change", () => {
    unclaimed.status = el("unclaimed-status-filter").value;
    loadUnclaimed(sessionEpoch, { force: true });
  });

  el("unclaimed-close").addEventListener("click", closeUnclaimedDetail);
  el("unclaimed-form").addEventListener("submit", previewUnclaimed);

  el("unclaimed-form").addEventListener("change", (event) => {
    if (event.target && event.target.name === "unclaimed-action") {
      invalidateUnclaimedPreview("处理方式已切换，请重新预览后再提交。");
      renderUnclaimedDetail();
    }
  });

  //: The candidate radios are re-created on every detail render, so the choice
  //: is tracked through event delegation instead of per-node listeners.
  el("unclaimed-candidates").addEventListener("change", (event) => {
    const input = event.target;
    if (!input || input.name !== "unclaimed-candidate") return;
    unclaimed.subscriptionId = input.checked ? input.value : "";
    invalidateUnclaimedPreview("已重新选择候选申购单，请再次预览。");
  });

  el("unclaimed-reason").addEventListener("input", () => {
    invalidateUnclaimedPreview("理由已修改：预览与最终确认已失效，请重新预览。");
  });

  el("unclaimed-confirm-check").addEventListener("change", updateUnclaimedSubmitState);
  el("unclaimed-resolve-btn").addEventListener("click", resolveUnclaimed);

  el("backup-btn").addEventListener("click", async () => {
    const epoch = sessionEpoch;
    try {
      const data = await api("/api/backup", { method: "POST", body: {} });
      if (epoch !== sessionEpoch) return;
      flashNote(`备份完成：${data.backup || "已生成"}`, "ok");
    } catch (err) {
      if (err && err.message === "unauthorized") return;
      if (epoch !== sessionEpoch) return;
      flashNote(`备份失败：${err.message || err}`, "bad");
    }
  });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      for (const card of document.querySelectorAll(".fund-card")) {
        const canvas = card.querySelector('[data-field="nav-chart"]');
        if (canvas) drawNav(canvas, canvas.__history || []);
      }
    }, 150);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.authenticated) refreshAll();
  });
}

wireStaticControls();
refreshAll();
