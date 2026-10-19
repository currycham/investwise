const $ = (s) => document.querySelector(s);
const MARKET = { A: "A股", HK: "港股", US: "美股" };
const state = { me: null, companies: [], open: new Set(), chatCompany: null, editing: null };

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

// ---------- 登录 ----------
let registering = false;
function showAuth() { $("#auth").classList.remove("hidden"); $("#app").classList.add("hidden"); }
$("#auth-toggle").onclick = (e) => {
  e.preventDefault();
  registering = !registering;
  $("#auth-invite").classList.toggle("hidden", !registering);
  $("#auth-submit").textContent = registering ? "注册" : "登录";
  $("#auth-toggle").textContent = registering ? "已有账号？登录" : "没有账号？注册";
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
$("#auth-pass").onkeydown = (e) => { if (e.key === "Enter") $("#auth-submit").click(); };
$("#btn-logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showAuth(); };

// ---------- 渲染 ----------
async function boot() {
  try { state.me = await api("/api/me"); } catch { return; }
  $("#auth").classList.add("hidden");
  $("#app").classList.remove("hidden");
  await refresh();
  pollStatus();
}

async function refresh() {
  const [companies, alerts, digest] = await Promise.all([api("/api/companies"), api("/api/alerts"), api("/api/digest")]);
  state.companies = companies;
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
  const stanceClass = (s) => (s === "偏利好" ? "bull" : s === "偏利空" ? "bear" : "neutral");
  const thesisClass = (t) => (t === "加强" ? "bull" : t ? "bear" : "");
  el.innerHTML = head + `
    <p class="digest-overview">${esc(d.overview)}</p>
    <div class="digest-rows">${d.companies.map((c) => `
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
    ${d.reflections.length ? `<div class="reflections"><b>值得想一想</b><ol>${d.reflections.map((r) => `<li>${esc(r)}</li>`).join("")}</ol></div>` : ""}`;
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
      <span><span class="badge ${dirClass(a.direction)}">${esc(a.direction)} ${fmtScore(a.score)}</span>
        ${a.thesis_broken ? '<span class="badge bear">逻辑被证伪</span>' : ""}</span>
      <span class="alert-title">${esc(a.title)}</span>
      <span class="muted small">${esc(a.report_date)}</span>
    </div>`).join("");
  document.querySelectorAll(".alert-row").forEach((el) => (el.onclick = () => focusCompany(Number(el.dataset.company))));
}

function renderCompanies() {
  const list = state.companies;
  $("#company-count").textContent = list.length ? `(${list.length})` : "";
  $("#empty-hint").classList.toggle("hidden", list.length > 0);
  $("#company-list").innerHTML = list.map((c) => `
    <div class="company ${c.alerts_7d ? "has-alert" : ""}" id="company-${c.id}">
      <div class="company-head" data-toggle="${c.id}">
        <span class="company-name">${esc(c.name)}</span>
        <span class="tag">${MARKET[c.market]} ${esc(c.ticker)}</span>
        ${c.alerts_7d ? `<span class="tag alert">预警 ${c.alerts_7d}</span>` : ""}
        ${c.last_report ? `<span class="muted small">最近更新 ${c.last_report}</span>` : '<span class="muted small">暂无重要信息</span>'}
        <span class="company-meta">
          <button class="small" data-chat="${c.id}">💬 提问我</button>
          <button class="ghost small" data-edit="${c.id}">编辑</button>
          <button class="ghost small" data-del="${c.id}">删除</button>
        </span>
      </div>
      <div class="company-body ${state.open.has(c.id) ? "" : "hidden"}" id="body-${c.id}">
        ${c.thesis ? `<div class="thesis"><b>我的投资逻辑：</b>${esc(c.thesis)}</div>`
                   : `<div class="thesis muted">还没写投资逻辑。写下"为什么买它"，AI 才能判断新信息是在加强还是削弱你的判断。</div>`}
        <div class="timeline" id="tl-${c.id}"><span class="muted">加载中…</span></div>
      </div>
    </div>`).join("");

  list.forEach((c) => state.open.has(c.id) && loadTimeline(c.id));
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

async function loadTimeline(id) {
  const days = await api(`/api/companies/${id}/timeline`);
  const el = $(`#tl-${id}`);
  if (!el) return;
  el.innerHTML = days.length ? days.map((d) => `
    <div class="day">
      <div class="day-head">${d.date}<span class="day-summary">${esc(d.day_summary)}</span></div>
      ${d.items.map(renderItem).join("")}
    </div>`).join("") : '<span class="muted">近90天没有重要信息。</span>';
}

function renderItem(it) {
  const cls = dirClass(it.direction);
  const fws = Object.entries(it.frameworks || {}).filter(([, v]) => v && v.score !== null && v.score !== undefined);
  return `
    <div class="item ${it.is_alert ? "is-alert" : ""}">
      <div class="item-head">
        ${it.is_alert ? '<span class="badge" style="color:var(--alert)">⚠ 预警</span>' : ""}
        <span class="item-title">${esc(it.title)}</span>
        <span class="badge ${cls}">${esc(it.direction)}</span>
        <span class="score ${cls}">${fmtScore(it.score)}</span>
        <span class="muted small">置信度 ${Math.round(it.confidence * 100)}% · ${esc(it.horizon)}</span>
      </div>
      <p>${esc(it.summary)}</p>
      ${it.thesis_impact && it.thesis_impact !== "未提供" && it.thesis_impact !== "无关"
        ? `<p><span class="badge ${it.thesis_impact === "加强" ? "bull" : "bear"}">投资逻辑：${esc(it.thesis_impact)}${it.thesis_broken ? "（核心前提被证伪）" : ""}</span></p>` : ""}
      <details>
        <summary>AI 判断依据</summary>
        <p>${esc(it.reasoning)}</p>
        ${fws.length ? `<div class="frameworks">${fws.map(([k, v]) => `
          <div class="fw"><b>${esc(k)}</b><span class="score ${v.score > 0 ? "bull" : v.score < 0 ? "bear" : ""}">${fmtScore(v.score)}</span>
          <div>${esc(v.comment)}</div></div>`).join("")}</div>` : ""}
        ${it.watch_points?.length ? `<p class="small"><b>接下来跟踪：</b>${it.watch_points.map(esc).join("；")}</p>` : ""}
      </details>
      ${it.sources?.length ? `<ol class="sources">${it.sources.map((s) => `
        <li>${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}
          <span class="muted">${esc(s.source)} ${esc(s.published_at)}</span></li>`).join("")}</ol>` : ""}
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
  $("#cf-replace-hint").textContent = c ? "提示：如果修改了代码（换成另一家公司），该公司的历史信息和对话会被清空。" : "";
  $("#cf-error").textContent = "";
  $("#dlg-company").showModal();
}
$("#cf-save").onclick = async () => {
  const body = { name: $("#cf-name").value, ticker: $("#cf-ticker").value, market: $("#cf-market").value || null, thesis: $("#cf-thesis").value };
  try {
    if (state.editing) await api(`/api/companies/${state.editing.id}`, { method: "PUT", json: body });
    else await api("/api/companies", { json: body });
    $("#dlg-company").close();
    await refresh();
    pollStatus();
  } catch (e) { $("#cf-error").textContent = e.message; }
};

// ---------- 设置 ----------
$("#btn-settings").onclick = () => {
  $("#th-range").value = state.me.alert_threshold;
  $("#th-val").textContent = state.me.alert_threshold;
  $("#dlg-settings").showModal();
};
$("#th-range").oninput = () => ($("#th-val").textContent = $("#th-range").value);
$("#th-save").onclick = async () => {
  const v = Number($("#th-range").value);
  await api("/api/settings", { method: "PUT", json: { alert_threshold: v } });
  state.me.alert_threshold = v;
  $("#dlg-settings").close();
  refresh();
};

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
  } catch (e) { pending.textContent = `出错了：${e.message}`; }
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

boot().catch(showAuth);
