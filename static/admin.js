const $ = (s) => document.querySelector(s);
const MARKET = { A: "A股", HK: "港股", US: "美股" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yuan = (v) => `¥${Number(v || 0).toFixed(2)}`;
const num = (v) => Number(v || 0).toLocaleString();

async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (res.status === 401 || res.status === 404 && path.startsWith("/api/admin/overview")) { location.href = "/"; throw new Error("无权限"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败 ${res.status}`);
  return data;
}

function flash(msg, isError = false) {
  const el = $("#flash");
  el.textContent = msg;
  el.className = `flash${isError ? " error" : ""}`;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => el.classList.add("hidden"), 6000);
}

const table = (head, rows) =>
  `<div class="tbl"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
   <tbody>${rows.join("") || `<tr><td colspan="${head.length}" class="muted">暂无数据</td></tr>`}</tbody></table></div>`;

function queueLine(q) {
  if (q.running) return `正在执行：<b>${esc(q.label)}</b>（${esc(q.progress)}）${q.pending.length ? `，另有 ${q.pending.length} 个排队` : ""}`;
  return q.pending.length ? `排队中：${q.pending.map(esc).join("、")}` : "空闲";
}

// ---------- 概览 ----------
async function viewOverview() {
  const d = await api("/api/admin/overview");
  return `
    <div class="card">
      <div class="row-between"><b>API 余额</b><span class="muted small">每 10 分钟最多查询一次</span></div>
      ${table(["服务", "用途", "余额", ""], d.balances.map((b) => `
        <tr><td>${esc(b.name)}</td><td class="muted small">${esc(b.use)}</td>
          <td class="num ${b.error || b.balance < 10 ? "bad" : ""}">${b.error ? `查询失败 <span class="small">${esc(b.error)}</span>` : yuan(b.balance)}</td>
          <td><a href="${esc(b.console)}" target="_blank" rel="noopener" class="small">去充值 →</a></td></tr>`))}
    </div>
    <div class="stats">${d.stats.map(([k, v]) => `<div class="stat"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("")}</div>
    <div class="card"><b>检索队列</b>：${queueLine(d.queue)}</div>
    <div class="two-col">
      <div>${table(["近 7 天按环节", "调用", "输入 tokens", "输出 tokens", "成本"], d.by_task.map((t) => `
        <tr><td>${esc(t.name)}</td><td class="num">${num(t.calls)}</td><td class="num">${num(t.input)}</td>
          <td class="num">${num(t.output)}</td><td class="num">${yuan(t.cost)}</td></tr>`))}</div>
      <div>${table(["日期（北京时间）", "调用", "成本"], d.by_day.map((r) => `
        <tr><td class="num">${r.day}</td><td class="num">${num(r.calls)}</td><td class="num">${yuan(r.cost)}</td></tr>`))}</div>
    </div>
    <p class="muted small">成本按 .env 中 DEEPSEEK_PRICE_* 单价估算，实际以 DeepSeek 控制台账单为准。</p>`;
}

// ---------- 用户 ----------
async function viewUsers() {
  const rows = await api("/api/admin/users");
  return `
    ${table(["用户", "角色", "持仓", "提问", "近 30 天成本", "累计成本", "注册", "最近登录", "操作"], rows.map((u) => `
      <tr class="${u.banned ? "dim" : ""}">
        <td><b>${esc(u.username)}</b> <span class="muted small">#${u.id}</span>${u.banned ? ' <span class="badge bear">已停用</span>' : ""}</td>
        <td>${u.role === "admin" ? `<span class="badge bull">管理员</span>${u.pinned_admin ? ' <span class="muted small" title="在 .env 的 ADMIN_USERNAMES 中">固定</span>' : ""}` : '<span class="muted">用户</span>'}</td>
        <td class="num">${u.companies}</td>
        <td class="num">${u.questions}</td>
        <td class="num">${yuan(u.cost_30d)}</td>
        <td class="num">${yuan(u.cost_total)}</td>
        <td class="num small">${esc(u.created_at)}</td>
        <td class="num small">${esc(u.last_login_at || "")}</td>
        <td class="actions">
          <button class="small" data-act="role" data-id="${u.id}" data-role="${u.role === "admin" ? "user" : "admin"}">${u.role === "admin" ? "取消管理员" : "设为管理员"}</button>
          <button class="small" data-act="reset" data-id="${u.id}" data-name="${esc(u.username)}">重置密码</button>
          <button class="small ${u.banned ? "" : "danger"}" data-act="ban" data-id="${u.id}" data-banned="${u.banned ? 0 : 1}" data-name="${esc(u.username)}">${u.banned ? "恢复" : "停用"}</button>
        </td>
      </tr>`))}
    <p class="muted small">停用会立即踢下线，数据保留；重置密码会生成一个临时密码，请私下发给对方。</p>`;
}

const userActions = {
  async role(b) { await api(`/api/admin/users/${b.dataset.id}/role`, { json: { role: b.dataset.role } }); flash("角色已更新"); },
  async ban(b) {
    const banning = b.dataset.banned === "1";
    if (banning && !confirm(`停用「${b.dataset.name}」？对方会被立即踢下线。`)) return false;
    await api(`/api/admin/users/${b.dataset.id}/ban`, { json: { banned: banning } });
    flash(banning ? "已停用" : "已恢复");
  },
  async reset(b) {
    if (!confirm(`重置「${b.dataset.name}」的密码？对方当前的登录会失效。`)) return false;
    const r = await api(`/api/admin/users/${b.dataset.id}/reset-password`, { method: "POST" });
    prompt(`「${b.dataset.name}」的临时密码（只显示这一次，请复制后私下发给对方）：`, r.password);
  },
};

// ---------- 任务 ----------
async function viewJobs() {
  const d = await api("/api/admin/jobs");
  const STATUS = { running: "进行中", done: "完成", failed: "失败" };
  return `
    <div class="card row-between">
      <span><b>检索队列</b>：${queueLine(d.queue)}</span>
      <span class="actions">
        <button data-act="digests" title="用当天已有信息重新生成所有用户的今日概览">重新生成今日概览</button>
        <button class="primary" data-act="runall">立即全站检索</button>
      </span>
    </div>
    ${table(["#", "类型", "用户", "公司数", "状态", "新增信息", "开始", "用时", "错误"], d.jobs.map((j) => `
      <tr>
        <td class="num">${j.id}</td><td>${esc(j.label)}</td><td>${esc(j.username || "全站")}</td>
        <td class="num">${j.company_count}</td>
        <td><span class="badge ${j.status === "failed" ? "bear" : j.status === "running" ? "neutral" : "bull"}">${STATUS[j.status] || j.status}</span>${j.status === "running" ? ` <span class="small muted">${esc(j.progress)}</span>` : ""}</td>
        <td class="num">${j.items_added}</td>
        <td class="num small">${esc(j.started_at)}</td>
        <td class="num">${j.seconds != null ? (j.seconds >= 60 ? `${Math.round(j.seconds / 60)} 分` : `${j.seconds} 秒`) : ""}</td>
        <td class="small">${j.errors.length ? `<details><summary class="bad">${j.errors.length} 个</summary>${j.errors.map(esc).join("<br>")}</details>` : ""}</td>
      </tr>`))}`;
}

const jobActions = {
  async runall() {
    if (!confirm("立即对所有用户的全部持仓检索一次（回看 36 小时）？")) return false;
    await api("/api/admin/run-all", { method: "POST" }); flash("已加入队列");
  },
  async digests() { await api("/api/admin/digests", { method: "POST" }); flash("正在后台重新生成，稍后刷新查看"); },
};

// ---------- 持仓公司 ----------
async function viewCompanies() {
  const rows = await api("/api/admin/companies");
  return `
    ${table(["公司", "市场", "持有人数", "近 7 天抓取", "累计抓取", "近 7 天重要信息", "最近抓取", ""], rows.map((c) => `
      <tr class="${c.news_7d ? "" : "warn-row"}">
        <td><b>${esc(c.name)}</b> <span class="muted small">${esc(c.ticker)}</span></td>
        <td>${MARKET[c.market]}</td>
        <td class="num">${c.holders}</td>
        <td class="num">${c.news_7d}</td>
        <td class="num">${c.news_total}</td>
        <td class="num">${c.items_7d}</td>
        <td class="num small">${esc(c.last_fetched || "从未")}</td>
        <td><button class="small" data-act="backfill" data-market="${c.market}" data-ticker="${esc(c.ticker)}">回溯 7 天</button></td>
      </tr>`))}
    <p class="muted small">黄色行：近 7 天一条新闻都没抓到，可能是代码有误或数据源覆盖不到（港股依赖博查搜索）。回溯只会分析之前没分析过的新闻。</p>`;
}

const companyActions = {
  async backfill(b) {
    const r = await api("/api/admin/companies/backfill", { json: { market: b.dataset.market, ticker: b.dataset.ticker } });
    flash(`已加入队列（涉及 ${r.companies} 个持仓）`);
  },
};

// ---------- 路由 ----------
const VIEWS = { overview: [viewOverview, {}], users: [viewUsers, userActions], jobs: [viewJobs, jobActions], companies: [viewCompanies, companyActions] };

async function render() {
  const tab = VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  document.querySelectorAll("[data-tab]").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  const [view, actions] = VIEWS[tab];
  try {
    $("#view").innerHTML = await view();
  } catch (e) { $("#view").innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  $("#view").querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try {
      if ((await actions[b.dataset.act](b)) !== false) render();
    } catch (e) { flash(e.message, true); }
    b.disabled = false;
  }));
}

window.addEventListener("hashchange", render);
render();
