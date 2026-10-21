const $ = (s) => document.querySelector(s);
const MARKET = { A: "A股", HK: "港股", US: "美股" };
const state = { me: null, companies: [], open: new Set(), chatCompany: null, editing: null, newsPool: "all" };
const POOL = { holding: "持仓中", watching: "观察中", closed: "投过的" };

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith("/api/login")) { showAuth(); throw new Error("未登录"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail?.[0]?.msg || data.detail || `请求失败 ${res.status}`);
  return data;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dirClass = (d) => (d === "利好" ? "bull" : d === "利空" ? "bear" : "neutral");
const fmtScore = (s) => (s > 0 ? "+" : "") + Number(s).toFixed(0);

// ---------- 首页与使用说明 ----------
document.querySelectorAll(".guide-slot").forEach((el) => el.append($("#tpl-guide").content.cloneNode(true)));
$("#btn-guide").onclick = () => {
  const show = $("#guide").classList.toggle("hidden") === false;
  if (show) $("#guide").scrollIntoView({ behavior: "smooth", block: "start" });
};
$("#guide-close").onclick = () => $("#guide").classList.add("hidden");
document.querySelectorAll("[data-open]").forEach((b) => (b.onclick = () => $(b.dataset.open === "import" ? "#btn-import" : "#btn-add").click()));

// ---------- 登录 ----------
let registering = false;
function showAuth() { $("#auth").classList.remove("hidden"); $("#app").classList.add("hidden"); }
$("#auth-toggle").onclick = (e) => {
  e.preventDefault();
  registering = !registering;
  $("#auth-invite").classList.toggle("hidden", !registering || !inviteRequired);
  $("#auth-submit").textContent = $("#auth-title").textContent = registering ? "注册" : "登录";
  $("#auth-toggle").textContent = registering ? "已有账号？登录" : `没有账号？注册${inviteRequired ? "（需要邀请码）" : ""}`;
};
$("#auth-submit").onclick = async () => {
  $("#auth-error").textContent = "";
  try {
    await api(registering ? "/api/register" : "/api/login", {
      json: { username: $("#auth-user").value.trim(), password: $("#auth-pass").value, invite_code: $("#auth-invite").value.trim() },
    });
    boot();
  } catch (e) { $("#auth-error").textContent = e.message; }
};
let inviteRequired = true;
api("/api/public-config").then((c) => {
  inviteRequired = c.invite_required;
  if (!inviteRequired) $("#auth-toggle").textContent = "没有账号？注册";
}).catch(() => {});
$("#auth-pass").onkeydown = (e) => { if (e.key === "Enter") $("#auth-submit").click(); };
$("#btn-logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showAuth(); };

// ---------- 标签页：我的画像 / 资讯 / 数据 ----------
const TABS = ["profile", "news", "data"];
function showTab(name) {
  if (!TABS.includes(name)) name = "news";
  state.tab = name;
  TABS.forEach((t) => $(`#tab-${t}`).classList.toggle("hidden", t !== name));
  document.querySelectorAll("#tabs [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "profile") loadProfile();
  if (name === "data") loadData();
}
document.querySelectorAll("#tabs [data-tab]").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------- 渲染 ----------
async function boot() {
  try { state.me = await api("/api/me"); } catch { return; }
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#btn-admin").classList.toggle("hidden", !state.me.is_admin);
  await refresh();
  showTab(location.hash.slice(1) || "news");
  pollStatus();
}

async function refresh() {
  const [companies, alerts, digest, due] = await Promise.all([
    api("/api/companies"), api("/api/alerts"), api("/api/digest"), api("/api/decisions/due")]);
  state.companies = companies;
  if (state.tab === "data") loadData();
  renderReviews(due);
  renderDigest(digest);
  renderAlerts(alerts);
  renderCompanies();
}

function renderDigest(d) {
  const el = $("#digest");
  el.classList.toggle("hidden", !d.company_total);
  if (!d.company_total) return;
  const head = `<div class="digest-head"><h2>今日概览</h2><span class="muted small">${d.date}${d.updated_at ? ` · ${d.updated_at} 更新` : ""}</span></div>`;
  if (d.empty) {
    el.innerHTML = head + `<p class="muted">今日无重要变化。${d.last_info_date ? `上次有重要信息是 ${d.last_info_date}。` : ""}</p>`;
    return;
  }
  const pending = (id) => state.companies.find((c) => c.id === id)?.pending_judgments || 0;
  const anyPending = d.companies.some((c) => pending(c.company_id));
  const stanceClass = (s) => (s === "偏利好" ? "bull" : s === "偏利空" ? "bear" : "neutral");
  const thesisClass = (t) => (t === "加强" ? "bull" : t ? "bear" : "");
  el.innerHTML = head + `
    ${anyPending ? `<p class="digest-overview">有新信息等你先判断。判断完之后，这里会显示 AI 的总览和组合层面的问题。</p>`
                 : `<p class="digest-overview">${esc(d.overview)}</p>`}
    <div class="digest-rows">${d.companies.map((c) => pending(c.company_id) ? `
      <div class="digest-row" data-company="${c.company_id}">
        <span class="alert-company">${esc(c.name)}</span>
        <span class="digest-badges"><span class="tag judge">待你判断 ${pending(c.company_id)}</span></span>
        <span class="muted">点击查看信息，先给出你的判断</span>
      </div>` : `
      <div class="digest-row" data-company="${c.company_id}">
        <span class="alert-company">${esc(c.name)}</span>
        <span class="digest-badges">
          <span class="badge ${stanceClass(c.stance)}">${esc(c.stance)} ${fmtScore(c.score)}</span>
          ${c.alerts ? `<span class="tag alert">预警 ${c.alerts}</span>` : ""}
        </span>
        <span>${esc(c.summary)}
          ${c.thesis_effect ? `<span class="badge ${thesisClass(c.thesis_effect)}">投资逻辑：${esc(c.thesis_effect)}</span>` : ""}
          ${c.dates.length > 1 || c.dates[0] !== d.date ? `<span class="muted small">（${c.dates.length > 1 ? `${c.dates[0]} ~ ${c.dates.at(-1)}` : c.dates[0]}，共 ${c.count} 条）</span>` : ""}
        </span>
      </div>`).join("")}</div>
    ${d.reflections.length && !anyPending ? `<div class="reflections"><b>值得想一想</b><ol>${d.reflections.map((r) => `<li>${esc(r)}</li>`).join("")}</ol></div>` : ""}`;
  el.querySelectorAll(".digest-row").forEach((row) => (row.onclick = () => focusCompany(Number(row.dataset.company))));
}

function focusCompany(id) {
  state.open.add(id);
  renderCompanies();
  document.getElementById(`company-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderAlerts(alerts) {
  $("#alerts").classList.toggle("hidden", !alerts.length);
  $("#alerts-list").innerHTML = alerts.map((a) => `
    <div class="alert-row" data-company="${a.company_id}">
      <span class="alert-company">${esc(a.company_name)}</span>
      <span>${a.needs_judgment ? '<span class="tag judge">待你判断</span>'
        : `<span class="badge ${dirClass(a.direction)}">${esc(a.direction)} ${fmtScore(a.score)}</span>`}
        ${a.thesis_broken ? '<span class="badge bear">逻辑被证伪</span>' : ""}</span>
      <span class="alert-title">${esc(a.title)}</span>
      <span class="muted small">${esc(a.report_date)}</span>
    </div>`).join("");
  document.querySelectorAll(".alert-row").forEach((el) => (el.onclick = () => focusCompany(Number(el.dataset.company))));
}

function renderCompanies() {
  // 资讯只跟踪持仓中和观察中的公司；清仓的公司停止跟踪，在「数据 → 投过的」里查看
  const tracked = state.companies.filter((c) => c.status !== "closed");
  const list = tracked.filter((c) => state.newsPool === "all" || c.status === state.newsPool);
  $("#company-count").textContent = tracked.length ? `(${tracked.length})` : "";
  const counts = { all: tracked.length, holding: 0, watching: 0 };
  tracked.forEach((c) => counts[c.status]++);
  $("#news-pool").innerHTML = counts.watching ? [["all", "全部"], ["holding", "持仓中"], ["watching", "观察中"]]
    .map(([k, v]) => `<button class="chip ${state.newsPool === k ? "on" : ""}" data-pool="${k}">${v} ${counts[k]}</button>`).join("") : "";
  $("#news-pool").querySelectorAll("[data-pool]").forEach((b) => (b.onclick = () => { state.newsPool = b.dataset.pool; renderCompanies(); }));
  // 还没有持仓：显示欢迎和使用说明；加入公司后说明自动收起，可从顶栏「使用说明」再打开
  const empty = state.companies.length === 0;
  $("#welcome").classList.toggle("hidden", !empty);
  $("#welcome-name").textContent = state.me.username;
  if (empty) $("#guide").classList.remove("hidden");
  else if (renderCompanies.wasEmpty) $("#guide").classList.add("hidden");
  renderCompanies.wasEmpty = empty;
  $(".companies").classList.toggle("hidden", empty);
  $("#company-list").innerHTML = list.map((c) => `
    <div class="company ${c.alerts_7d ? "has-alert" : ""}" id="company-${c.id}">
      <div class="company-head" data-toggle="${c.id}">
        <span class="company-name">${esc(c.name)}</span>
        <span class="tag">${MARKET[c.market]} ${esc(c.ticker)}</span>
        ${c.status === "watching" ? '<span class="tag">观察中</span>' : ""}
        ${c.alerts_7d ? `<span class="tag alert">预警 ${c.alerts_7d}</span>` : ""}
        ${c.pending_judgments ? `<span class="tag judge">待你判断 ${c.pending_judgments}</span>` : ""}
        ${c.last_report ? `<span class="muted small">最近更新 ${c.last_report}</span>` : '<span class="muted small">暂无重要信息</span>'}
        <span class="company-meta">
          <button class="small" data-chat="${c.id}">💬 提问我</button>
          <button class="small" data-decide="${c.id}">📝 记录决策</button>
          <button class="ghost small" data-edit="${c.id}">编辑</button>
          <button class="ghost small" data-del="${c.id}">删除</button>
        </span>
      </div>
      <div class="company-body ${state.open.has(c.id) ? "" : "hidden"}" id="body-${c.id}">
        ${thesisBlock(c)}
        ${c.decisions ? `<details class="decisions"><summary>我的决策记录（${c.decisions}）</summary><div id="dec-${c.id}"></div></details>` : ""}
        <div class="timeline" id="tl-${c.id}"><span class="muted">加载中…</span></div>
      </div>
    </div>`).join("");

  list.forEach((c) => state.open.has(c.id) && loadTimeline(c.id));
  document.querySelectorAll("details.decisions").forEach((el) => (el.ontoggle = () => el.open && loadDecisions(Number(el.querySelector("[id^=dec-]").id.slice(4)))));
  document.querySelectorAll("[data-decide]").forEach((el) => (el.onclick = () => openDecision(Number(el.dataset.decide))));
  document.querySelectorAll("[data-toggle]").forEach((el) => (el.onclick = (e) => {
    if (e.target.closest("button")) return;
    const id = Number(el.dataset.toggle);
    const body = $(`#body-${id}`);
    const show = body.classList.contains("hidden");
    body.classList.toggle("hidden", !show);
    show ? (state.open.add(id), loadTimeline(id)) : state.open.delete(id);
  }));
  document.querySelectorAll("[data-chat]").forEach((el) => (el.onclick = () => openChat(Number(el.dataset.chat))));
  document.querySelectorAll("[data-edit]").forEach((el) => (el.onclick = () => openCompanyDialog(Number(el.dataset.edit))));
  document.querySelectorAll("[data-del]").forEach((el) => (el.onclick = async () => {
    const c = state.companies.find((x) => x.id === Number(el.dataset.del));
    if (!confirm(`确定删除「${c.name}」？其历史信息和对话也会一并删除。`)) return;
    await api(`/api/companies/${c.id}`, { method: "DELETE" });
    refresh();
  }));
}

function thesisBlock(c) {
  const rows = [["为什么买", c.thesis], ["关键假设", c.assumptions], ["什么情况说明我错了", c.wrong_if], ["计划持有", c.horizon]]
    .filter(([, v]) => v);
  if (!rows.length) return `<div class="thesis muted">还没写投资逻辑。点「编辑」写下为什么买、关键假设、什么情况说明你错了，AI 才能判断新信息是在加强还是削弱你的判断。</div>`;
  return `<div class="thesis">${rows.map(([k, v]) => `<div><b>${k}：</b><span class="pre">${esc(v)}</span></div>`).join("")}
    ${c.wrong_if ? "" : '<div class="muted small">还没写"什么情况说明我错了"——这可能是最值得想清楚的一条。</div>'}</div>`;
}

async function loadTimeline(id) {
  const days = await api(`/api/companies/${id}/timeline`);
  const el = $(`#tl-${id}`);
  if (!el) return;
  el.innerHTML = days.length ? days.map((d) => `
    <div class="day">
      <div class="day-head">${d.date}<span class="day-summary">${d.items.some((i) => i.needs_judgment) ? "" : esc(d.day_summary)}</span></div>
      ${d.items.map(renderItem).join("")}
    </div>`).join("") : '<span class="muted">近90天没有重要信息。</span>';
  el.querySelectorAll("[data-judge]").forEach((b) => (b.onclick = async () => {
    el.querySelectorAll(`[data-judge="${b.dataset.judge}"]`).forEach((x) => (x.disabled = true));
    try {
      await api(`/api/items/${b.dataset.judge}/judge`, { json: { verdict: b.dataset.v } });
      await refresh();
    } catch (e) { alert(e.message); }
  }));
}

const sourcesList = (it) => it.sources?.length ? `<ol class="sources">${it.sources.map((s) => `
  <li>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}
    <span class="muted">${esc(s.source)} ${esc(s.published_at)}</span></li>`).join("")}</ol>` : "";

function renderJudge(it) {
  return `
    <div class="item judge-card">
      <div class="item-head"><span class="tag judge">先判断</span><span class="item-title">${esc(it.title)}</span></div>
      <p>${esc(it.summary)}</p>
      ${sourcesList(it)}
      <div class="judge-ask">
        <span>你怎么看？对这家公司的长期价值是：</span>
        ${["利好", "利空", "中性"].map((v) => `<button class="small" data-judge="${it.id}" data-v="${v}">${v}</button>`).join("")}
      </div>
    </div>`;
}

function renderItem(it) {
  if (it.needs_judgment) return renderJudge(it);
  const cls = dirClass(it.direction);
  const mine = it.my_verdict ? `<span class="badge ${it.my_verdict === it.direction ? dirClass(it.my_verdict) : "differ"}"
    title="${it.my_verdict === it.direction ? "和 AI 判断一致" : "和 AI 判断不同，值得想想谁漏了什么"}">你的判断：${esc(it.my_verdict)}${it.my_verdict === it.direction ? " ✓" : " ≠ AI"}</span>` : "";
  const fws = Object.entries(it.frameworks || {}).filter(([, v]) => v && v.score !== null && v.score !== undefined);
  return `
    <div class="item ${it.is_alert ? "is-alert" : ""}">
      <div class="item-head">
        ${it.is_alert ? '<span class="badge" style="color:var(--alert)">⚠ 预警</span>' : ""}
        <span class="item-title">${esc(it.title)}</span>
        <span class="badge ${cls}">${esc(it.direction)}</span>
        <span class="score ${cls}">${fmtScore(it.score)}</span>
        <span class="muted small">置信度 ${Math.round(it.confidence * 100)}% · ${esc(it.horizon)}</span>
        ${mine}
      </div>
      <p>${esc(it.summary)}</p>
      ${it.profile_note ? `<p class="profile-note"><span class="tag">从你的风格看</span> ${esc(it.profile_note)}</p>` : ""}
      ${it.thesis_impact && it.thesis_impact !== "未提供" && it.thesis_impact !== "无关"
        ? `<p><span class="badge ${it.thesis_impact === "加强" ? "bull" : "bear"}">投资逻辑：${esc(it.thesis_impact)}${it.thesis_broken ? "（核心前提被证伪）" : ""}</span>
            ${it.thesis_note ? `<span class="muted small">${esc(it.thesis_note)}</span>` : ""}</p>` : ""}
      <details>
        <summary>AI 判断依据</summary>
        <p>${esc(it.reasoning)}</p>
        ${fws.length ? `<div class="frameworks">${fws.map(([k, v]) => `
          <div class="fw"><b>${esc(k)}</b><span class="score ${v.score > 0 ? "bull" : v.score < 0 ? "bear" : ""}">${fmtScore(v.score)}</span>
          <div>${esc(v.comment)}</div></div>`).join("")}</div>` : ""}
        ${it.watch_points?.length ? `<p class="small"><b>接下来跟踪：</b>${it.watch_points.map(esc).join("；")}</p>` : ""}
      </details>
      ${sourcesList(it)}
    </div>`;
}

// ---------- 检索任务 ----------
$("#btn-run").onclick = async () => {
  try { await api("/api/run", { method: "POST" }); pollStatus(); } catch (e) { alert(e.message); }
};
let pollTimer = null;
async function pollStatus() {
  clearTimeout(pollTimer);
  const s = await api("/api/run/status").catch(() => null);
  if (!s) return;
  const active = s.running || s.pending > 0;
  $("#btn-run").disabled = active;
  $("#run-status").textContent = s.running ? `${s.label}中：${s.progress}${s.pending ? `（另有 ${s.pending} 个排队）` : ""}`
    : s.pending ? "检索任务排队中…"
    : s.finished_at ? `上次检索 ${s.finished_at.replace("T", " ")}${s.errors.length ? `（${s.errors.length} 个错误）` : ""}` : "";
  $("#run-status").title = s.errors.join("\n");
  if (active) {
    // 每个任务完成后刷新一次页面数据
    if (state.lastFinished && s.finished_at !== state.lastFinished) refresh();
    state.lastFinished = s.finished_at;
    pollTimer = setTimeout(pollStatus, 2000);
    state.wasRunning = true;
  } else if (state.wasRunning) { state.wasRunning = false; refresh(); }
}

// ---------- 导入 ----------
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
$("#btn-import").onclick = () => {
  $("#import-step1").classList.remove("hidden");
  $("#import-step2").classList.add("hidden");
  $("#import-error").textContent = "";
  $("#dlg-import").showModal();
};
$("#import-parse").onclick = async () => {
  const fd = new FormData();
  fd.append("text", $("#import-text").value);
  if ($("#import-file").files[0]) fd.append("file", $("#import-file").files[0]);
  $("#import-parse").disabled = true;
  $("#import-parse").textContent = "识别中…";
  try {
    const r = await api("/api/import/parse", { method: "POST", body: fd });
    $("#import-preview").innerHTML = r.companies.map((c) => `
      <tr>
        <td><input type="checkbox" checked></td>
        <td><input value="${esc(c.name)}"></td>
        <td><input value="${esc(c.ticker)}"></td>
        <td><select>${Object.entries(MARKET).map(([k, v]) => `<option value="${k}" ${k === c.market ? "selected" : ""}>${v}</option>`).join("")}</select></td>
        <td class="muted small">${esc(c.note)}</td>
      </tr>`).join("") || '<tr><td colspan="5" class="muted">没有识别到公司</td></tr>';
    $("#import-uncertain").innerHTML = r.uncertain.length
      ? "未能确定：" + r.uncertain.map((u) => `${esc(u.raw)}（${esc(u.reason)}）`).join("；") + "。可稍后用「添加单个」补充。" : "";
    $("#import-step1").classList.add("hidden");
    $("#import-step2").classList.remove("hidden");
  } catch (e) { $("#import-error").textContent = e.message; }
  $("#import-parse").disabled = false;
  $("#import-parse").textContent = "识别";
};
$("#import-back").onclick = () => {
  $("#import-step1").classList.remove("hidden");
  $("#import-step2").classList.add("hidden");
};
$("#import-confirm").onclick = async () => {
  const companies = [...$("#import-preview").querySelectorAll("tr")]
    .filter((tr) => tr.querySelector("input[type=checkbox]")?.checked)
    .map((tr) => {
      const [name, ticker] = tr.querySelectorAll("input:not([type=checkbox])");
      return { name: name.value, ticker: ticker.value, market: tr.querySelector("select").value };
    });
  try {
    const r = await api("/api/import/confirm", { json: { companies } });
    $("#dlg-import").close();
    $("#import-text").value = "";
    $("#import-file").value = "";
    await refresh();
    if (r.added) pollStatus();
  } catch (e) { $("#import-error").textContent = e.message; }
};

// ---------- 添加/编辑 ----------
$("#btn-add").onclick = () => openCompanyDialog(null);
function openCompanyDialog(id) {
  const c = id ? state.companies.find((x) => x.id === id) : null;
  state.editing = c;
  $("#company-dlg-title").textContent = c ? "编辑公司" : "添加公司";
  $("#cf-name").value = c?.name || "";
  $("#cf-ticker").value = c?.ticker || "";
  $("#cf-market").value = c?.market || "";
  $("#cf-thesis").value = c?.thesis || "";
  $("#cf-assumptions").value = c?.assumptions || "";
  $("#cf-wrongif").value = c?.wrong_if || "";
  $("#cf-horizon").value = c?.horizon || "";
  $("#cf-status").value = "holding";
  $("#cf-status-wrap").classList.toggle("hidden", !!c);  // 已有公司的池子在「数据」页里调整
  $("#cf-review-out").classList.add("hidden");
  $("#cf-replace-hint").textContent = c ? "提示：如果修改了代码（换成另一家公司），该公司的历史信息和对话会被清空。" : "";
  $("#cf-error").textContent = "";
  $("#dlg-company").showModal();
}
$("#cf-save").onclick = async () => {
  const body = { name: $("#cf-name").value, ticker: $("#cf-ticker").value, market: $("#cf-market").value || null,
    status: $("#cf-status").value, ...thesisForm() };
  try {
    if (state.editing) await api(`/api/companies/${state.editing.id}`, { method: "PUT", json: body });
    else await api("/api/companies", { json: body });
    $("#dlg-company").close();
    await refresh();
    pollStatus();
  } catch (e) { $("#cf-error").textContent = e.message; }
};

const thesisForm = () => ({ thesis: $("#cf-thesis").value, assumptions: $("#cf-assumptions").value,
  wrong_if: $("#cf-wrongif").value, horizon: $("#cf-horizon").value });
$("#cf-review").onclick = async () => {
  const out = $("#cf-review-out");
  $("#cf-review").disabled = true;
  $("#cf-review").textContent = "体检中…";
  out.classList.remove("hidden");
  out.innerHTML = '<span class="muted">AI 正在读你的投资逻辑…</span>';
  try {
    const r = await api("/api/thesis/review", { json: { name: $("#cf-name").value, ticker: $("#cf-ticker").value, ...thesisForm() } });
    out.innerHTML = `<p><b>${esc(r.overall)}</b></p>${r.issues.length ? `<ol>${r.issues.map((i) => `
      <li><span class="tag">${esc(i.field)}</span> ${esc(i.problem)}<div class="review-q">→ ${esc(i.question)}</div></li>`).join("")}</ol>`
      : '<p class="muted">没发现明显的问题。</p>'}<p class="muted small">按这些问题修改后可以再体检一次。</p>`;
  } catch (e) { out.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
  $("#cf-review").disabled = false;
  $("#cf-review").textContent = "投资逻辑体检";
};

// ---------- 设置 ----------
$("#btn-settings").onclick = () => {
  $("#th-range").value = state.me.alert_threshold;
  $("#th-val").textContent = state.me.alert_threshold;
  $("#jf-on").checked = state.me.judge_first;
  $("#em-addr").value = state.me.email || "";
  $("#em-on").checked = state.me.email_alerts;
  $("#em-error").textContent = "";
  const off = !state.me.email_enabled;
  ["#em-addr", "#em-on", "#em-test"].forEach((s) => ($(s).disabled = off));
  if (off) $("#em-error").textContent = "站点还没配置发信邮箱，暂时不能用邮件预警。";
  $("#dlg-settings").showModal();
};
$("#em-test").onclick = async () => {
  $("#em-error").textContent = "";
  $("#em-test").disabled = true;
  try {
    await api("/api/settings/test-email", { json: { email: $("#em-addr").value.trim() } });
    $("#em-error").textContent = "已发送，请查收（可能在垃圾邮件里）。";
  } catch (e) { $("#em-error").textContent = e.message; }
  $("#em-test").disabled = false;
};
$("#th-range").oninput = () => ($("#th-val").textContent = $("#th-range").value);
$("#th-save").onclick = async () => {
  const body = { alert_threshold: Number($("#th-range").value), email: $("#em-addr").value.trim(),
    email_alerts: $("#em-on").checked, judge_first: $("#jf-on").checked };
  try {
    await api("/api/settings", { method: "PUT", json: body });
  } catch (e) { $("#em-error").textContent = /pattern|string/i.test(e.message) ? "邮箱格式不对" : e.message; return; }
  Object.assign(state.me, body);
  $("#dlg-settings").close();
  refresh();
};

// ---------- 决策日志 ----------
const ACTIONS = ["买入", "加仓", "减仓", "卖出", "继续持有", "观望"];
const EMOTIONS = ["平静", "兴奋", "焦虑", "恐惧", "怕错过", "后悔", "不确定"];
const actionClass = (a) => (["买入", "加仓"].includes(a) ? "bull" : ["减仓", "卖出"].includes(a) ? "bear" : "neutral");

function chips(el, options, selected) {
  el.innerHTML = options.map((o) => `<button type="button" class="chip ${o === selected ? "on" : ""}" data-v="${o}">${o}</button>`).join("");
  el.querySelectorAll(".chip").forEach((b) => (b.onclick = () => {
    const was = b.classList.contains("on");
    el.querySelectorAll(".chip").forEach((x) => x.classList.remove("on"));
    if (!was || el.id === "dc-action") b.classList.add("on");  // 情绪可以取消选择，动作必选
  }));
}
const chipValue = (el) => el.querySelector(".chip.on")?.dataset.v || "";

function openDecision(id) {
  state.deciding = state.companies.find((c) => c.id === id);
  $("#dc-company").textContent = state.deciding.name;
  chips($("#dc-action"), ACTIONS, "继续持有");
  chips($("#dc-emotion"), EMOTIONS, "");
  $("#dc-reason").value = "";
  $("#dc-price").value = "";
  $("#dc-conf").value = 3;
  $("#dc-conf-val").textContent = 3;
  $("#dc-review").value = "90";
  $("#dc-error").textContent = "";
  $("#dlg-decision").showModal();
}
$("#dc-conf").oninput = () => ($("#dc-conf-val").textContent = $("#dc-conf").value);
$("#dc-save").onclick = async () => {
  const body = { action: chipValue($("#dc-action")), reason: $("#dc-reason").value.trim(), emotion: chipValue($("#dc-emotion")),
    confidence: Number($("#dc-conf").value), price: $("#dc-price").value.trim(), review_days: Number($("#dc-review").value) };
  if (body.reason.length < 2) { $("#dc-error").textContent = "写下这次决策的理由"; return; }
  try {
    await api(`/api/companies/${state.deciding.id}/decisions`, { json: body });
    $("#dlg-decision").close();
    state.open.add(state.deciding.id);
    await refresh();
  } catch (e) { $("#dc-error").textContent = e.message; }
};

function renderDecision(d, withCompany = false) {
  return `
    <div class="decision">
      <div class="decision-head">
        <span class="muted small">${d.decided_on}</span>
        ${withCompany ? `<b>${esc(d.company_name)}</b>` : ""}
        <span class="badge ${actionClass(d.action)}">${esc(d.action)}</span>
        <span class="muted small">把握 ${d.confidence}/5${d.emotion ? ` · ${esc(d.emotion)}` : ""}${d.price ? ` · 价格 ${esc(d.price)}` : ""}</span>
        <button class="ghost small" data-del-decision="${d.id}" title="删除">✕</button>
      </div>
      <div class="pre">${esc(d.reason)}</div>
      ${d.reviewed_at ? `<div class="review-note"><b>复盘（${d.reviewed_at.slice(0, 10)}）：</b><span class="pre">${esc(d.review_note)}</span></div>`
        : `<div class="muted small">${d.due ? "已到复盘时间，见页面顶部" : `将于 ${d.review_on} 提醒复盘`}</div>`}
    </div>`;
}
function bindDecisionDeletes(root) {
  root.querySelectorAll("[data-del-decision]").forEach((b) => (b.onclick = async () => {
    if (!confirm("删除这条决策记录？")) return;
    await api(`/api/decisions/${b.dataset.delDecision}`, { method: "DELETE" });
    await refresh();
    if ($("#dlg-thinking").open) openThinking();
  }));
}
async function loadDecisions(companyId) {
  const el = $(`#dec-${companyId}`);
  const rows = await api(`/api/decisions?company_id=${companyId}`);
  if (!el) return;
  el.innerHTML = rows.map((d) => renderDecision(d)).join("");
  bindDecisionDeletes(el);
}

// 到期复盘
function renderReviews(due) {
  const el = $("#reviews");
  el.classList.toggle("hidden", !due.length);
  el.innerHTML = due.length ? `<h2>🔁 到了复盘的时候</h2>` + due.map((d) => {
    const s = d.since;
    return `
    <div class="review-card" data-review="${d.id}">
      <div class="decision-head">
        <b>${esc(d.company_name)}</b><span class="muted small">${d.decided_on}</span>
        <span class="badge ${actionClass(d.action)}">${esc(d.action)}</span>
        <span class="muted small">把握 ${d.confidence}/5${d.emotion ? ` · 当时${esc(d.emotion)}` : ""}${d.price ? ` · 价格 ${esc(d.price)}` : ""}</span>
      </div>
      <div class="review-then"><b>当时的理由：</b><span class="pre">${esc(d.reason)}</span></div>
      <div class="review-since"><b>之后发生了什么：</b>${s.count
        ? `${s.count} 条重要信息，净倾向 <span class="score ${s.net > 0 ? "bull" : s.net < 0 ? "bear" : ""}">${fmtScore(s.net)}</span>；
           加强你的投资逻辑 ${s.strengthened} 条，削弱 ${s.weakened} 条。
           <ul>${s.top.map((i) => `<li><span class="muted small">${i.report_date}</span> <span class="badge ${dirClass(i.direction)}">${esc(i.direction)} ${fmtScore(i.score)}</span> ${esc(i.title)}</li>`).join("")}</ul>`
        : "这段时间没有检索到重要信息。"}</div>
      <div class="review-qs"></div>
      <textarea rows="3" placeholder="回头看：当时的理由哪些成立、哪些没有？情绪和把握程度合适吗？结果是判断对了，还是运气？下次会怎么做？"></textarea>
      <div class="dlg-actions">
        <button class="small" data-act="questions">让 AI 提几个复盘问题</button>
        <button class="small ghost" data-act="snooze">一个月后再说</button>
        <button class="small primary" data-act="save">保存复盘</button>
      </div>
    </div>`;
  }).join("") : "";
  el.querySelectorAll(".review-card").forEach((card) => {
    const id = card.dataset.review;
    card.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try {
        if (b.dataset.act === "questions") {
          b.textContent = "思考中…";
          const r = await api(`/api/decisions/${id}/questions`, { method: "POST" });
          card.querySelector(".review-qs").innerHTML = `<ol>${r.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ol>`;
          b.remove();
          return;
        }
        const body = b.dataset.act === "snooze" ? { snooze_days: 30 } : { note: card.querySelector("textarea").value };
        await api(`/api/decisions/${id}/review`, { method: "PUT", json: body });
        await refresh();
      } catch (e) { alert(e.message); }
      b.disabled = false;
    }));
  });
}

// ---------- 我的思考 ----------
$("#btn-thinking").onclick = () => openThinking();
async function openThinking() {
  if (!$("#dlg-thinking").open) $("#dlg-thinking").showModal();
  const [j, decisions] = await Promise.all([api("/api/thinking/judgments"), api("/api/decisions")]);
  const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "—");
  const V = ["利好", "利空", "中性"];
  const reviewed = decisions.filter((d) => d.reviewed_at).length;
  $("#thinking-body").innerHTML = `
    <h4>先判断，再看 AI</h4>
    ${j.total ? `
      <div class="stats">
        <div class="stat"><div class="v">${j.total}</div><div class="k">判断过的信息</div></div>
        <div class="stat"><div class="v">${pct(j.agree, j.total)}</div><div class="k">和 AI 一致</div></div>
        <div class="stat"><div class="v">${j.opposite}</div><div class="k">方向完全相反</div></div>
      </div>
      <div class="tbl"><table><thead><tr><th>你 \\ AI</th>${V.map((v) => `<th>${v}</th>`).join("")}</tr></thead>
        <tbody>${V.map((m) => `<tr><td><b>${m}</b></td>${V.map((a) => `<td class="num ${m === a ? "" : j.matrix[m][a] ? "differ-cell" : ""}">${j.matrix[m][a]}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
      <p class="muted small">看看自己偏向哪一边：比如你常把 AI 认为中性的信息判断为利好，可能是对持仓有偏爱；常把利空看成中性，可能在回避坏消息。一致不代表对，不一致也不代表错——值得回看的是理由。</p>
      ${j.disagreements.length ? `<details open><summary>最近和 AI 判断不同的信息</summary>${j.disagreements.map((r) => `
        <div class="disagree">
          <div><span class="muted small">${r.report_date} · ${esc(r.company_name)}</span> ${esc(r.title)}</div>
          <div><span class="badge differ">你：${esc(r.verdict)}</span> <span class="badge ${dirClass(r.direction)}">AI：${esc(r.direction)} ${fmtScore(r.score)}</span></div>
          <div class="muted small">AI 的理由：${esc(r.reasoning)}</div>
        </div>`).join("")}</details>` : ""}`
      : `<p class="muted">还没有判断记录。在 ⚙ 设置里开启「先判断，再看 AI」，新信息会先隐藏 AI 的结论，让你自己判断。</p>`}
    <h4>决策日志</h4>
    ${decisions.length ? `<p class="muted small">共 ${decisions.length} 条，已复盘 ${reviewed} 条。</p>
      <div id="thinking-decisions">${decisions.map((d) => renderDecision(d, true)).join("")}</div>`
      : `<p class="muted">还没有决策记录。买入、卖出，或者决定不动的时候，点公司旁的「📝 记录决策」写下当时的理由。到期后会提醒你复盘。</p>`}`;
  bindDecisionDeletes($("#thinking-body"));
}

// ---------- 提问助手 ----------
async function openChat(id) {
  const c = state.companies.find((x) => x.id === id);
  state.chatCompany = c;
  $("#chat-title").textContent = `${c.name} · 投资思考`;
  $("#chat").classList.remove("hidden");
  const history = await api(`/api/companies/${id}/chat`);
  renderChat(history);
  if (!history.length) sendChat("我想认真评估一下这家公司是否值得投资，请开始向我提问。", true);
}
function renderChat(msgs) {
  $("#chat-body").innerHTML = msgs.map((m) => `<div class="msg ${m.role}">${esc(m.content)}</div>`).join("");
  $("#chat-body").scrollTop = $("#chat-body").scrollHeight;
}
function appendMsg(role, text, pending = false) {
  const div = document.createElement("div");
  div.className = `msg ${role}${pending ? " pending" : ""}`;
  div.textContent = text;
  $("#chat-body").appendChild(div);
  $("#chat-body").scrollTop = $("#chat-body").scrollHeight;
  return div;
}
async function sendChat(text, silent = false) {
  const c = state.chatCompany;
  if (!silent) appendMsg("user", text);
  const pending = appendMsg("assistant", "思考中…", true);
  try {
    const r = await api(`/api/companies/${c.id}/chat`, { json: { message: text } });
    pending.classList.remove("pending");
    pending.textContent = r.reply;
  } catch (e) { pending.textContent = e.message.includes("预算") ? e.message : `出错了：${e.message}`; }
  $("#chat-body").scrollTop = $("#chat-body").scrollHeight;
}
$("#chat-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#chat-input").value.trim();
  if (!text) return;
  $("#chat-input").value = "";
  sendChat(text);
};
$("#chat-input").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
};
$("#chat-close").onclick = () => $("#chat").classList.add("hidden");
$("#chat-reset").onclick = async () => {
  if (!confirm("清空这家公司的对话，重新开始？")) return;
  await api(`/api/companies/${state.chatCompany.id}/chat`, { method: "DELETE" });
  openChat(state.chatCompany.id);
};

// 等 profile.js、data.js 都加载完再启动
window.addEventListener("DOMContentLoaded", () => boot().catch(showAuth));
