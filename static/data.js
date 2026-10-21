// ---------- 数据 ----------
// 依赖 app.js 里的 $、api、esc、state、MARKET、POOL、dirClass

const CCY = { CNY: "¥", HKD: "HK$", USD: "$" };
const SIGNED = new Set(["change_pct", "chg_20d", "chg_60d", "chg_250d", "dist_high_52w", "ma200_gap", "pnl_pct", "rev_yoy", "np_yoy", "np_cagr3"]);
Object.assign(state, { dataPool: "holding", dataMarket: "all", dataSort: null, dataOpen: null });

const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? null : Number(v).toFixed(d));
const signCls = (v) => (v > 0 ? "bull" : v < 0 ? "bear" : "");  // 红涨绿跌
function money(v, ccy = "CNY") {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  const body = a >= 1e12 ? `${(a / 1e12).toFixed(2)}万亿` : a >= 1e8 ? `${(a / 1e8).toFixed(2)}亿` : a >= 1e4 ? `${(a / 1e4).toFixed(2)}万` : a.toFixed(0);
  return `${s}${CCY[ccy] || ""}${body}`;
}

function fmtMetric(key, row) {
  const v = row.metrics[key];
  const meta = state.data.metricMeta[key];
  if (!meta) return "—";
  if (meta.fmt === "text") return esc(key === "report_period" && v ? v.slice(0, 7) : v || "—");
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  if (meta.fmt === "money") return money(v, key === "market_cap" ? row.currency : "CNY");
  if (meta.fmt === "price") return `${num(v, v >= 100 ? 1 : 2)} <span class="muted small">${row.currency}</span>`;
  if (meta.fmt === "x") return num(v, v >= 100 ? 0 : 2);
  const txt = `${SIGNED.has(key) && v > 0 ? "+" : ""}${num(v, Math.abs(v) >= 100 ? 0 : 1)}%`;
  if (SIGNED.has(key)) return `<span class="${signCls(v)}">${txt}</span>`;
  if (key === "pe_pct" || key === "pb_pct") return `<span class="${v >= 80 ? "hot" : v <= 20 ? "cool" : ""}">${txt}</span>`;
  if (key === "weight") return `<span class="wcell"><i style="width:${Math.min(100, v)}%"></i>${txt}</span>`;
  return txt;
}

async function loadData() {
  const el = $("#tab-data");
  if (!state.data) el.innerHTML = '<p class="muted">加载中…</p>';
  try {
    const d = await api("/api/data");
    d.metricMeta = Object.fromEntries(d.catalog.metrics.map((m) => [m.key, m]));
    state.data = d;
  } catch (e) { el.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  renderData();
  if (state.data.updating) setTimeout(pollStatus, 1000);  // 首次更新完成后 pollStatus 会刷新页面
}

function renderData() {
  const d = state.data;
  const el = $("#tab-data");
  if (!d.rows.length) {
    el.innerHTML = `<div class="card"><p>还没有公司。先在「资讯」页导入或添加公司，这里会显示它们的估值、财务、仓位和关注点。</p></div>`;
    return;
  }
  el.innerHTML = `${renderOverview(d)}${renderEvents(d)}
    <div class="data-toolbar">
      <div class="chips">${["holding", "watching", "closed"].map((p) => `<button class="chip ${state.dataPool === p ? "on" : ""}" data-pool="${p}">${POOL[p]} ${d.summary.counts[p]}</button>`).join("")}</div>
      <div class="chips">${[["all", "全部市场"], ["A", "A股"], ["HK", "港股"], ["US", "美股"]].map(([k, v]) => `<button class="chip ${state.dataMarket === k ? "on" : ""}" data-market="${k}">${v}</button>`).join("")}</div>
      <button id="btn-prefs" class="small">⚙ 指标与关注点</button>
    </div>
    ${d.updating ? '<p class="warn small">有公司的行情和财务数据正在首次更新，通常一两分钟，完成后页面会自动刷新。</p>' : ""}
    <div id="data-table"></div>
    <p class="muted small">价格为最近一个已收盘交易日；港股、美股按中行折算价换成人民币（1 美元 = ${num(d.fx.USD, 4)} 元，1 港币 = ${num(d.fx.HKD, 4)} 元）。数据来自东方财富、百度股市通、Finnhub 等公开来源，可能有误差，以公司公告为准。</p>`;
  el.querySelectorAll("[data-pool]").forEach((b) => (b.onclick = () => { state.dataPool = b.dataset.pool; state.dataOpen = null; renderData(); }));
  el.querySelectorAll("[data-market]").forEach((b) => (b.onclick = () => { state.dataMarket = b.dataset.market; renderData(); }));
  $("#btn-prefs").onclick = openPrefs;
  $("#btn-digest")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "生成中…";
    try { await api("/api/data/digest", { method: "POST" }); await loadData(); } catch (err) { alert(err.message); e.target.disabled = false; }
  });
  el.querySelectorAll("[data-event]").forEach((row) => (row.onclick = () => toggleEvent(row)));
  el.querySelectorAll("[data-goto-company]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); openCompanyData(Number(b.dataset.gotoCompany)); }));
  renderTable();
}

function renderOverview(d) {
  const s = d.summary;
  const tile = (k, v, cls = "") => `<div class="stat"><div class="v ${cls}">${v}</div><div class="k">${k}</div></div>`;
  const pct = (v) => (v === null || v === undefined ? "—" : `${num(v)}%`);
  const tiles = [
    s.total_value != null ? tile("持仓市值（折人民币）", money(s.total_value)) : "",
    s.total_pnl != null ? tile(`浮动盈亏 ${s.total_pnl_pct != null ? `${s.total_pnl_pct > 0 ? "+" : ""}${num(s.total_pnl_pct)}%` : ""}`, money(s.total_pnl), signCls(s.total_pnl)) : "",
    tile(`加权 PE${s.pe_pct != null ? ` · 分位 ${num(s.pe_pct, 0)}%` : ""}`, s.pe != null ? num(s.pe) : "—"),
    tile("加权股息率", pct(s.dividend_yield)),
    tile("加权年度 ROE", pct(s.roe_fy)),
    tile("最大单一持仓", pct(s.top_weight)),
    tile("关注点", `<span class="hot">${s.risks}</span> <span class="muted small">风险</span> · <span class="cool">${s.chances}</span> <span class="muted small">机会</span>`),
  ].join("");
  const bar = (title, parts) => parts.length ? `<div class="dist"><div class="muted small">${title}</div>
    <div class="stack">${parts.map(([k, v], i) => `<i class="c${i % 6}" style="width:${v}%" title="${esc(k)} ${v}%"></i>`).join("")}</div>
    <div class="legend">${parts.map(([k, v], i) => `<span><b class="c${i % 6}"></b>${esc(k)} ${v}%</span>`).join("")}</div></div>` : "";
  const noWeights = s.counts.holding && !s.weighted_count;
  return `<section class="card data-overview">
    <div class="row-between"><h2>数据概览${d.digest ? ` <span class="muted small">${d.digest.date}</span>` : ""}</h2>
      <button id="btn-digest" class="small ghost">${d.digest ? "↻ 重新生成" : "生成 AI 概览"}</button></div>
    ${d.digest ? `<p class="digest-overview">${esc(d.digest.overview)}</p>
      ${d.digest.points.length ? `<ul class="points">${d.digest.points.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}`
      : `<p class="muted">每天检索后会根据${d.has_profile ? "你的画像" : "持仓数据"}写一段概览，也可以现在生成。${d.has_profile ? "" : "先去「我的画像」生成画像，概览和推荐指标会更贴合你的风格。"}</p>`}
    <div class="stats">${tiles}</div>
    ${noWeights ? `<p class="warn small">还没填仓位：点开下面的公司填买入份额和买入价（或只填权重），就能算出仓位分布、加权估值和盈亏。</p>` : ""}
    <div class="dists">${bar("市场分布（按仓位）", s.markets)}${bar("行业分布（按仓位）", s.industries)}</div>
  </section>`;
}

function renderEvents(d) {
  if (!d.events.length) return "";
  return `<section class="card events">
    <h2>最近 30 天的提醒</h2>
    ${d.events.map((e) => `
      <div class="event-row" data-event="${e.id}">
        <span class="muted small">${e.event_date}</span>
        <span class="alert-company">${esc(e.name)}</span>
        <span class="badge ${e.kind === "drop" ? "bear" : "neutral"}">${e.kind === "drop" ? "📉 " : "📄 "}${esc(e.title)}</span>
        <span class="event-sum">${e.kind === "drop"
          ? (e.analysis ? esc(e.analysis.summary) : '<span class="muted">点击分析原因</span>')
          : `<button class="small ghost" data-goto-company="${e.company_id}">看财报分析 →</button>`}</span>
      </div>
      <div class="event-detail hidden" id="ev-${e.id}">${e.analysis ? dropDetail(e) : ""}</div>`).join("")}
  </section>`;
}

function dropDetail(e) {
  const a = e.analysis;
  const d = e.data || {};
  const src = (ids) => ids.map((i) => a.sources?.[i]).filter(Boolean)
    .map((s) => s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.source || "来源")}</a>` : esc(s.source)).join(" · ");
  return `<p class="muted small">当日跌 ${num(Math.abs(d.change_pct))}%；${esc(d.index || "大盘")} ${d.index_change == null ? "涨跌未知" : `${d.index_change > 0 ? "+" : ""}${num(d.index_change, 2)}%`} · 判断：<b>${esc(a.category)}</b></p>
    <ul>${a.reasons.map((r) => `<li>${esc(r.text)} <span class="small">${src(r.source_ids)}</span></li>`).join("")}</ul>
    ${a.long_term ? `<p><b>对长期价值：</b>${esc(a.long_term)}</p>` : ""}
    ${a.check.length ? `<p class="small"><b>值得核实：</b>${a.check.map(esc).join("；")}</p>` : ""}`;
}

async function toggleEvent(row) {
  const detail = $(`#ev-${row.dataset.event}`);
  const ev = state.data.events.find((e) => e.id === Number(row.dataset.event));
  if (ev.kind !== "drop") return;
  if (!ev.analysis) {
    detail.innerHTML = '<p class="muted">AI 正在分析下跌原因…</p>';
    detail.classList.remove("hidden");
    try { ev.analysis = await api(`/api/events/${ev.id}/analyze`, { method: "POST" }); } catch (e) { detail.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
    row.querySelector(".event-sum").textContent = ev.analysis.summary;
    detail.innerHTML = dropDetail(ev);
    return;
  }
  detail.classList.toggle("hidden");
}

// ---------- 表格 ----------
function visibleRows() {
  const d = state.data;
  let rows = d.rows.filter((r) => r.status === state.dataPool && (state.dataMarket === "all" || r.market === state.dataMarket));
  const sort = state.dataSort || (state.dataPool === "holding" ? { key: "weight", dir: -1 } : null);
  if (state.dataPool === "closed") {
    rows.sort((a, b) => (b.trades[0]?.closed_at || "").localeCompare(a.trades[0]?.closed_at || ""));
  } else if (sort) {
    const val = (r) => (sort.key === "name" ? r.name : r.metrics[sort.key]);
    rows.sort((a, b) => {
      const x = val(a), y = val(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * sort.dir;
    });
  }
  return rows;
}

function renderTable() {
  const d = state.data;
  const rows = visibleRows();
  const el = $("#data-table");
  if (!rows.length) {
    el.innerHTML = `<p class="muted empty">${state.dataPool === "closed" ? "还没有清仓记录。持仓中的公司点开后可以「清仓」，这里会留下每一笔投资的盈亏和总结。"
      : state.dataPool === "watching" ? "没有观察中的公司。添加公司时可以选择「观察中（还没买）」。" : "这个筛选下没有公司。"}</p>`;
    return;
  }
  if (state.dataPool === "closed") return renderClosed(el, rows);
  const cols = d.prefs.metrics.filter((k) => d.metricMeta[k] && !(state.dataPool === "watching" && ["weight", "cost_weight", "market_value", "pnl_pct"].includes(k)));
  const sortMark = (k) => (state.dataSort?.key === k ? (state.dataSort.dir > 0 ? " ▲" : " ▼") : "");
  el.innerHTML = `<div class="tbl data-tbl"><table>
    <thead><tr><th class="sticky" data-sort="name">公司${sortMark("name")}</th>${cols.map((k) => `<th data-sort="${k}" title="${esc(d.metricMeta[k].desc)}">${esc(d.metricMeta[k].label)}${sortMark(k)}</th>`).join("")}<th>关注点</th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr class="data-row ${state.dataOpen === r.id ? "open" : ""}" data-row="${r.id}">
        <td class="sticky"><b>${esc(r.name)}</b><div class="muted small">${MARKET[r.market]} ${esc(r.ticker)}</div></td>
        ${cols.map((k) => `<td class="${d.metricMeta[k].fmt === "text" ? "" : "num"}">${fmtMetric(k, r)}</td>`).join("")}
        <td>${r.concerns.map((c) => `<span class="badge ${c.kind === "risk" ? "risk" : "chance"}" title="${esc(c.detail)}">${esc(c.label)}</span>`).join(" ") || (r.data_error && !r.metrics.price ? `<span class="muted small">${esc(r.data_error.slice(0, 20))}</span>` : "")}</td>
      </tr>
      ${state.dataOpen === r.id ? `<tr class="detail-row"><td colspan="${cols.length + 2}"><div id="detail-${r.id}" class="company-detail"><span class="muted">加载中…</span></div></td></tr>` : ""}`).join("")}
    </tbody></table></div>`;
  el.querySelectorAll("[data-sort]").forEach((th) => (th.onclick = () => {
    const k = th.dataset.sort;
    state.dataSort = state.dataSort?.key === k ? { key: k, dir: -state.dataSort.dir } : { key: k, dir: k === "name" ? 1 : -1 };
    renderTable();
  }));
  el.querySelectorAll("[data-row]").forEach((tr) => (tr.onclick = () => {
    const id = Number(tr.dataset.row);
    state.dataOpen = state.dataOpen === id ? null : id;
    renderTable();
  }));
  if (state.dataOpen) loadDetail(state.dataOpen);
}

function openCompanyData(id) {
  const r = state.data.rows.find((x) => x.id === id);
  if (!r) return;
  state.dataPool = r.status;
  state.dataMarket = "all";
  state.dataOpen = id;
  renderData();
  document.querySelector(`[data-row="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderClosed(el, rows) {
  el.innerHTML = `<div class="tbl data-tbl"><table>
    <thead><tr><th class="sticky">公司</th><th>结果</th><th>买入价</th><th>卖出价</th><th>收益率</th><th>盈亏额</th><th>持有期</th><th>总结</th></tr></thead>
    <tbody>${rows.map((r) => {
      const t = r.trades[0] || {};
      const summary = t.summary_json ? JSON.parse(t.summary_json) : null;
      return `
      <tr class="data-row ${state.dataOpen === r.id ? "open" : ""}" data-row="${r.id}">
        <td class="sticky"><b>${esc(r.name)}</b><div class="muted small">${MARKET[r.market]} ${esc(r.ticker)}</div></td>
        <td><span class="badge ${t.outcome === "获利了结" ? "bull" : "bear"}">${esc(t.outcome || "")}</span></td>
        <td class="num">${t.cost_price ?? "—"}</td><td class="num">${t.sell_price ?? "—"}</td>
        <td class="num ${signCls(t.pnl_pct)}">${t.pnl_pct != null ? `${t.pnl_pct > 0 ? "+" : ""}${num(t.pnl_pct)}%` : "—"}</td>
        <td class="num ${signCls(t.pnl)}">${t.pnl != null ? money(t.pnl, t.currency) : "—"}</td>
        <td class="num small">${esc(t.opened_at || "?")} ~ ${esc(t.closed_at || "")}</td>
        <td>${summary ? esc(summary.title) : '<span class="muted small">未生成</span>'}</td>
      </tr>
      ${state.dataOpen === r.id ? `<tr class="detail-row"><td colspan="8"><div class="company-detail">${closedDetail(r)}</div></td></tr>` : ""}`;
    }).join("")}</tbody></table></div>`;
  el.querySelectorAll("[data-row]").forEach((tr) => (tr.onclick = (e) => {
    if (e.target.closest(".company-detail")) return;
    const id = Number(tr.dataset.row);
    state.dataOpen = state.dataOpen === id ? null : id;
    renderTable();
  }));
  bindClosedDetail(el);
}

function summaryHtml(s) {
  return `<div class="trade-summary">
    <p class="pf-headline">${esc(s.title)}</p>
    <p><b>经过：</b>${esc(s.story)}</p>
    <p><b>投资逻辑验证：</b>${esc(s.thesis_review)}</p>
    <p><b>决策质量与运气：</b>${esc(s.decision_quality)}</p>
    ${s.lessons?.length ? `<b>经验教训</b><ol>${s.lessons.map((l) => `<li>${esc(l)}</li>`).join("")}</ol>` : ""}
    ${s.next_time ? `<p class="review-q">下次：${esc(s.next_time)}</p>` : ""}</div>`;
}

function closedDetail(r) {
  return r.trades.map((t, i) => {
    const s = t.summary_json ? JSON.parse(t.summary_json) : null;
    return `<div class="trade">
      ${r.trades.length > 1 ? `<div class="muted small">第 ${r.trades.length - i} 笔 · ${esc(t.closed_at)} ${esc(t.outcome)}</div>` : ""}
      ${t.note ? `<p><b>清仓时你写的：</b>${esc(t.note)}</p>` : ""}
      ${s ? summaryHtml(s) : `<button class="small" data-summary="${t.id}">让 AI 写这笔投资的总结</button>`}
    </div>`;
  }).join("") + `<div class="dlg-actions"><button class="small" data-reopen="watching">重新关注</button><button class="small primary" data-reopen="holding">又买回来了</button></div>`;
}

function bindClosedDetail(root) {
  root.querySelectorAll("[data-summary]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    b.textContent = "AI 正在回顾这笔投资…";
    try { await api(`/api/trades/${b.dataset.summary}/summary`, { method: "POST" }); await loadData(); } catch (e) { alert(e.message); b.disabled = false; }
  }));
  root.querySelectorAll("[data-reopen]").forEach((b) => (b.onclick = async () => {
    await api(`/api/companies/${state.dataOpen}/status`, { method: "PUT", json: { status: b.dataset.reopen } });
    state.dataPool = b.dataset.reopen;
    await refresh();
    await loadData();
    pollStatus();
  }));
}

// ---------- 公司详情 ----------
async function loadDetail(id) {
  const r = state.data.rows.find((x) => x.id === id);
  let d;
  try { d = await api(`/api/companies/${id}/data`); } catch (e) { $(`#detail-${id}`).innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  const el = $(`#detail-${id}`);
  if (!el) return;
  const meta = state.data.metricMeta;
  const groups = {};
  Object.values(meta).forEach((m) => { if (m.key !== "industry" && r.metrics[m.key] != null) (groups[m.group] ||= []).push(m); });
  const drops = d.events.filter((e) => e.kind === "drop");
  const analysis = d.analyses[0];
  el.innerHTML = `
    <div class="detail-grid">
      <div>
        ${r.status === "holding" ? positionForm(r) : `<div class="detail-block"><b>观察中</b><p class="muted small">还没买。买入后点下面的按钮移到「持仓中」，再填仓位。</p>
          <div class="actions"><button class="small primary" data-status="holding">已买入，转为持仓</button></div></div>`}
        ${sparkline(d.prices, r)}
        ${r.concerns.length ? `<div class="detail-block"><b>关注点</b><ul class="concerns">${r.concerns.map((c) => `<li><span class="badge ${c.kind === "risk" ? "risk" : "chance"}">${esc(c.label)}</span> ${esc(c.detail)}</li>`).join("")}</ul></div>` : ""}
      </div>
      <div class="detail-block metrics-all">
        ${Object.entries(groups).filter(([g]) => g !== "仓位" || r.status === "holding").map(([g, ms]) => `<div class="mgroup"><div class="muted small">${g}</div>${ms.map((m) => `<div class="mrow" title="${esc(m.desc)}"><span>${esc(m.label)}</span><span class="num">${fmtMetric(m.key, r)}</span></div>`).join("")}</div>`).join("")}
        ${r.data_error ? `<p class="muted small">部分数据没取到：${esc(r.data_error.slice(0, 120))}</p>` : ""}
      </div>
    </div>
    ${drops.length ? `<div class="detail-block"><b>暴跌记录</b>${drops.map((e) => `<div class="drop"><span class="muted small">${e.event_date}</span> ${esc(e.title)}${e.analysis ? `：${esc(e.analysis.summary)}${dropDetail(e)}` : ""}</div>`).join("")}</div>` : ""}
    <div class="detail-block">
      <div class="row-between"><b>财报价值分析${analysis ? ` · ${esc(analysis.period)}` : ""}</b>
        <button class="small ghost" data-report>${analysis ? "↻ 重新分析" : "生成十年财报分析"}</button></div>
      <div id="report-${id}">${analysis ? reportHtml(analysis) : '<p class="muted small">发布新财报后会自动生成：结合近十年数据，看增长、盈利能力与护城河、现金流、财务安全和估值。也可以现在生成。</p>'}</div>
    </div>
    ${d.annual.length ? annualTable(d.annual, d.fin_currency) : ""}`;
  bindDetail(el, r);
}

function positionForm(r) {
  const mode = state.data.prefs.position_mode;
  return `<div class="detail-block">
    <div class="row-between"><b>我的仓位</b><span class="muted small">${mode === "shares" ? "按份额" : "只填权重"} · 可在「⚙ 指标与关注点」切换</span></div>
    <div class="pos-form">
      ${mode === "shares" ? `<label>持有份额（股）<input data-pos="shares" type="number" step="any" min="0" value="${r.shares ?? ""}"></label>`
        : `<label>仓位权重（%）<input data-pos="manual_weight" type="number" step="any" min="0" max="100" value="${r.manual_weight ?? ""}"></label>`}
      <label>买入价（${r.currency}）<input data-pos="cost_price" type="number" step="any" min="0" value="${r.cost_price ?? ""}"></label>
      <label>买入日期<input data-pos="opened_at" type="date" value="${esc(r.opened_at || "")}"></label>
    </div>
    ${r.pnl != null ? `<p class="small">市值 ${money(r.market_value)}，浮动盈亏 <span class="${signCls(r.pnl)}">${money(r.pnl)}（${r.pnl_pct > 0 ? "+" : ""}${num(r.pnl_pct)}%）</span></p>` : ""}
    <div class="actions">
      <button class="small primary" data-save-pos>保存仓位</button>
      <button class="small" data-close-pos>清仓（获利了结 / 止损出局）</button>
      <button class="small ghost" data-status="watching">移到观察中</button>
    </div>
  </div>`;
}

function sparkline(prices, r) {
  if (prices.length < 10) return "";
  const W = 320, H = 70;
  const vals = prices.map((p) => p[1]);
  const cost = r.status === "holding" ? r.cost_price : null;
  const lo = Math.min(...vals, cost ?? Infinity), hi = Math.max(...vals, cost ?? -Infinity);
  const y = (v) => (H - 4 - ((v - lo) / (hi - lo || 1)) * (H - 8)).toFixed(1);
  const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * W).toFixed(1)},${y(v)}`).join(" ");
  const up = vals.at(-1) >= vals[0];
  return `<div class="detail-block"><div class="row-between"><b>近一年走势</b><span class="muted small">${prices[0][0]} ~ ${prices.at(-1)[0]}</span></div>
    <svg viewBox="0 0 ${W} ${H}" class="spark" preserveAspectRatio="none" role="img" aria-label="近一年股价走势">
      ${cost ? `<line x1="0" x2="${W}" y1="${y(cost)}" y2="${y(cost)}" class="cost-line"/>` : ""}
      <polyline points="${pts}" class="${up ? "up" : "down"}"/></svg>
    ${cost ? `<div class="muted small">虚线为你的买入价 ${cost}</div>` : ""}</div>`;
}

function reportHtml(a) {
  const vcls = { 向好: "bull", 稳定: "neutral", 走弱: "bear", 恶化: "bear" }[a.verdict] || "neutral";
  return `<p><span class="badge ${vcls}">${esc(a.verdict)}</span> <b>${esc(a.headline)}</b></p>
    <div class="report-sections">${a.sections.map((s) => `<div class="fw"><b>${esc(s.title)}</b><div>${esc(s.comment)}</div></div>`).join("")}</div>
    ${a.thesis_check ? `<p><b>对你的投资逻辑：</b>${esc(a.thesis_check)}</p>` : ""}
    ${a.watch?.length ? `<p class="small"><b>接下来跟踪：</b>${a.watch.map(esc).join("；")}</p>` : ""}
    ${a.questions?.length ? `<ol class="review-qs">${a.questions.map((q) => `<li class="review-q">${esc(q)}</li>`).join("")}</ol>` : ""}`;
}

function annualTable(annual, ccy) {
  const yi = (v) => (v == null ? "—" : (v / 1e8).toFixed(v >= 1e10 || v <= -1e10 ? 0 : 1));
  const p = (v) => (v == null ? "—" : `${v.toFixed(1)}%`);
  const lines = [["营收（亿）", (r) => yi(r.revenue)], ["营收增速", (r) => p(r.rev_yoy)], ["净利润（亿）", (r) => yi(r.net_profit)],
    ["净利增速", (r) => p(r.np_yoy)], ["ROE", (r) => p(r.roe)], ["毛利率", (r) => p(r.gross_margin)], ["净利率", (r) => p(r.net_margin)],
    ["资产负债率", (r) => p(r.debt_ratio)], ["经营现金流（亿）", (r) => yi(r.ocf)], ["资本开支（亿）", (r) => yi(r.capex)], ["自由现金流（亿）", (r) => yi(r.fcf)]];
  return `<details class="detail-block" open><summary>近 ${annual.length} 年财务（${esc(ccy || "")}）</summary><div class="tbl"><table>
    <thead><tr><th class="sticky"></th>${annual.map((r) => `<th class="num">${r.date.slice(0, 4)}</th>`).join("")}</tr></thead>
    <tbody>${lines.map(([k, f]) => `<tr><td class="sticky">${k}</td>${annual.map((r) => `<td class="num">${f(r)}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div></details>`;
}

function bindDetail(el, r) {
  el.querySelector("[data-save-pos]")?.addEventListener("click", async (e) => {
    const body = { shares: r.shares, cost_price: r.cost_price, manual_weight: r.manual_weight, opened_at: r.opened_at || "" };
    el.querySelectorAll("[data-pos]").forEach((i) => (body[i.dataset.pos] = i.type === "number" ? (i.value === "" ? null : Number(i.value)) : i.value));
    e.target.disabled = true;
    try { await api(`/api/companies/${r.id}/position`, { method: "PUT", json: body }); await loadData(); } catch (err) { alert(err.message); e.target.disabled = false; }
  });
  el.querySelectorAll("[data-status]").forEach((b) => (b.onclick = async () => {
    await api(`/api/companies/${r.id}/status`, { method: "PUT", json: { status: b.dataset.status } });
    state.dataPool = b.dataset.status;
    await refresh();
    await loadData();
  }));
  el.querySelector("[data-close-pos]")?.addEventListener("click", () => openClose(r));
  el.querySelector("[data-report]")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "AI 正在读近十年财报…";
    try {
      const a = await api(`/api/companies/${r.id}/report-analysis`, { method: "POST" });
      $(`#report-${r.id}`).innerHTML = reportHtml(a);
      e.target.textContent = "↻ 重新分析";
    } catch (err) { $(`#report-${r.id}`).innerHTML = `<p class="error">${esc(err.message)}</p>`; e.target.textContent = "重试"; }
    e.target.disabled = false;
  });
}

// ---------- 清仓 ----------
function openClose(r) {
  state.closing = r;
  $("#cl-company").textContent = r.name;
  $("#cl-form").classList.remove("hidden");
  $("#cl-result").classList.add("hidden");
  $("#cl-cost").value = r.cost_price ?? "";
  $("#cl-sell").value = r.metrics.price ?? "";
  $("#cl-opened").value = r.opened_at || "";
  $("#cl-closed").value = new Date().toISOString().slice(0, 10);
  $("#cl-note").value = "";
  $("#cl-error").textContent = "";
  $("#cl-save").disabled = false;
  $("#cl-save").textContent = "清仓并生成总结";
  updateClosePnl();
  $("#dlg-close").showModal();
}
function updateClosePnl() {
  const cost = Number($("#cl-cost").value), sell = Number($("#cl-sell").value), r = state.closing;
  const auto = cost && sell ? (sell >= cost ? "获利了结" : "止损出局") : chipValue($("#cl-outcome")) || "获利了结";
  chips($("#cl-outcome"), ["获利了结", "止损出局"], auto);
  $("#cl-outcome").querySelectorAll(".chip").forEach((b) => b.addEventListener("click", () => {
    if (!$("#cl-outcome").querySelector(".chip.on")) b.classList.add("on");  // 必选
  }));
  $("#cl-pnl").innerHTML = cost && sell ? `收益率 <b class="${signCls(sell - cost)}">${sell >= cost ? "+" : ""}${num((sell / cost - 1) * 100)}%</b>`
    + (r.shares ? `，盈亏 <b class="${signCls(sell - cost)}">${money((sell - cost) * r.shares, r.currency)}</b>（${r.shares} 股）` : "") : "填上买入价和卖出价，自动算盈亏。";
}
["#cl-cost", "#cl-sell"].forEach((s) => ($(s).oninput = updateClosePnl));
$("#cl-save").onclick = async () => {
  const r = state.closing;
  const body = { outcome: chipValue($("#cl-outcome")) || "获利了结", sell_price: Number($("#cl-sell").value) || null,
    cost_price: Number($("#cl-cost").value) || null, opened_at: $("#cl-opened").value, closed_at: $("#cl-closed").value, note: $("#cl-note").value };
  $("#cl-save").disabled = true;
  $("#cl-save").textContent = "AI 正在回顾这笔投资…";
  try {
    const t = await api(`/api/companies/${r.id}/close`, { json: body });
    state.dataPool = "closed";
    state.dataOpen = r.id;
    refresh();
    let html;
    try { html = summaryHtml(await api(`/api/trades/${t.trade_id}/summary`, { method: "POST" })); }
    catch (e) { html = `<p class="error">已清仓，但总结生成失败：${esc(e.message)}。可以稍后在「投过的」里重新生成。</p>`; }
    $("#cl-form").classList.add("hidden");
    $("#cl-result").innerHTML = `${html}<div class="dlg-actions"><button class="primary" data-close>完成</button></div>`;
    $("#cl-result").classList.remove("hidden");
    $("#cl-result [data-close]").onclick = () => $("#dlg-close").close();
    loadData();
  } catch (e) {
    $("#cl-error").textContent = e.message;
    $("#cl-save").disabled = false;
    $("#cl-save").textContent = "清仓并生成总结";
  }
};

// ---------- 指标与关注点设置 ----------
function openPrefs() {
  const d = state.data;
  const p = d.prefs;
  document.querySelectorAll("[name=pmode]").forEach((i) => (i.checked = i.value === p.position_mode));
  const groups = {};
  d.catalog.metrics.forEach((m) => (groups[m.group] ||= []).push(m));
  const on = new Set(p.metrics);
  $("#pf-metrics").innerHTML = Object.entries(groups).map(([g, ms]) => `<div class="pf-group"><div class="muted small">${g}</div>
    <div class="chips">${ms.map((m) => `<button type="button" class="chip ${on.has(m.key) ? "on" : ""}" data-metric="${m.key}" title="${esc(m.desc)}">${esc(m.label)}</button>`).join("")}</div></div>`).join("");
  $("#pf-metrics").querySelectorAll("[data-metric]").forEach((b) => (b.onclick = () => b.classList.toggle("on")));
  $("#pf-concerns").innerHTML = `<table><tbody>${d.catalog.concerns.map((c) => {
    const s = p.concerns[c.key] || {};
    return `<tr><td><label class="check"><input type="checkbox" data-concern="${c.key}" ${s.on ? "checked" : ""}> <span class="badge ${c.kind === "risk" ? "risk" : "chance"}">${esc(c.label)}</span></label></td>
      <td class="muted small">${esc(c.desc)}</td>
      <td class="num">${c.unit ? `<span class="small muted">${esc(c.unit)}</span> <input class="th-input" data-threshold="${c.key}" type="number" step="any" value="${s.threshold ?? c.threshold}">` : ""}</td></tr>`;
  }).join("")}</tbody></table>`;
  $("#pr-reset").classList.toggle("hidden", !d.has_profile);
  $("#pr-error").textContent = "";
  $("#dlg-prefs").showModal();
}
$("#pr-save").onclick = async () => {
  const concerns = {};
  document.querySelectorAll("[data-concern]").forEach((i) => {
    const th = document.querySelector(`[data-threshold="${i.dataset.concern}"]`);
    concerns[i.dataset.concern] = { on: i.checked, threshold: th ? Number(th.value) : 0 };
  });
  // 保持目录里的顺序
  const metrics = [...document.querySelectorAll("[data-metric].on")].map((b) => b.dataset.metric);
  try {
    await api("/api/profile/prefs", { method: "PUT", json: { metrics, concerns, position_mode: document.querySelector("[name=pmode]:checked")?.value || "shares" } });
    $("#dlg-prefs").close();
    state.dataSort = null;
    loadData();
  } catch (e) { $("#pr-error").textContent = e.message; }
};
$("#pr-reset").onclick = async () => {
  await api("/api/profile/apply", { method: "POST" });
  $("#dlg-prefs").close();
  loadData();
};
