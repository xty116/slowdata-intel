/* 日报阅读：自动生成目录导航 + 滚动高亮 */
"use strict";
(function () {
  const body = document.getElementById("report-body");
  const box = document.getElementById("toc-links");
  const hs = Array.from(body.querySelectorAll("h2"));
  if (!hs.length) {
    box.innerHTML = '<span class="muted">本报告无章节标题</span>';
    return;
  }
  const items = hs.map((h, i) => {
    const id = "sec-" + i;
    h.id = id;
    return { id, text: h.textContent.trim().replace(/^#+\s*/, "") };
  });
  box.innerHTML = items.map((it) => `<a href="#${it.id}" data-sec="${it.id}">${it.text}</a>`).join("");

  const links = Array.from(box.querySelectorAll("a"));
  const onScroll = () => {
    let cur = items[0].id;
    for (const it of items) {
      const el = document.getElementById(it.id);
      if (el && el.getBoundingClientRect().top <= 100) cur = it.id;
    }
    links.forEach((a) => a.classList.toggle("active", a.dataset.sec === cur));
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
