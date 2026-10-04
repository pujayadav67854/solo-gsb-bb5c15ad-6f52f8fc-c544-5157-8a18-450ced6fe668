/* 事故复盘时间线 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  reviews: [],
  current: null,      // 服务器返回的复盘快照 {review, events, relations, computation}
  editEvents: [],     // 编辑中的事件 [{key, id?, name, earliest_start, latest_start, duration_minutes, note}]
  editRelations: [],  // 编辑中的关系 [{predecessor_key, successor_key, min_gap_minutes, max_gap_minutes}]
  seq: 0,
};

function newKey() { return "k" + (++state.seq) + "_" + Date.now(); }

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.error || ("HTTP " + resp.status));
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function showBanner(html) {
  const b = $("#banner");
  b.innerHTML = html;
  b.classList.remove("hidden");
}
function hideBanner() { $("#banner").classList.add("hidden"); }

/* ---------------------------------------------------------------- 加载 */

async function loadReviews(selectId) {
  const data = await api("GET", "/api/reviews");
  state.reviews = data.reviews;
  const sel = $("#review-select");
  sel.innerHTML = "";
  for (const r of state.reviews) {
    const opt = document.createElement("option");
    opt.value = r.id;
    opt.textContent = `#${r.id} ${r.title}（v${r.version}${r.status === "finalized" ? "·已定稿" : ""}）`;
    sel.appendChild(opt);
  }
  if (selectId) sel.value = selectId;
  $("#empty").classList.toggle("hidden", state.reviews.length > 0);
}

async function openReview(id) {
  const bundle = await api("GET", "/api/reviews/" + id);
  state.current = bundle;
  resetEdits();
  hideBanner();
  render();
  loadVersions().catch((err) => showBanner("⚠ 版本列表加载失败：" + escapeHtml(err.message)));
}

function resetEdits() {
  const b = state.current;
  state.editEvents = b.events.map((e) => ({
    key: "id_" + e.id,
    id: e.id,
    name: e.name,
    earliest_start: e.earliest_start || "",
    latest_start: e.latest_start || "",
    duration_minutes: e.duration_minutes === null ? "" : e.duration_minutes,
    note: e.note || "",
  }));
  const idToKey = {};
  for (const e of state.editEvents) if (e.id) idToKey[e.id] = e.key;
  state.editRelations = b.relations.map((r) => ({
    predecessor_key: idToKey[r.predecessor_id],
    successor_key: idToKey[r.successor_id],
    min_gap_minutes: r.min_gap_minutes === null || r.min_gap_minutes === undefined ? 0 : r.min_gap_minutes,
    max_gap_minutes: r.max_gap_minutes === null || r.max_gap_minutes === undefined ? "" : r.max_gap_minutes,
  })).filter((r) => r.predecessor_key && r.successor_key);
}

/* ---------------------------------------------------------------- 渲染 */

function eventName(key) {
  const e = state.editEvents.find((x) => x.key === key);
  return e ? e.name : "?";
}

function render() {
  const b = state.current;
  if (!b) return;
  $("#main").classList.remove("hidden");
  $("#empty").classList.add("hidden");
  $("#review-title").value = b.review.title;

  const comp = b.computation;
  const vb = $("#version-badge");
  vb.textContent = "版本 v" + b.review.version;
  const sb = $("#status-badge");
  if (b.review.status === "finalized") {
    sb.textContent = "已定稿"; sb.className = "badge ok";
  } else if (comp && comp.status === "conflict") {
    sb.textContent = "存在冲突·不得定稿"; sb.className = "badge err";
  } else if (comp && comp.result.drafts.length) {
    sb.textContent = "草稿·有暂存事件"; sb.className = "badge warn";
  } else {
    sb.textContent = "草稿"; sb.className = "badge";
  }
  $("#btn-finalize").disabled = b.review.status === "finalized";

  renderEventsTable();
  renderRelations();
  renderResults();
}

function renderEventsTable() {
  const tbody = $("#events-table tbody");
  tbody.innerHTML = "";
  for (const e of state.editEvents) {
    const tr = document.createElement("tr");
    tr.dataset.key = e.key;
    tr.innerHTML =
      `<td><input data-f="name" type="text" value=""></td>` +
      `<td><input data-f="earliest_start" type="datetime-local"></td>` +
      `<td><input data-f="latest_start" type="datetime-local"></td>` +
      `<td><input data-f="duration_minutes" type="number" min="0" style="width:80px"></td>` +
      `<td><input data-f="note" type="text"></td>` +
      `<td><button class="row-del" title="删除事件">✕</button></td>`;
    tr.querySelector('[data-f="name"]').value = e.name;
    tr.querySelector('[data-f="earliest_start"]').value = e.earliest_start;
    tr.querySelector('[data-f="latest_start"]').value = e.latest_start;
    tr.querySelector('[data-f="duration_minutes"]').value = e.duration_minutes;
    tr.querySelector('[data-f="note"]').value = e.note;
    tr.querySelector(".row-del").addEventListener("click", () => {
      syncFromDOM();
      state.editEvents = state.editEvents.filter((x) => x.key !== e.key);
      state.editRelations = state.editRelations.filter(
        (r) => r.predecessor_key !== e.key && r.successor_key !== e.key);
      renderEventsTable();
      renderRelations();
    });
    tbody.appendChild(tr);
  }
}

/* 间隔展示文案：未设置（最短 0 / 最长不限）时不附加说明，与旧语义一致 */
function gapText(r) {
  const min = r.min_gap_minutes === null || r.min_gap_minutes === undefined ? 0 : Number(r.min_gap_minutes);
  const max = r.max_gap_minutes === null || r.max_gap_minutes === undefined || r.max_gap_minutes === ""
    ? null : Number(r.max_gap_minutes);
  const parts = [];
  if (min > 0) parts.push(`最短等待 ${min} 分钟`);
  if (max !== null) parts.push(`最长等待 ${max} 分钟`);
  return parts.length ? `（${parts.join("，")}）` : "";
}

/* 解析间隔输入：空串 → 默认值；非法 → {ok:false} */
function parseGapInput(raw, label, emptyValue) {
  const s = String(raw).trim();
  if (s === "") return { ok: true, value: emptyValue };
  if (!/^\d+$/.test(s)) return { ok: false, message: `“先于”关系的${label}必须是非负整数分钟` };
  return { ok: true, value: parseInt(s, 10) };
}

function renderRelations() {
  const opts = state.editEvents
    .map((e) => `<option value="${e.key}">${escapeHtml(e.name)}</option>`).join("");
  $("#rel-pre").innerHTML = opts;
  $("#rel-suc").innerHTML = opts;
  const ul = $("#relations-list");
  ul.innerHTML = "";
  state.editRelations.forEach((r, idx) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `「${eventName(r.predecessor_key)}」先于「${eventName(r.successor_key)}」`;
    li.appendChild(label);

    const gaps = document.createElement("span");
    gaps.className = "rel-gaps";
    gaps.innerHTML =
      `<label>最短等待 <input type="number" min="0" step="1" data-gap="min" value="${r.min_gap_minutes}"> 分钟</label>` +
      `<label>最长等待 <input type="number" min="0" step="1" data-gap="max" placeholder="不限" value="${r.max_gap_minutes}"> 分钟</label>`;
    gaps.querySelector('[data-gap="min"]').addEventListener("input", (ev) => {
      r.min_gap_minutes = ev.target.value === "" ? "" : parseInt(ev.target.value, 10);
      if (Number.isNaN(r.min_gap_minutes)) r.min_gap_minutes = "";
    });
    gaps.querySelector('[data-gap="max"]').addEventListener("input", (ev) => {
      r.max_gap_minutes = ev.target.value === "" ? "" : parseInt(ev.target.value, 10);
      if (Number.isNaN(r.max_gap_minutes)) r.max_gap_minutes = "";
    });
    li.appendChild(gaps);

    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.addEventListener("click", () => {
      state.editRelations.splice(idx, 1);
      renderRelations();
    });
    li.appendChild(btn);
    ul.appendChild(li);
  });
}

function chainHtml(chain) {
  return `<ol class="chain">` + chain.map((s) => `<li>${escapeHtml(s.text)}</li>`).join("") + `</ol>`;
}

function fmtTime(s) { return s ? escapeHtml(String(s).replace("T", " ")) : "—（暂存）"; }

function buildResultHtml(res, evByKey) {
  // 生成推算结果各分区 HTML。evByKey: {窗口中使用的事件 id/key -> 事件输入}
  const nConf = res.conflicts.length;
  const nDraft = res.drafts.length;
  let summary;
  if (nConf > 0) {
    summary = `<div class="summary err">推算发现 ${nConf} 处约束冲突 —— 原始输入已保留，该版本不得定稿。</div>`;
  } else if (nDraft > 0) {
    summary = `<div class="summary warn">推算通过，但有 ${nDraft} 个事件处于暂存状态，补齐时间范围前不得定稿。</div>`;
  } else {
    summary = `<div class="summary ok">推算通过：所有事件均存在可行时间窗。</div>`;
  }

  let conflicts = "";
  if (nConf > 0) {
    conflicts = `<div class="section-title">冲突明细（输入已保留）</div>` + res.conflicts.map((c) => {
      let html = `<div class="card conflict"><div class="msg">⚠ ${escapeHtml(c.message)}</div>`;
      if (c.earliest_chain) {
        html += `<details open><summary>最早可行开始的依据链</summary>${chainHtml(c.earliest_chain)}</details>`;
        html += `<details open><summary>最晚可行开始的依据链</summary>${chainHtml(c.latest_chain)}</details>`;
      }
      return html + `</div>`;
    }).join("");
  }

  let drafts = "";
  if (nDraft > 0) {
    drafts = `<div class="section-title">暂存事件（不参与推算）</div>` + res.drafts.map((d) =>
      `<div class="card draft"><h3>「${escapeHtml(d.name)}」</h3>` +
      `<div class="reason">${escapeHtml(d.reason)}</div></div>`).join("");
  }

  let skipped = "";
  if (res.skipped_relations.length > 0) {
    skipped = `<div class="section-title">暂不参与推算的关系</div>` + res.skipped_relations.map((s) =>
      `<div class="card draft"><div class="reason">${escapeHtml(s.reason)}</div></div>`).join("");
  }

  let windows = "";
  const winIds = Object.keys(res.windows);
  if (winIds.length > 0) {
    windows = `<div class="section-title">各事件可行时间窗（推算结果，与原始输入分开保存）</div>` +
      winIds.map((id) => {
        const w = res.windows[id];
        const ev = evByKey[id];
        const name = ev ? ev.name : id;
        const dur = ev ? ev.duration_minutes : "";
        return `<div class="card">` +
          `<h3>「${escapeHtml(name)}」 <span class="window">${w.earliest_start.replace("T", " ")} ~ ${w.latest_start.replace("T", " ")}</span></h3>` +
          `<div class="hint">可行结束窗口：${w.earliest_end.replace("T", " ")} ~ ${w.latest_end.replace("T", " ")}` +
          (dur === null || dur === undefined || dur === "" ? "" : `（持续 ${dur} 分钟）`) + `</div>` +
          `<details><summary>最早可行开始的依据链</summary>${chainHtml(w.earliest_chain)}</details>` +
          `<details><summary>最晚可行开始的依据链</summary>${chainHtml(w.latest_chain)}</details>` +
          `</div>`;
      }).join("");
  }

  return { summary, conflicts, drafts, skipped, windows };
}

function renderResults() {
  const comp = state.current.computation;
  const slots = [
    ["#result-summary", "summary"], ["#conflicts", "conflicts"],
    ["#drafts", "drafts"], ["#skipped", "skipped"], ["#windows", "windows"],
  ];
  if (!comp) {
    for (const [sel] of slots) $(sel).innerHTML = "";
    $("#result-summary").innerHTML = `<div class="summary warn">尚无推算结果，请保存后查看。</div>`;
    return;
  }
  const byKey = {};
  for (const e of state.current.events) byKey[String(e.id)] = e;
  const parts = buildResultHtml(comp.result, byKey);
  for (const [sel, key] of slots) $(sel).innerHTML = parts[key];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- 保存 */

function syncFromDOM() {
  document.querySelectorAll("#events-table tbody tr").forEach((tr) => {
    const e = state.editEvents.find((x) => x.key === tr.dataset.key);
    if (!e) return;
    e.name = tr.querySelector('[data-f="name"]').value.trim();
    e.earliest_start = tr.querySelector('[data-f="earliest_start"]').value;
    e.latest_start = tr.querySelector('[data-f="latest_start"]').value;
    const d = tr.querySelector('[data-f="duration_minutes"]').value;
    e.duration_minutes = d === "" ? "" : parseInt(d, 10);
    e.note = tr.querySelector('[data-f="note"]').value;
  });
}

async function save() {
  if (!state.current) return;
  syncFromDOM();
  const relations = [];
  for (const r of state.editRelations) {
    const min = parseGapInput(r.min_gap_minutes, "最短等待", 0);
    const max = parseGapInput(r.max_gap_minutes, "最长等待", null);
    if (!min.ok || !max.ok) {
      showBanner("⚠ " + escapeHtml(min.message || max.message));
      return;
    }
    if (max.value !== null && max.value < min.value) {
      showBanner(`⚠ 关系「${escapeHtml(eventName(r.predecessor_key))}」先于` +
        `「${escapeHtml(eventName(r.successor_key))}」的最长等待不得短于最短等待`);
      return;
    }
    relations.push({
      predecessor_key: r.predecessor_key,
      successor_key: r.successor_key,
      min_gap_minutes: min.value,
      max_gap_minutes: max.value,
    });
  }
  const payload = {
    base_version: state.current.review.version,
    title: $("#review-title").value.trim(),
    events: state.editEvents.map((e) => ({
      key: e.key,
      name: e.name,
      earliest_start: e.earliest_start || null,
      latest_start: e.latest_start || null,
      duration_minutes: e.duration_minutes === "" ? null : e.duration_minutes,
      note: e.note,
    })),
    relations,
  };
  try {
    const bundle = await api("PUT", "/api/reviews/" + state.current.review.id, payload);
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    loadVersions();
    loadReviews(bundle.review.id);
  } catch (err) {
    handleSaveError(err);
  }
}

function handleSaveError(err) {
  if (err.status === 409) {
    showBanner(
      `⚠ ${escapeHtml(err.data.error)}（服务器当前版本：v${err.data.current_version}，` +
      `你基于 v${state.current.review.version} 编辑）` +
      `<button id="btn-reload-latest">加载最新版本（放弃未保存修改）</button>`);
    $("#btn-reload-latest").addEventListener("click", () => {
      if (confirm("确定放弃当前未保存的修改并加载最新版本吗？")) {
        openReview(state.current.review.id);
      }
    });
  } else {
    showBanner("⚠ " + escapeHtml(err.message || "保存失败"));
  }
}

async function finalize() {
  if (!state.current) return;
  try {
    const bundle = await api("POST", "/api/reviews/" + state.current.review.id + "/finalize",
      { base_version: state.current.review.version });
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    loadVersions();
    loadReviews(bundle.review.id);
  } catch (err) {
    handleSaveError(err);
  }
}

/* ------------------------------------------------------------ 版本回看/恢复 */

async function loadVersions() {
  if (!state.current) return;
  const data = await api("GET", "/api/reviews/" + state.current.review.id + "/versions");
  const tbody = $("#versions-table tbody");
  tbody.innerHTML = "";
  for (const v of data.versions) {
    const tr = document.createElement("tr");
    const cur = v.version === data.current_version;
    let html = `<td>v${v.version}${cur ? ' <span class="kind-tag">当前</span>' : ""}</td>`;
    html += `<td>${kindTag(v)}</td>`;
    html += `<td>${statusText(v)}</td>`;
    html += `<td class="hint">${(v.computed_at || "").replace("T", " ")}</td>`;
    html += `<td class="version-actions"></td>`;
    tr.innerHTML = html;
    const td = tr.querySelector(".version-actions");
    if (v.reviewable) {
      const btn = document.createElement("button");
      btn.textContent = "回看";
      btn.addEventListener("click", () => openVersion(v.version));
      td.appendChild(btn);
    } else {
      const span = document.createElement("span");
      span.className = "legacy-note";
      span.textContent = "不可回看、不可恢复（" + (v.reason || "缺少输入快照") + "）";
      td.appendChild(span);
    }
    tbody.appendChild(tr);
  }
}

function kindTag(v) {
  if (v.kind === "finalize") return `<span class="kind-tag finalize">已定稿</span>`;
  if (v.kind === "restore") return `<span class="kind-tag restore">恢复产生</span>`;
  if (v.kind === "save") return `<span class="kind-tag">保存</span>`;
  return `<span class="kind-tag">历史记录</span>`;
}

function statusText(v) {
  if (v.state === "finalized") return `<span class="badge ok">已定稿</span>`;
  if (v.status === "conflict") return `<span class="badge err">冲突·不得定稿</span>`;
  if (v.has_drafts) return `<span class="badge warn">草稿·有暂存</span>`;
  return `<span class="badge">草稿</span>`;
}

async function openVersion(version) {
  const rid = state.current.review.id;
  let detail;
  try {
    detail = await api("GET", `/api/reviews/${rid}/versions/${version}`);
  } catch (err) {
    showBanner("⚠ " + escapeHtml(err.message || "无法回看该版本"));
    return;
  }
  $("#vm-title").textContent = `v${detail.version} ${detail.title}`;
  $("#vm-badges").innerHTML = kindTag(detail) + " " + statusText(detail);
  let meta = `推算状态：${detail.status === "ok" ? "通过" : "冲突"}`;
  if (detail.kind === "restore" && detail.source_version) {
    meta += `　|　由 v${detail.source_version} 恢复而来`;
  }
  meta += `　|　推算时间：${(detail.computed_at || "").replace("T", " ")}`;
  $("#vm-meta").textContent = meta;
  $("#vm-body").innerHTML = renderVersionDetail(detail);
  const btn = $("#btn-vm-restore");
  btn.disabled = false;
  btn.onclick = () => restoreVersion(detail);
  $("#version-modal").classList.remove("hidden");
}

function closeVersionModal() {
  $("#version-modal").classList.add("hidden");
}

function renderVersionDetail(detail) {
  const events = detail.input.events || [];
  const byKey = {};
  for (const e of events) byKey[e.key] = e;

  const rows = events.map((e) =>
    `<tr><td>${escapeHtml(e.name)}</td>` +
    `<td>${fmtTime(e.earliest_start)}</td>` +
    `<td>${fmtTime(e.latest_start)}</td>` +
    `<td>${e.duration_minutes === null || e.duration_minutes === undefined ? "—" : e.duration_minutes}</td>` +
    `<td>${escapeHtml(e.note || "")}</td></tr>`).join("");

  const rels = (detail.input.relations || []).map((r) => {
    const a = byKey[r.predecessor_key], b = byKey[r.successor_key];
    const an = a ? a.name : "?";
    const bn = b ? b.name : "?";
    return `<div>「${escapeHtml(an)}」先于「${escapeHtml(bn)}」${escapeHtml(gapText(r))}</div>`;
  }).join("") || `<div class="hint">无</div>`;

  // 快照事件以 id_<旧行id> 为 key，推算结果窗口仍以旧行 id 为键
  const winIndex = {};
  for (const e of events) winIndex[String(e.key).replace(/^id_/, "")] = e;
  const parts = buildResultHtml(detail.result, winIndex);

  return `<div class="section-title">当时输入（不可变快照）</div>` +
    `<table><thead><tr><th>事件名称</th><th>最早开始</th><th>最晚开始</th>` +
    `<th>持续(分钟)</th><th>备注</th></tr></thead><tbody>${rows}</tbody></table>` +
    `<div class="section-title">“先于”关系</div><div class="vm-relations">${rels}</div>` +
    `<div class="section-title">当时推算依据与结果</div>` +
    parts.summary + parts.conflicts + parts.drafts + parts.skipped + parts.windows;
}

async function restoreVersion(detail) {
  if (!state.current) return;
  if (!confirm(`确定把 v${detail.version} 的输入恢复为当前复盘的新草稿版本吗？\n` +
               `将重新推算，且不会覆盖任何旧版本（需基于当前 v${state.current.review.version}）。`)) {
    return;
  }
  try {
    const bundle = await api(
      "POST",
      `/api/reviews/${state.current.review.id}/versions/${detail.version}/restore`,
      { base_version: state.current.review.version });
    closeVersionModal();
    state.current = bundle;
    resetEdits();
    hideBanner();
    render();
    await loadVersions();
    loadReviews(bundle.review.id);
  } catch (err) {
    handleSaveError(err);
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

$("#btn-new").addEventListener("click", async () => {
  const title = prompt("复盘标题：", "未命名复盘");
  if (title === null) return;
  const bundle = await api("POST", "/api/reviews", { title: title.trim() || "未命名复盘" });
  await loadReviews(bundle.review.id);
  await openReview(bundle.review.id);
});

$("#btn-delete").addEventListener("click", async () => {
  if (!state.current) return;
  if (!confirm(`确定删除复盘「${state.current.review.title}」吗？该操作不可恢复。`)) return;
  await api("DELETE", "/api/reviews/" + state.current.review.id);
  state.current = null;
  $("#main").classList.add("hidden");
  await loadReviews();
  if (state.reviews.length) await openReview(state.reviews[0].id);
});

$("#btn-refresh").addEventListener("click", () => {
  if (state.current) openReview(state.current.review.id);
});

$("#btn-reload-versions").addEventListener("click", () => {
  if (state.current) loadVersions();
});
$("#btn-vm-close").addEventListener("click", closeVersionModal);
$("#version-modal").addEventListener("click", (e) => {
  if (e.target.id === "version-modal") closeVersionModal();
});

$("#review-select").addEventListener("change", (e) => openReview(e.target.value));
$("#btn-save").addEventListener("click", save);
$("#btn-finalize").addEventListener("click", finalize);

$("#btn-add-event").addEventListener("click", () => {
  syncFromDOM();
  state.editEvents.push({
    key: newKey(), name: "新事件", earliest_start: "", latest_start: "",
    duration_minutes: "", note: "",
  });
  renderEventsTable();
  renderRelations();
});

$("#btn-add-relation").addEventListener("click", () => {
  const a = $("#rel-pre").value, b = $("#rel-suc").value;
  if (!a || !b) return;
  const min = parseGapInput($("#rel-min-gap").value, "最短等待", 0);
  const max = parseGapInput($("#rel-max-gap").value, "最长等待", null);
  if (!min.ok || !max.ok) {
    showBanner("⚠ " + escapeHtml(min.message || max.message));
    return;
  }
  if (max.value !== null && max.value < min.value) {
    showBanner("⚠ “先于”关系的最长等待不得短于最短等待");
    return;
  }
  hideBanner();
  state.editRelations.push({
    predecessor_key: a, successor_key: b,
    min_gap_minutes: min.value, max_gap_minutes: max.value === null ? "" : max.value,
  });
  renderRelations();
});

/* ---------------------------------------------------------------- 启动 */

(async function init() {
  await loadReviews();
  if (state.reviews.length) await openReview(state.reviews[0].id);
})();
