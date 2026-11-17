/* 记忆管理页（原生 ES6，无构建）
 *
 * 三条约定：
 * 1. 只走现有 REST，不引入任何前端依赖；
 * 2. 详情里把「环境指纹 / 版本链 / 事件」都摊开，答辩时要能指着说清；
 * 3. 删除后立刻显示残留探测结果，不留「删了但没删干净」的模糊地带。
 */
const API = "/api/memory";
let currentId = null;

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts) {
  const res = await fetch(API + path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function banner(msg, bad) {
  const el = $("#banner");
  if (!msg) { el.style.display = "none"; return; }
  el.textContent = msg;
  el.className = "banner" + (bad ? " bad" : "");
  el.style.display = "block";
}

function statusTag(s) {
  return `<span class="tag ${esc(s)}">${esc(s)}</span>`;
}
function kindTag(k) {
  return `<span class="tag kind">${esc(k)}</span>`;
}
function pct(x) {
  return `${Math.round((Number(x) || 0) * 100)}%`;
}

/* ── 概览 ─────────────────────────────────────────────── */
async function loadStats() {
  try {
    const s = await api("/stats");
    if (!s.ok) { banner(`记忆库不可用：${s.error || "未知原因"}（基础安全不受影响）`, true); return; }
    const env = s.env || {};
    $("#envChips").innerHTML = [
      `${env.os_id || "?"} ${env.os_version || ""} ${env.os_sp || ""}`.trim(),
      env.arch || "?", `kernel ${String(env.kernel || "").slice(0, 24)}`,
    ].map((t) => `<span class="chip">${esc(t)}</span>`).join("");

    const cards = [
      ["记忆总数", s.total],
      ["事件数", s.events],
      ["冲突记录", s.conflicts],
      ["墓碑", s.tombstones],
    ];
    const byKind = s.by_kind || {};
    Object.keys(byKind).forEach((k) => cards.push([`${k}`, byKind[k]]));
    $("#cards").innerHTML = cards.map(([k, v]) => `
      <div class="card"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("");
    banner("");
  } catch (e) {
    banner(`无法连接记忆接口：${e.message}`, true);
  }
}

/* ── 列表 ─────────────────────────────────────────────── */
function renderList(items, title) {
  $("#listTitle").innerHTML = `${esc(title)} <span class="count" id="listCount">${items.length} 条</span>`;
  if (!items.length) {
    $("#listWrap").innerHTML = `<div class="empty">没有符合条件的记忆</div>`;
    return;
  }
  const rows = items.map((m) => `
    <tr data-id="${esc(m.id)}" class="${m.id === currentId ? "active" : ""}">
      <td>
        <div>${esc(m.title)}</div>
        <div class="muted mono">${esc(m.id)}</div>
      </td>
      <td>${kindTag(m.kind)}</td>
      <td>${statusTag(m.status)}</td>
      <td class="score">${pct(m.verify_rate)}<div class="bar"><span style="width:${pct(m.verify_rate)}"></span></div></td>
      <td class="muted">${esc(String(m.last_verified_at || m.updated_at || "").slice(0, 19).replace("T", " "))}</td>
    </tr>`).join("");
  $("#listWrap").innerHTML = `<table>
    <thead><tr><th>标题</th><th>类型</th><th>状态</th><th>成功率</th><th>最近验证</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  document.querySelectorAll("tbody tr").forEach((tr) => {
    tr.addEventListener("click", () => openDetail(tr.dataset.id));
  });
}

async function loadList() {
  const kind = $("#kind").value;
  const status = $("#status").value;
  const qs = new URLSearchParams({ limit: "100" });
  if (kind) qs.set("kind", kind);
  if (status) qs.set("status", status);
  try {
    const r = await api(`/list?${qs}`);
    if (!r.ok) { banner(r.error || "列表加载失败", true); return; }
    renderList(r.items || [], "记忆列表");
  } catch (e) {
    banner(`列表加载失败：${e.message}`, true);
  }
}

/* ── 检索 ─────────────────────────────────────────────── */
async function doSearch() {
  const q = $("#query").value.trim();
  if (!q) { loadList(); return; }
  try {
    const r = await api(`/search?q=${encodeURIComponent(q)}&k=10`);
    if (!r.ok) { banner(r.error || "检索失败", true); return; }
    const items = (r.candidates || []).map((c) => ({
      id: c.memory_id,
      title: c.title,
      kind: c.kind,
      status: c.status,
      verify_rate: c.success,
      last_verified_at: "",
      _cand: c,
    }));
    renderList(items, `检索结果：${q}`);
    const notes = [];
    if (r.excluded && r.excluded.length) {
      notes.push(`已前置排除 ${r.excluded.length} 条：` +
        r.excluded.map((e) => `${e.title}（${e.reason}）`).join("；"));
    }
    if (r.hint) notes.push(r.hint);
    notes.push(`耗时 ${Number(r.took_ms || 0).toFixed(1)} ms，扫描 ${r.scanned} 条`);
    $("#detail").innerHTML = `<h3>本次检索说明</h3>
      <div class="field"><div class="lab">查询</div><div class="val">${esc(r.query)}</div></div>
      ${(r.candidates || []).map((c, i) => `
        <div class="field">
          <div class="lab">#${i + 1} ${esc(c.title)} · 总分 ${Number(c.score).toFixed(3)}</div>
          <div class="kv">语义 ${Number(c.sim).toFixed(2)} · 环境 ${Number(c.env_score).toFixed(2)}
 · 成功率 ${Number(c.success).toFixed(2)} · 新鲜度 ${Number(c.freshness).toFixed(2)}
惩罚 ${JSON.stringify(c.penalties || {})}</div>
        </div>`).join("")}
      <div class="hint">${esc(notes.filter(Boolean).join("\n"))}</div>`;
  } catch (e) {
    banner(`检索失败：${e.message}`, true);
  }
}

/* ── 详情 ─────────────────────────────────────────────── */
async function openDetail(id) {
  currentId = id;
  document.querySelectorAll("tbody tr").forEach((tr) => {
    tr.classList.toggle("active", tr.dataset.id === id);
  });
  try {
    const r = await api(`/${encodeURIComponent(id)}`);
    if (!r.ok) { $("#detail").innerHTML = `<div class="empty">${esc(r.reason || "读取失败")}</div>`; return; }
    const m = r.memory;
    const env = r.env;
    const versions = (r.versions || []).map((v) =>
      `<div class="muted">v${v.version} · ${esc(String(v.changed_at).slice(0, 19).replace("T", " "))} · ${esc(v.change_note || "")}</div>`
    ).join("") || `<div class="muted">无版本记录</div>`;
    const events = (r.events || []).slice(0, 12).map((e) =>
      `<div class="muted mono">${esc(String(e.ts).slice(0, 19).replace("T", " "))} ${esc(e.event)} ${esc(JSON.stringify(e.detail || {}))}</div>`
    ).join("") || `<div class="muted">无事件</div>`;
    const conflicts = (r.conflicts || []).map((c) =>
      `<div class="muted">与 <span class="mono">${esc(c.memory_a === id ? c.memory_b : c.memory_a)}</span>
       维度 ${esc(c.dimension)} · ${esc(c.resolution || "未裁决")}</div>`
    ).join("") || `<div class="muted">无冲突记录</div>`;

    $("#detail").innerHTML = `
      <h3>${esc(m.title)}</h3>
      <div class="muted mono">${esc(m.id)} · v${m.version}</div>
      <div class="row">${kindTag(m.kind)}${statusTag(m.status)}</div>

      <div class="field"><div class="lab">故障现象</div><div class="val">${esc(m.symptom)}</div></div>
      <div class="field"><div class="lab">诊断</div><div class="val">${esc(m.diagnosis || "—")}</div></div>
      <div class="field"><div class="lab">参考方案</div><div class="val">${esc(m.solution || "—")}</div></div>
      <div class="field"><div class="lab">历史验证</div><div class="val">
        成功 ${m.success_count} / 失败 ${m.fail_count}（成功率 ${pct(m.verify_rate)}）·
        最近验证 ${esc(String(m.last_verified_at || "—").slice(0, 19).replace("T", " "))}</div></div>

      <div class="field"><div class="lab">环境指纹</div><div class="kv">${esc(env ? JSON.stringify(env, null, 1) : "未记录")}</div></div>
      <div class="field"><div class="lab">来源（provenance）</div><div class="kv">${esc(JSON.stringify(m.provenance || {}, null, 1))}</div></div>
      <div class="field"><div class="lab">版本链</div><div class="kv">${versions}</div></div>
      <div class="field"><div class="lab">事件</div><div class="kv">${events}</div></div>
      <div class="field"><div class="lab">冲突</div><div class="kv">${conflicts}</div></div>

      <div class="row">
        <button data-act="verify-ok">验证通过</button>
        <button data-act="verify-fail">验证失败</button>
        <button data-act="stale">标记过期</button>
        <button data-act="active">恢复可用</button>
        <button class="danger" data-act="delete">删除（可验证）</button>
      </div>`;

    $("#detail").querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => act(btn.dataset.act, id));
    });
  } catch (e) {
    $("#detail").innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

async function act(kind, id) {
  try {
    if (kind === "verify-ok" || kind === "verify-fail") {
      await api(`/${encodeURIComponent(id)}/verify`, {
        method: "POST",
        body: JSON.stringify({ ok: kind === "verify-ok" }),
      });
    } else if (kind === "stale" || kind === "active") {
      await api(`/${encodeURIComponent(id)}/status`, {
        method: "POST",
        body: JSON.stringify({
          status: kind === "stale" ? "stale" : "active",
          note: kind === "stale" ? "人工标记为过期" : "人工恢复可用",
        }),
      });
    } else if (kind === "delete") {
      if (!confirm("删除后不可召回。是否继续？（会保留墓碑与事件作为删除证据）")) return;
      const r = await api(`/${encodeURIComponent(id)}?scope=all`, { method: "DELETE" });
      const residual = r.residual ? JSON.stringify(r.residual) : "";
      banner(r.clean
        ? `已删除，残留探测：${residual}（存储与索引均无残留）`
        : `删除未完成，残留：${residual}`, !r.clean);
      currentId = null;
      $("#detail").innerHTML = `<div class="empty">已删除，选择其他记忆查看</div>`;
      await loadStats();
      await loadList();
      return;
    }
    await loadStats();
    await loadList();
    await openDetail(id);
  } catch (e) {
    banner(`操作失败：${e.message}`, true);
  }
}

/* ── 巡检 ─────────────────────────────────────────────── */
async function sweep() {
  try {
    const r = await api("/sweep", { method: "POST" });
    if (!r.ok) { banner(r.error || "巡检失败", true); return; }
    banner(`巡检完成：扫描 ${r.scanned} 条，时间触发 ${(r.time || []).length}，环境触发 ${(r.env || []).length}`);
    await loadStats();
    await loadList();
  } catch (e) {
    banner(`巡检失败：${e.message}`, true);
  }
}

$("#btnSearch").addEventListener("click", doSearch);
$("#query").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
$("#btnList").addEventListener("click", loadList);
$("#btnSweep").addEventListener("click", sweep);
$("#btnRefresh").addEventListener("click", () => { loadStats(); loadList(); });
$("#kind").addEventListener("change", loadList);
$("#status").addEventListener("change", loadList);

loadStats();
loadList();
