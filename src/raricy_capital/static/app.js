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

// ------------------------------------------------------------------ load

function applyStatus(status, options = {}) {
  state.lastStatus = status;
  const funds = Array.isArray(status.funds) ? status.funds : [];
  state.funds = funds.map(normalizeFund);
  renderBanner(status);
  updateBadges(status);
  renderFunds(funds, { full: options.full === true });
  updateFilterOptions(state.funds);
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
  await Promise.all([loadEvents(epoch), loadOrders(epoch)]);
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
