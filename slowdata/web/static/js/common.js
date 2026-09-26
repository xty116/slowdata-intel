/* 公共工具：请求、格式化、toast、图表主题、徽章渲染 */
"use strict";

const ECHARTS_BASE = {
  textStyle: { color: "#93a1c4", fontFamily: "-apple-system, 'PingFang SC', 'Microsoft YaHei', sans-serif" },
  grid: { left: 44, right: 16, top: 26, bottom: 26 },
};

function fmtMoney(v) {
  if (v == null) return "—";
  return v >= 0.01 ? "$" + v.toFixed(4) : "$" + v.toFixed(6);
}
function fmtNum(v) { return v == null ? "—" : Number(v).toLocaleString(); }

function toast(msg, kind = "ok", ms = 3200) {
  let box = document.getElementById("toasts");
  if (!box) {
    box = document.createElement("div");
    box.id = "toasts";
    document.body.appendChild(box);
  }
  const t = document.createElement("div");
  t.className = "toast " + (kind === "err" ? "err" : kind === "ok" ? "ok" : "");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => { t.classList.add("fade-out"); setTimeout(() => t.remove(), 450); }, ms);
}

function setActiveNav() {
  const path = location.pathname;
  document.querySelectorAll(".topbar nav a").forEach((a) => {
    const href = a.getAttribute("href");
    if (href === "/") a.classList.toggle("active", path === "/");
    else if (href !== "/" && path.startsWith(href)) a.classList.toggle("active", true);
  });
}

function statusBadge(status) {
  const map = { success: "success", done: "done", running: "running", failed: "failed", idle: "idle" };
  return `<span class="badge ${map[status] || "idle"}">${status}</span>`;
}

function dimsChips(dims) {
  try {
    const arr = JSON.parse(dims || "[]");
    return arr.map((d) => `<span class="badge dim">${d}</span>`).join(" ");
  } catch { return ""; }
}

function impDots(n) {
  let s = '<span class="imp">';
  for (let i = 1; i <= 5; i++) s += `<i class="${i <= n ? "on" : ""}"></i>`;
  return s + "</span>";
}

function verdictBadges(v) {
  if (!v) return '<span class="muted">—</span>';
  return `<span class="verdict"><span class="ok">${v.support}✅</span> <span class="ct">${v.contradict}❌</span> <span class="un">${v.unverified}❓</span></span>`;
}

function initChart(el) {
  if (!el || !window.echarts) return null;
  const c = echarts.init(el, null, { renderer: "canvas" });
  window.addEventListener("resize", () => c.resize());
  return c;
}

const CRITIC_DIMS = ["factuality", "completeness", "citations", "timeliness", "actionability"];
const CRITIC_LABELS = {
  factuality: "事实性", completeness: "完整性", citations: "引用可靠性",
  timeliness: "时效性", actionability: "可操作性",
};

function criticRadarOption(scores) {
  const values = CRITIC_DIMS.map((d) => (scores && scores[d] != null ? scores[d] : 0));
  return {
    ...ECHARTS_BASE,
    tooltip: { trigger: "item" },
    radar: {
      indicator: CRITIC_DIMS.map((d) => ({ name: CRITIC_LABELS[d], max: 10 })),
      radius: "68%",
      axisName: { color: "#93a1c4", fontSize: 11 },
      splitLine: { lineStyle: { color: "rgba(120,150,220,0.15)" } },
      splitArea: { areaStyle: { color: ["rgba(120,150,220,0.03)", "rgba(120,150,220,0.06)"] } },
      axisLine: { lineStyle: { color: "rgba(120,150,220,0.2)" } },
    },
    series: [{
      type: "radar",
      data: [{
        value: values,
        name: "Critic 评分",
        areaStyle: { color: "rgba(91,140,255,0.28)" },
        lineStyle: { color: "#5b8cff", width: 2 },
        itemStyle: { color: "#22d3ee" },
      }],
    }],
  };
}
