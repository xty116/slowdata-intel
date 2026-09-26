/* 实体列表：类型 chips + 即时搜索（服务端过滤） */
"use strict";
const $ = (id) => document.getElementById(id);
let curType = "", timer = null;

const TYPE_BADGE = {
  company: "blue", model: "dim", benchmark: "", dataset: "blue", person: "dim", supplier: "running",
};

function load() {
  const p = new URLSearchParams();
  if (curType) p.set("etype", curType);
  if ($("f-q").value.trim()) p.set("q", $("f-q").value.trim());
  fetch("/api/entities-list?" + p.toString())
    .then((r) => r.json())
    .then((d) => {
      $("ent-body").innerHTML = d.rows.length
        ? d.rows.map((e) => `
          <tr>
            <td><b>${escapeHtml(e.name)}</b></td>
            <td><span class="badge ${TYPE_BADGE[e.type] || ""}">${e.type}</span></td>
            <td>${e.first_seen}</td>
            <td>${e.last_seen}</td>
            <td><a class="btn ghost sm" href="/entities/${e.id}">档案 →</a></td>
          </tr>`).join("")
        : `<tr><td colspan="5" class="empty">无匹配实体</td></tr>`;
    });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

fetch("/api/entities-list?limit=200").then((r) => r.json()).then((d) => {
  d.types.forEach((t) => {
    const c = document.createElement("span");
    c.className = "chip"; c.dataset.type = t; c.textContent = t;
    c.addEventListener("click", () => {
      document.querySelectorAll("#type-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
      curType = c.dataset.type;
      load();
    });
    $("type-chips").appendChild(c);
  });
});
$("f-q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
load();
