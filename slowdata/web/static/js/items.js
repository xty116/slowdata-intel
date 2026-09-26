/* 条目检索 SPA：即时筛选 + 分页 */
"use strict";
const PAGE_SIZE = 50;
let offset = 0, total = 0, timer = null;

const $ = (id) => document.getElementById(id);

function buildQuery() {
  const p = new URLSearchParams();
  if ($("f-q").value.trim()) p.set("q", $("f-q").value.trim());
  if ($("f-source").value) p.set("source", $("f-source").value);
  if ($("f-dim").value) p.set("dimension", $("f-dim").value);
  if ($("f-imp").value !== "0") p.set("min_imp", $("f-imp").value);
  if ($("f-ver").value) p.set("verified", $("f-ver").value);
  p.set("limit", PAGE_SIZE);
  p.set("offset", offset);
  return p.toString();
}

async function load() {
  const res = await fetch("/api/items?" + buildQuery());
  const d = await res.json();
  total = d.total;
  $("items-total").textContent = `共 ${total} 条`;
  const body = $("items-body");
  if (!d.rows.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty">无匹配条目</td></tr>`;
  } else {
    body.innerHTML = d.rows.map((r) => `
      <tr>
        <td class="num-col">${r.id}</td>
        <td><a class="ttl" href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a></td>
        <td>${dimsChips(r.dims)}</td>
        <td>${impDots(r.importance)}</td>
        <td>${r.is_new ? '<span class="badge blue">🆕 新</span>' : '<span class="badge">复现</span>'}</td>
        <td>${verdictBadges(r.verdicts)}</td>
        <td><span class="badge">${escapeHtml(r.source)}</span></td>
        <td>${r.date || '<span class="muted">—</span>'}</td>
      </tr>`).join("");
  }
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const cur = Math.floor(offset / PAGE_SIZE) + 1;
  $("page-info").textContent = `第 ${cur} / ${pages} 页`;
  $("btn-prev").disabled = offset <= 0;
  $("btn-next").disabled = offset + PAGE_SIZE >= total;
  $("btn-prev").style.opacity = offset <= 0 ? 0.4 : 1;
  $("btn-next").style.opacity = offset + PAGE_SIZE >= total ? 0.4 : 1;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// 信源下拉
fetch("/api/items?limit=1").then(async () => {
  const res = await fetch("/api/overview");
  const d = await res.json();
  const sel = $("f-source");
  d.per_source.forEach(([name]) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  });
});

["f-source", "f-dim", "f-imp", "f-ver"].forEach((id) => $(id).addEventListener("change", () => { offset = 0; load(); }));
$("f-q").addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(() => { offset = 0; load(); }, 300);
});
$("btn-prev").addEventListener("click", () => { offset = Math.max(0, offset - PAGE_SIZE); load(); });
$("btn-next").addEventListener("click", () => { offset += PAGE_SIZE; load(); });

load();
