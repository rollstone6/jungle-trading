// ===== 黑夜模式图表换肤共享层（t_712add03）=====
// canvas / SVG / ECharts / lightweight-charts 不认 CSS 变量，色值必须在运行时以
// getComputedStyle 读取当前 :root token 的字面值——这里就是那唯一真源，
// 禁止各模板另写一套色板（防明暗两套漂移）。
//
// 用法：
//   const C = JTTheme.colors();           // 每次渲染都重新取（跟随当前主题）
//   chart.setOption(JTTheme.applyECharts(option));  // 给 ECharts 注入主题化底样式
//   JTTheme.onTheme(() => redraw());      // 主题切换后重绘（已渲染图表不重建）
(function (global) {
  function rootVars() {
    return getComputedStyle(document.documentElement);
  }
  function isDark() {
    return document.documentElement.getAttribute("data-theme") === "dark";
  }
  // 读取 token 字面值（带兜底），命名与 styles.css :root 对齐。
  function colors() {
    const cs = rootVars();
    const v = (name, fb) => (cs.getPropertyValue(name).trim() || fb);
    return {
      dark: isDark(),
      bg: v("--bg", "#eef3f8"),
      panel: v("--panel", "#ffffff"),
      panel2: v("--panel-2", "#f8fafc"),
      panel3: v("--panel-3", "#f1f5f9"),
      text: v("--text", "#121826"),
      textSoft: v("--text-soft", "#334155"),
      textMid: v("--text-mid", "#475569"),
      muted: v("--muted", "#667085"),
      muted2: v("--muted-2", "#64748b"),
      line: v("--line", "#d9e2ec"),
      lineStrong: v("--line-strong", "#cbd5e1"),
      grid: v("--grid-line", "rgba(217, 226, 236, 0.9)"),
      axis: v("--chart-axis", "#64748b"),
      red: v("--red", "#d92d20"),
      green: v("--green", "#0a7d57"),
      gain: v("--gain", "#d92d20"),
      loss: v("--loss", "#0a7d57"),
      blue: v("--blue", "#1764d8"),
      cyan: v("--cyan", "#0e8aa8"),
      amber: v("--amber", "#b76e00"),
      warn: v("--warn", "#f59e0b"),
      info: v("--info", "#3b82f6"),
      accent: v("--accent", "#667eea"),
      accent2: v("--accent-2", "#764ba2"),
      accentRgb: v("--accent-rgb", "102, 126, 234"),
    };
  }
  // ECharts 主题化底样式：文字/坐标轴/网格线/tooltip 全部跟随 token。
  // 传入已有 option 会 shallow 合并（不覆盖调用方已显式设置的字段）。
  function applyECharts(option) {
    const c = colors();
    option = option || {};
    const axisBase = {
      axisLine: { lineStyle: { color: c.lineStrong } },
      axisTick: { lineStyle: { color: c.lineStrong } },
      axisLabel: { color: c.axis },
      splitLine: { lineStyle: { color: c.grid } },
    };
    option.textStyle = option.textStyle || { color: c.text };
    option.backgroundColor = option.backgroundColor || "transparent";
    // tooltip：暗底给深色面板 + 边框，亮底保持默认浅面板
    const tooltipTheme = c.dark
      ? { backgroundColor: c.panel2, borderColor: c.line, textStyle: { color: c.text } }
      : { backgroundColor: "rgba(255,255,255,0.96)", borderColor: c.line, textStyle: { color: c.text } };
    if (option.tooltip) Object.assign(option.tooltip, tooltipTheme);
    // legend 文字
    if (option.legend && !option.legend.textStyle) option.legend.textStyle = { color: c.axis };
    else if (option.legend) option.legend.textStyle = Object.assign({ color: c.axis }, option.legend.textStyle);
    // 数组型坐标轴（多 grid）与单轴都补齐
    ["xAxis", "yAxis"].forEach((k) => {
      if (!option[k]) return;
      const arr = Array.isArray(option[k]) ? option[k] : [option[k]];
      arr.forEach((ax) => deepAxis(ax, axisBase));
    });
    return option;
  }
  function deepAxis(ax, base) {
    if (!ax) return;
    ax.axisLine = Object.assign({}, base.axisLine, ax.axisLine || {});
    if (ax.axisLine && ax.axisLine.lineStyle) { /* keep merged color if not set */ }
    ax.axisLabel = Object.assign({}, base.axisLabel, ax.axisLabel || {});
    // splitLine 补色判定看 lineStyle.color 是否缺失，而不是整个 lineStyle 是否缺失
    // （QA t_c4e8437c P3③：模板写 splitLine:{lineStyle:{type:'dashed'}} 有对象无 color，
    //   旧条件跳过 → 暗色 --grid-line 没吃上，浅灰网格在暗底刺眼）
    if (ax.splitLine === undefined) ax.splitLine = { lineStyle: { color: base.splitLine.lineStyle.color } };
    else if (ax.splitLine && ax.splitLine.show !== false) {
      if (!ax.splitLine.lineStyle) ax.splitLine.lineStyle = {};
      if (!ax.splitLine.lineStyle.color) ax.splitLine.lineStyle.color = base.splitLine.lineStyle.color;
    }
  }
  // 重绘回调注册表：主题切换时统一触发（模板各自 redraw，图表不重建）。
  const listeners = [];
  function onTheme(fn) { listeners.push(fn); }
  function emit() { listeners.forEach((f) => { try { f(); } catch (e) { /* 单点失败不拖垮 */ } }); }
  // hex/rgb 色转 rgba（半透明填充用，保证单一色源）
  function toRgba(color, alpha) {
    const c = color.trim();
    let m = c.match(/^#([0-9a-f]{3})$/i);
    if (m) { const h = m[1]; return `rgba(${parseInt(h[0]+h[0],16)},${parseInt(h[1]+h[1],16)},${parseInt(h[2]+h[2],16)},${alpha})`; }
    m = c.match(/^#([0-9a-f]{6})$/i);
    if (m) { const h = m[1]; return `rgba(${parseInt(h.slice(0,2),16)},${parseInt(h.slice(2,4),16)},${parseInt(h.slice(4,6),16)},${alpha})`; }
    m = c.match(/^rgba?\(([^)]+)\)$/i);
    if (m) { const p = m[1].split(',').slice(0,3).map(x=>x.trim()); return `rgba(${p.join(',')},${alpha})`; }
    return c;
  }
  global.JTTheme = { colors, isDark, applyECharts, onTheme, toRgba };
  global.addEventListener("jt-theme-change", emit);
  // 历史行为变更（t_76975559，2026-11-25）：此处原有 matchMedia("(prefers-color-scheme: dark)")
  // 监听，auto 态下跟随系统深色模式切换。现 auto 态改为按本地时间驱动
  // （18:00–6:00 暗色，其余亮色；手动 light/dark 优先），系统主题变化不再驱动切换——
  // 时间驱动逻辑收敛在各模板 theme-toggle 脚本的每分钟 setInterval 边界检查中
  // （仅 auto 态且跨小时边界才写 data-theme 并派发 jt-theme-change，图表重绘走本文件 emit 通道）。
})(window);
