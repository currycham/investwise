// ---------- 我的画像 ----------
// 依赖 app.js 里的 $、api、esc、state

async function loadProfile() {
  const el = $("#tab-profile");
  if (!state.profileData) el.innerHTML = '<p class="muted">加载中…</p>';
  try { state.profileData = await api("/api/profile"); } catch (e) { el.innerHTML = `<p class="error">${esc(e.message)}</p>`; return; }
  renderProfile();
}

function renderProfile() {
  const d = state.profileData;
  const p = d.profile;
  const selected = new Set(d.tags);
  const metricLabel = Object.fromEntries(d.catalog.metrics.map((m) => [m.key, m.label]));
  const concernLabel = Object.fromEntries(d.catalog.concerns.map((c) => [c.key, c.label]));
  const h = d.history;
  const list = (title, items, cls = "") => items?.length ? `<div class="pf-block ${cls}"><b>${title}</b><ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";

  $("#tab-profile").innerHTML = `
    <section class="card pf-card">
      <div class="row-between"><h2>AI 眼中的你</h2>
        ${p ? `<span class="muted small">更新于 ${esc((d.profile_at || "").slice(0, 10))}</span>` : ""}</div>
      ${p ? `
        <p class="pf-headline">${esc(p.headline)}</p>
        <div class="pf-styles">${p.styles.map((s) => `
          <div class="pf-style"><span>${esc(s.name)}</span><div class="bar"><i style="width:${Math.round(s.weight * 100)}%"></i></div><span class="muted small">${Math.round(s.weight * 100)}%</span></div>`).join("")}</div>
        <div class="pf-grid">
          ${list("你的投资理念", p.philosophy)}
          ${list("从你的行为里看到的", p.evidence)}
          ${list("说的和做的，不太一致的地方", p.gaps, "pf-gaps")}
          ${list("这种风格做得好的地方", p.strengths)}
          ${list("要小心的陷阱", p.pitfalls)}
          ${list("值得想清楚的问题", p.questions, "pf-questions")}
        </div>
        <p class="muted small">基于：${h.companies} 家公司、${h.theses} 份投资逻辑、${h.decisions} 条决策、${h.judgments} 次先判断、${h.closed} 笔清仓、${h.chats} 条对话。用得越多，画像越准；隔一段时间可以重新分析，看看自己变了没有。</p>`
      : `<p>投资是一面镜子。先选几个你认同的标签、用自己的话说说你的投资理念，AI 会结合你的持仓、投资逻辑、决策记录和对话，帮你总结投资风格——尤其是<b>"你以为的自己"和"你实际怎么做"</b>不一致的地方。</p>
         <p class="muted small">画像还会决定「资讯」的解读角度，以及「数据」页默认显示哪些指标、监控哪些关注点。</p>`}
    </section>

    <section class="card">
      <h2>你认同哪些说法？<span class="muted small">可多选</span></h2>
      ${d.catalog.tags.map((g) => `
        <div class="pf-group"><div class="muted small">${esc(g.group)}</div>
          <div class="chips">${g.tags.map((t) => `<button type="button" class="chip ${selected.has(t) ? "on" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("")}</div>
        </div>`).join("")}
      <label>用自己的话说说你的投资理念（可选）
        <textarea id="pf-statement" rows="3" placeholder="例如：只买看得懂的好生意，在价格合理时买入并长期持有；宁可错过，不愿看错。">${esc(d.statement)}</textarea>
      </label>
      <p id="pf-error" class="error"></p>
      <div class="dlg-actions">
        <button id="pf-save">只保存标签</button>
        <button id="pf-analyze" class="primary">${p ? "保存并重新分析" : "保存并生成画像"}</button>
      </div>
    </section>

    ${p ? `
    <section class="card">
      <h2>画像怎么用</h2>
      <p><b>资讯：</b>${esc(p.news_focus.replace(/[。.]?$/, "。"))}每条资讯会多一句"从你的风格看"，并按相关度排序；重大事件不会因为和画像无关而被过滤。</p>
      <p><b>数据页推荐指标：</b>${p.metrics.map((k) => `<span class="tag">${esc(metricLabel[k] || k)}</span>`).join(" ")}</p>
      <p><b>推荐监控的关注点：</b>${p.concerns.map((k) => `<span class="tag">${esc(concernLabel[k] || k)}</span>`).join(" ")}</p>
      <div class="row-between">
        <span class="muted small">${d.prefs.custom ? "你在数据页自己调整过指标，重新分析不会覆盖。" : "数据页当前使用画像推荐的设置。"}</span>
        ${d.prefs.custom ? '<button id="pf-apply" class="small">恢复为画像推荐</button>' : '<button class="small" data-goto="data">去数据页看看</button>'}
      </div>
    </section>` : ""}`;

  const root = $("#tab-profile");
  root.querySelectorAll("[data-tag]").forEach((b) => (b.onclick = () => b.classList.toggle("on")));
  root.querySelectorAll("[data-goto]").forEach((b) => (b.onclick = () => showTab(b.dataset.goto)));
  const saveTags = () => api("/api/profile/tags", { method: "PUT", json: {
    tags: [...root.querySelectorAll("[data-tag].on")].map((b) => b.dataset.tag), statement: $("#pf-statement").value } });
  $("#pf-save").onclick = async () => {
    try { await saveTags(); await loadProfile(); } catch (e) { $("#pf-error").textContent = e.message; }
  };
  $("#pf-analyze").onclick = async () => {
    const b = $("#pf-analyze");
    b.disabled = true;
    b.textContent = "AI 正在读你的历史记录…";
    $("#pf-error").textContent = "";
    try {
      await saveTags();
      await api("/api/profile/analyze", { method: "POST" });
      await loadProfile();
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      $("#pf-error").textContent = e.message;
      b.disabled = false;
      b.textContent = "重试";
    }
  };
  $("#pf-apply")?.addEventListener("click", async () => { await api("/api/profile/apply", { method: "POST" }); loadProfile(); });
}
