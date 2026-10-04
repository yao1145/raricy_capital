"use strict";

// Operator console for the two funds. No external dependencies, no CDN, no
// credential persistence: the control token is sent once to /api/login and the
// browser only keeps the short-lived HttpOnly session cookie.

const MONEY_SCALE = 10000;      // 1e-4 fish-credit units
const SHARE_SCALE = 100000000;  // 1e-8 share atoms

const el = (id) => document.getElementById(id);
const state = { authenticated: false, funds: [], filter: "" };

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
    if (phase === "stopped") return ["warn", "已停机"];
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

// --------------------------------------------------------------- api layer

async function api(path, options = {}) {
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
    showLogin();
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    const message = (data && (data.message || data.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return data || {};
}

// ---------------------------------------------------------------- login ui

function showLogin() {
  state.authenticated = false;
  el("login-panel").hidden = false;
  el("console").hidden = true;
  el("logout-btn").hidden = true;
}

function showConsole() {
  state.authenticated = true;
  el("login-panel").hidden = true;
  el("console").hidden = false;
  el("logout-btn").hidden = false;
}

async function attemptLogin(token) {
  await api("/api/login", { method: "POST", body: { token } });
  await refreshAll();
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

function renderMetrics(container, fund) {
  container.replaceChildren();
  addMetric(container, "基金净资产", formatMoney(fund.equity_units), metricTone(fund.equity_units));
  addMetric(container, "交易桶现金", formatMoney(fund.wallet_units));
  addMetric(container, "持仓价值", formatMoney(fund.position_value_units));
  addMetric(container, "可用现金", formatMoney(fund.available_cash_units));
  addMetric(container, "在外份额", formatShares(fund.shares_atoms));
  addMetric(container, "已实现利润", formatMoney(fund.realized_profit_units), metricTone(fund.realized_profit_units));
  addMetric(container, "费用余额", formatMoney(fund.fee_balance_units));
  addMetric(container, "待确认收款", formatMoney(fund.pending_receipts_units));
  addMetric(container, "应付款", formatMoney(fund.liabilities_units));
  addMetric(container, "未认领款", formatMoney(fund.unclaimed_units));
  addMetric(container, "资本流入", formatMoney(fund.capital_flows_units), metricTone(fund.capital_flows_units));
  if (fund.benchmark_nav !== undefined) addMetric(container, "基准净值", formatNav(fund.benchmark_nav));
  if (fund.user_shares_atoms !== undefined) addMetric(container, "我的份额", formatShares(fund.user_shares_atoms));
  if (fund.user_value_units !== undefined) addMetric(container, "我的价值", formatMoney(fund.user_value_units));
}

function renderHolders(container, holders) {
  container.replaceChildren();
  if (!holders.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无持有人记录";
    container.append(li);
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

function drawNav(canvas, history) {
  const points = parseHistory(history);
  canvas.__history = history;
  const empty = canvas.parentElement.querySelector('[data-field="nav-empty"]');
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssWidth = Math.max(canvas.clientWidth || canvas.getBoundingClientRect().width || 320, 200);
  const cssHeight = 150;
  canvas.width = Math.floor(cssWidth * dpr);
  canvas.height = Math.floor(cssHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);

  if (points.length < 2) {
    if (empty) empty.hidden = false;
    if (points.length === 1) {
      ctx.fillStyle = "#2ec4b6";
      ctx.beginPath();
      ctx.arc(cssWidth / 2, cssHeight / 2, 3, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  if (empty) empty.hidden = true;

  const pad = { top: 12, right: 12, bottom: 20, left: 46 };
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

  ctx.strokeStyle = "#16324a";
  ctx.lineWidth = 1;
  ctx.font = "10px Consolas, monospace";
  ctx.fillStyle = "#8ca6ba";
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
  gradient.addColorStop(0, "rgba(46, 196, 182, 0.30)");
  gradient.addColorStop(1, "rgba(46, 196, 182, 0.02)");

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
  ctx.strokeStyle = "#2ec4b6";
  ctx.lineWidth = 1.6;
  ctx.stroke();

  ctx.beginPath();
  ctx.arc(xOf(last), yOf(last), 3, 0, Math.PI * 2);
  ctx.fillStyle = "#7fe3d8";
  ctx.fill();
}

// ----------------------------------------------------------- fund cards

function applyFund(card, fund) {
  card.dataset.fundId = fund.fund_id || "";
  card.querySelector('[data-field="label"]').textContent = fund.label || fund.fund_id || "未命名基金";
  card.querySelector('[data-field="fund_id"]').textContent = fund.fund_id || "";
  card.querySelector('[data-field="nav"]').textContent = formatNav(fund.nav);
  card.querySelector('[data-field="updated_ms"]').textContent = formatTime(fund.updated_ms);

  const badge = card.querySelector('[data-field="state"]');
  const [tone, text] = stateBadge(fund);
  badge.dataset.state = tone;
  badge.textContent = text;

  renderMetrics(card.querySelector('[data-field="metrics"]'), fund);
  const holdersBox = card.querySelector('[data-field="holders"]');
  renderHolders(holdersBox, []);
  loadHolders(card, fund.fund_id);
  loadNavHistory(card, fund.fund_id);
}

function normalizeFund(fund) {
  if (fund && typeof fund.status === "object" && fund.status !== null) {
    return Object.assign({}, fund, fund.status);
  }
  return fund || {};
}

function renderFunds(funds) {
  const container = el("funds");
  container.replaceChildren();
  const template = el("fund-card-template");
  if (!Array.isArray(funds) || !funds.length) {
    const card = document.createElement("article");
    card.className = "card fund-card";
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "暂无基金状态数据";
    card.append(empty);
    container.append(card);
    return;
  }
  for (const raw of funds) {
    const fund = normalizeFund(raw);
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector(".fund-card");
    applyFund(card, fund);
    wireControls(card);
    container.append(fragment);
  }
}

async function loadNavHistory(card, fundId) {
  if (!fundId) return;
  try {
    const data = await api(`/api/funds/${encodeURIComponent(fundId)}/nav-history`);
    drawNav(card.querySelector('[data-field="nav-chart"]'), data.history || []);
  } catch (err) {
    drawNav(card.querySelector('[data-field="nav-chart"]'), []);
  }
}

async function loadHolders(card, fundId) {
  if (!fundId) return;
  try {
    const data = await api(`/api/holders?fund_id=${encodeURIComponent(fundId)}`);
    renderHolders(card.querySelector('[data-field="holders"]'), data.holders || []);
  } catch (err) {
    renderHolders(card.querySelector('[data-field="holders"]'), []);
  }
}

// --------------------------------------------------------------- controls

function wireControls(card) {
  const fundId = () => card.dataset.fundId;

  async function run(label, fn) {
    try {
      await fn();
      feedback(card, `${label}成功`, "ok");
      await refreshAll();
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
      form.password.value = "";
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
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无事件";
    list.append(li);
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
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "暂无订单";
    list.append(li);
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

function renderBanner(status) {
  const banner = el("status-banner");
  banner.replaceChildren();
  const entries = [];
  const network = status.network || {};
  const state = status.status || {};
  // `live` gates external writes (trading and transfers); it is not a real-money
  // "实盘" flag, so it is labelled as enabled writes vs a read-only preview.
  if (status.live !== undefined) entries.push(["运行模式", status.live ? "交易与转账已启用" : "只读预览"]);
  if (network.state !== undefined) entries.push(["网络", String(network.state)]);
  if (state.detail !== undefined) entries.push(["状态", String(state.detail)]);
  if (state.last_health_ms !== undefined) entries.push(["最近健康检查", formatTime(state.last_health_ms)]);
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
  const liveBadge = el("live-badge");
  if (status.live) { liveBadge.dataset.state = "warn"; liveBadge.textContent = "交易与转账已启用"; }
  else if (status.live !== undefined) { liveBadge.dataset.state = "ok"; liveBadge.textContent = "只读预览"; }
}

function updateFilterOptions(funds) {
  const select = el("event-filter");
  const previous = select.value;
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "全部基金";
  select.append(all);
  for (const fund of funds) {
    const option = document.createElement("option");
    option.value = fund.fund_id || "";
    option.textContent = fund.label || fund.fund_id || "";
    select.append(option);
  }
  select.value = previous;
}

// ------------------------------------------------------------------ load

async function loadStatus() {
  const status = await api("/api/status");
  const funds = Array.isArray(status.funds) ? status.funds.map(normalizeFund) : [];
  state.funds = funds;
  renderBanner(status);
  renderFunds(status.funds || []);
  updateFilterOptions(funds);
  renderEvents(status.events || []);
  const netBadge = el("net-badge");
  const network = status.network || {};
  if (network.state === undefined) { netBadge.dataset.state = "unknown"; netBadge.textContent = "网络未知"; }
  else if (network.ok === false) { netBadge.dataset.state = "bad"; netBadge.textContent = "网络异常"; }
  else { netBadge.dataset.state = "ok"; netBadge.textContent = "网络正常"; }
}

async function loadEvents() {
  const query = new URLSearchParams({ limit: "100" });
  const filter = el("event-filter").value;
  if (filter) query.set("fund_id", filter);
  const data = await api(`/api/events?${query.toString()}`);
  renderEvents(data.events || []);
}

async function loadOrders() {
  const filter = el("event-filter").value;
  const query = new URLSearchParams({ limit: "100" });
  if (filter) query.set("fund_id", filter);
  const data = await api(`/api/orders?${query.toString()}`);
  renderOrders(data.orders || []);
}

async function refreshAll() {
  try {
    await loadStatus();
    showConsole();
  } catch (err) {
    if (err && err.message === "unauthorized") return;
    const banner = el("status-banner");
    banner.classList.remove("empty");
    banner.replaceChildren();
    banner.append(document.createTextNode(`状态加载失败：${err.message || err}`));
  }
}

// ------------------------------------------------------------------ wiring

function wireStaticControls() {
  el("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = el("login-token");
    const error = el("login-error");
    error.hidden = true;
    try {
      await attemptLogin(input.value);
      input.value = "";
    } catch (err) {
      error.hidden = false;
      error.textContent = err.message === "unauthorized" ? "令牌无效" : `登录失败：${err.message || err}`;
    }
  });

  el("logout-btn").addEventListener("click", async () => {
    try { await api("/api/logout", { method: "POST", body: {} }); } catch (err) { /* ignore */ }
    showLogin();
  });

  el("refresh-btn").addEventListener("click", () => refreshAll());
  el("events-refresh").addEventListener("click", () => loadEvents());
  el("orders-refresh").addEventListener("click", () => loadOrders());
  el("event-filter").addEventListener("change", () => { loadEvents(); loadOrders(); });

  el("backup-btn").addEventListener("click", async () => {
    try {
      const data = await api("/api/backup", { method: "POST", body: {} });
      const banner = el("status-banner");
      banner.classList.remove("empty");
      banner.replaceChildren();
      const strong = document.createElement("strong");
      strong.textContent = "备份：";
      banner.append(strong, document.createTextNode(data.backup || "已完成"));
    } catch (err) {
      if (err && err.message !== "unauthorized") window.alert(`备份失败：${err.message || err}`);
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
}

wireStaticControls();
refreshAll();
