/* 运行页：节点成本柱状图 + Critic 雷达 + 实时触发进度 */
"use strict";
let nodeChart = null, radarChart = null;

function renderCharts(d) {
  nodeChart = nodeChart || initChart(document.getElementById("chart-nodes"));
  const nodes = d.per_node || {};
  const names = Object.keys(nodes);
  if (names.length) {
    nodeChart.setOption({
      ...ECHARTS_BASE,
      tooltip: { trigger: "axis", formatter: (ps) => `${ps[0].name}<br/>成本 $${ps[0].value}` },
      grid: { left: 130, right: 30, top: 16, bottom: 26 },
      xAxis: { type: "value", axisLabel: { color: "#5d6b8f", fontSize: 10 }, splitLine: { lineStyle: { color: "rgba(120,150,220,0.1)" } } },
      yAxis: { type: "category", data: names, axisLabel: { color: "#93a1c4", fontSize: 11 }, axisLine: { lineStyle: { color: "rgba(120,150,220,0.25)" } } },
      series: [{
        type: "bar",
        data: names.map((n) => nodes[n].cost_usd),
        itemStyle: {
          borderRadius: [0, 6, 6, 0],
          color: new echarts.graphic.LinearGradient(1, 0, 0, 0, [{ offset: 0, color: "#22d3ee" }, { offset: 1, color: "rgba(91,140,255,0.3)" }]),
        },
        label: { show: true, position: "right", color: "#93a1c4", fontSize: 10, formatter: (p) => "$" + p.value },
      }],
    });
  } else {
    nodeChart.setOption({ ...ECHARTS_BASE, title: { text: "暂无节点成本数据", left: "center", top: "middle", textStyle: { color: "#5d6b8f", fontSize: 12, fontWeight: 400 } } });
  }

  radarChart = radarChart || initChart(document.getElementById("chart-radar"));
  if (d.critic_scores && Object.keys(d.critic_scores).length) {
    radarChart.setOption(criticRadarOption(d.critic_scores));
  } else {
    radarChart.setOption({ ...ECHARTS_BASE, title: { text: "暂无 Critic 评分", left: "center", top: "middle", textStyle: { color: "#5d6b8f", fontSize: 12, fontWeight: 400 } } });
  }
}

async function loadCharts() {
  const res = await fetch("/api/overview");
  const d = await res.json();
  const latest = await (await fetch("/api/runs/latest")).json();
  renderCharts(latest);
  const pill = document.getElementById("live-pill");
  pill.innerHTML = statusBadge(d.snapshot.status);
  const live = document.getElementById("run-live");
  if (d.snapshot.status === "running") {
    live.innerHTML = `<span class="badge running pulse">● 运行中 ${d.snapshot.started_at}</span>`;
    setTimeout(loadCharts, 5000);
  } else if (d.snapshot.status === "done") {
    live.innerHTML = `<span class="badge success">✓ 上次运行完成</span>`;
  } else if (d.snapshot.status === "failed") {
    live.innerHTML = `<span class="badge failed">✗ 上次运行失败</span>`;
  } else {
    live.innerHTML = "";
  }
}

document.getElementById("btn-trigger").addEventListener("click", async () => {
  const mq = document.getElementById("trigger-mq").value;
  const fd = new FormData();
  if (mq) fd.append("max_queries", mq);
  const res = await fetch("/api/runs", { method: "POST", body: fd });
  const d = await res.json();
  if (d.started) { toast("已触发运行，页面自动刷新"); setTimeout(loadCharts, 3000); }
  else if (d.status === "running") toast("已有运行进行中");
  else toast("触发失败", "err");
});

loadCharts();
