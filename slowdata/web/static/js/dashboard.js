/* 概览页：数据拉取 + ECharts 渲染 + 运行触发/轮询 */
"use strict";

let costChart = null, radarChart = null, sourceChart = null;

async function loadOverview() {
  const res = await fetch("/api/overview");
  const d = await res.json();

  // 指标卡
  document.getElementById("s-items").textContent = fmtNum(d.totals.items);
  document.getElementById("s-entities").textContent = fmtNum(d.totals.entities);
  document.getElementById("s-events").textContent = fmtNum(d.totals.events);
  document.getElementById("s-claims").textContent = fmtNum(d.totals.claims);

  // 成本趋势
  costChart = costChart || initChart(document.getElementById("chart-cost"));
  costChart.setOption({
    ...ECHARTS_BASE,
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: d.cost_series.dates.map((x) => x.slice(5)), axisLine: { lineStyle: { color: "rgba(120,150,220,0.25)" } }, axisLabel: { color: "#5d6b8f", fontSize: 11 } },
    yAxis: { type: "value", axisLabel: { color: "#5d6b8f", fontSize: 11 }, splitLine: { lineStyle: { color: "rgba(120,150,220,0.1)" } } },
    series: [{
      type: "bar",
      data: d.cost_series.costs,
      itemStyle: {
        borderRadius: [6, 6, 0, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#22d3ee" }, { offset: 1, color: "rgba(91,140,255,0.25)" }]),
      },
      label: { show: true, position: "top", color: "#93a1c4", fontSize: 10, formatter: (p) => (p.value > 0 ? "$" + p.value : "") },
    }],
  });

  // Critic 雷达
  radarChart = radarChart || initChart(document.getElementById("chart-radar"));
  if (d.latest_run && d.latest_run.critic_scores && Object.keys(d.latest_run.critic_scores).length) {
    radarChart.setOption(criticRadarOption(d.latest_run.critic_scores));
  } else {
    radarChart.setOption({ ...ECHARTS_BASE, title: { text: "暂无 Critic 评分（跑一次完整流水线后出现）", left: "center", top: "middle", textStyle: { color: "#5d6b8f", fontSize: 12, fontWeight: 400 } } });
  }

  // 信源分布
  sourceChart = sourceChart || initChart(document.getElementById("chart-source"));
  if (d.per_source.length) {
    sourceChart.setOption({
      ...ECHARTS_BASE,
      tooltip: { trigger: "item", formatter: (p) => `${p.name}: ${p.value} 条` },
      legend: { bottom: 0, textStyle: { color: "#93a1c4", fontSize: 11 } },
      series: [{
        type: "pie",
        radius: ["42%", "68%"],
        center: ["50%", "44%"],
        itemStyle: { borderRadius: 6, borderColor: "#0f1526", borderWidth: 2 },
        label: { show: false },
        data: d.per_source.map(([name, value]) => ({ name, value })),
        color: ["#5b8cff", "#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f87171", "#f472b6"],
      }],
    });
  } else {
    sourceChart.setOption({ ...ECHARTS_BASE, title: { text: "暂无信源数据", left: "center", top: "middle", textStyle: { color: "#5d6b8f", fontSize: 12, fontWeight: 400 } } });
  }

  // 运行状态
  const snap = d.snapshot;
  const pill = document.getElementById("run-pill");
  pill.innerHTML = statusBadge(snap.status);
  // 公开只读模式：隐藏触发按钮（系统按每日 08:30 定时自动运行）
  if (snap.readonly) {
    document.getElementById("btn-trigger").style.display = "none";
    document.getElementById("trigger-mq").style.display = "none";
    document.getElementById("run-live").innerHTML =
      '<span class="muted">🔒 公开只读模式 · 由系统每日 08:30（北京时间）自动运行</span>';
  }
  const det = document.getElementById("run-detail");
  if (d.latest_run && d.latest_run.id) {
    det.innerHTML =
      `<span>最近运行 <b>${d.latest_run.id}</b></span>` +
      `<span>${d.latest_run.at}</span>` +
      `<span>成本 <b>${fmtMoney(d.latest_run.cost_usd)}</b></span>` +
      `<span>调用 <b>${d.latest_run.calls}</b> 次</span>` +
      (d.latest_run.critic_round ? `<span>Critic 第 <b>${d.latest_run.critic_round}</b> 轮</span>` : "") +
      (d.latest_run.critic_failed_accept ? `<span class="badge running">⚠️ 降级接受</span>` : "");
  } else {
    det.innerHTML = `<span class="muted">还没有运行记录</span>`;
  }

  // 运行中轮询（只读模式跳过）
  const live = document.getElementById("run-live");
  if (!snap.readonly) {
    if (snap.status === "running") {
      live.innerHTML = `<span class="badge running pulse">● 运行中，开始于 ${snap.started_at}</span>`;
      setTimeout(loadOverview, 5000);
    } else {
      live.innerHTML = "";
    }
  }

  // 日报列表
  document.getElementById("report-list").innerHTML = d.reports.length
    ? d.reports.map((r) => `<li><a href="/reports/${r}">📄 ${r}</a></li>`).join("")
    : `<li class="muted">暂无日报</li>`;

  // 最近运行
  document.getElementById("recent-runs").innerHTML =
    `<tr><th>时间</th><th>状态</th><th>成本</th><th>调用</th></tr>` +
    d.recent_runs.map((r) =>
      `<tr><td>${r.at}</td><td>${statusBadge(r.status)}</td><td class="num-col">${fmtMoney(r.cost)}</td><td class="num-col">${r.calls}</td></tr>`
    ).join("");
}

// 手动触发
document.getElementById("btn-trigger").addEventListener("click", async () => {
  const mq = document.getElementById("trigger-mq").value;
  const fd = new FormData();
  if (mq) fd.append("max_queries", mq);
  const res = await fetch("/api/runs", { method: "POST", body: fd });
  const d = await res.json();
  if (d.started) { toast("已触发运行，页面将自动刷新进度"); setTimeout(loadOverview, 3000); }
  else if (d.status === "running") { toast("已有运行进行中"); }
  else { toast("触发失败", "err"); }
});

loadOverview();
setInterval(loadOverview, 30000);
