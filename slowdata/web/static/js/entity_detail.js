/* 实体档案：事件活跃度时间线图 */
"use strict";
(async () => {
  const eid = location.pathname.split("/").pop();
  const res = await fetch(`/api/entity/${eid}/timeline`);
  const d = await res.json();
  const el = document.getElementById("chart-timeline");
  const chart = initChart(el);
  if (!d.days.length) {
    chart.setOption({ ...ECHARTS_BASE, title: { text: "暂无事件记录", left: "center", top: "middle", textStyle: { color: "#5d6b8f", fontSize: 12, fontWeight: 400 } } });
    return;
  }
  chart.setOption({
    ...ECHARTS_BASE,
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: d.days.map((x) => x.slice(5)), axisLabel: { color: "#5d6b8f", fontSize: 10 }, axisLine: { lineStyle: { color: "rgba(120,150,220,0.25)" } } },
    yAxis: { type: "value", minInterval: 1, axisLabel: { color: "#5d6b8f", fontSize: 11 }, splitLine: { lineStyle: { color: "rgba(120,150,220,0.1)" } } },
    series: [{
      type: "bar",
      data: d.counts,
      itemStyle: {
        borderRadius: [5, 5, 0, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: "#a78bfa" }, { offset: 1, color: "rgba(167,139,250,0.2)" }]),
      },
    }],
  });
})();
