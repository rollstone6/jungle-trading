function pad(value) {
  return String(value).padStart(2, "0");
}

function updateClock() {
  const clock = document.querySelector("[data-clock]");
  if (!clock) return;
  const now = new Date();
  const cn = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Shanghai" }));
  const text = `${cn.getFullYear()}-${pad(cn.getMonth() + 1)}-${pad(cn.getDate())} ${pad(cn.getHours())}:${pad(cn.getMinutes())}:${pad(cn.getSeconds())}`;
  clock.textContent = text;
}

updateClock();
window.setInterval(updateClock, 1000);

function showToast(message) {
  const toast = document.querySelector("#toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 3600);
}

function apiUrl(path) {
  const url = new URL(path, window.location.origin);
  const pwd = new URLSearchParams(window.location.search).get("pwd");
  if (pwd) url.searchParams.set("pwd", pwd);
  return url.toString();
}

function createPositionRow() {
  const row = document.createElement("div");
  row.className = "manual-row";
  row.dataset.positionRow = "";
  row.innerHTML = `
    <input name="code" inputmode="numeric" maxlength="6" value="" aria-label="股票代码" />
    <input name="quantity" inputmode="decimal" value="" aria-label="持仓数量" />
    <input name="cost_price" inputmode="decimal" value="" aria-label="成本价格" />
    <button type="button" class="icon-button" data-remove-row title="删除这一行">×</button>
  `;
  return row;
}

function readDecimal(value) {
  const cleaned = String(value || "").replace(/,/g, "").trim();
  if (!cleaned) return null;
  const parsed = Number.parseFloat(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function collectManualPayload(form) {
  const positions = [];
  form.querySelectorAll("[data-position-row]").forEach((row) => {
    const code = row.querySelector('input[name="code"]').value.trim();
    const quantity = readDecimal(row.querySelector('input[name="quantity"]').value);
    const costPrice = readDecimal(row.querySelector('input[name="cost_price"]').value);
    if (!code && quantity === null && costPrice === null) return;
    if (!/^\d{6}$/.test(code)) throw new Error(`股票代码格式错误：${code || "空"}`);
    if (!quantity || quantity <= 0) throw new Error(`${code} 的持仓数量必须大于 0`);
    if (!costPrice || costPrice <= 0) throw new Error(`${code} 的成本价格必须大于 0`);
    positions.push({ code, quantity, cost_price: costPrice });
  });
  if (!positions.length) throw new Error("至少需要输入一条持仓。");
  return {
    account: {
      principal: readDecimal(form.elements.principal.value),
      total_asset: readDecimal(form.elements.total_asset.value),
    },
    positions,
  };
}

function bindManualPositionForm() {
  const form = document.querySelector("[data-manual-position-form]");
  if (!form) return;
  const modal = document.querySelector("[data-position-modal]");
  const openButton = document.querySelector("[data-open-position-modal]");
  const closeButton = document.querySelector("[data-close-position-modal]");
  const rows = form.querySelector("[data-position-rows]");
  const addButton = form.querySelector("[data-add-position-row]");

  function openModal() {
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add("modal-open");
    const firstInput = modal.querySelector("input");
    if (firstInput) firstInput.focus();
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  if (openButton) openButton.addEventListener("click", openModal);
  if (closeButton) closeButton.addEventListener("click", closeModal);
  if (modal) {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal();
    });
  }
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && modal && !modal.hidden) closeModal();
  });

  addButton.addEventListener("click", () => {
    rows.appendChild(createPositionRow());
    const last = rows.querySelector("[data-position-row]:last-child input");
    if (last) last.focus();
  });

  form.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-row]");
    if (!removeButton) return;
    const row = removeButton.closest("[data-position-row]");
    if (!row) return;
    if (rows.querySelectorAll("[data-position-row]").length <= 1) {
      row.querySelectorAll("input").forEach((input) => {
        input.value = "";
      });
      return;
    }
    row.remove();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    try {
      const payload = collectManualPayload(form);
      submit.disabled = true;
      submit.textContent = "更新中";
      showToast("正在更新持仓并刷新行情...");
      const response = await fetch(apiUrl("/api/positions/manual"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "持仓更新失败。");
      showToast(result.message || "持仓已更新。");
      window.setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      showToast(error.message || "持仓更新失败，请检查输入。");
    } finally {
      submit.disabled = false;
      submit.textContent = "确定更新";
    }
  });
}

bindManualPositionForm();

function formatCnDateTime(value) {
  if (!value) return "暂无同步时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const cn = new Date(date.toLocaleString("en-US", { timeZone: "Asia/Shanghai" }));
  return `${cn.getFullYear()}-${pad(cn.getMonth() + 1)}-${pad(cn.getDate())} ${pad(cn.getHours())}:${pad(cn.getMinutes())}:${pad(cn.getSeconds())}`;
}

function bindReportDatePicker() {
  const control = document.querySelector("[data-report-date-control]");
  const calendar = document.querySelector("[data-report-calendar]");
  const content = document.querySelector("[data-ai-report-content]");
  const timeLabel = document.querySelector("[data-ai-report-time]");
  const button = document.querySelector("[data-open-report-date]");
  if (!control || !calendar || !content || !button) return;

  const availableDates = new Set(
    (control.dataset.availableDates || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );
  let activeDate = parseIsoDate(control.dataset.currentDate) || new Date();
  let activeMonth = new Date(activeDate.getFullYear(), activeDate.getMonth(), 1);

  function parseIsoDate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  }

  function isoDate(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  }

  function renderCalendar() {
    const year = activeMonth.getFullYear();
    const month = activeMonth.getMonth();
    const firstDay = new Date(year, month, 1);
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const offset = firstDay.getDay();
    const cells = [];
    for (let index = 0; index < offset; index += 1) {
      cells.push('<span class="calendar-empty"></span>');
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      const date = new Date(year, month, day);
      const key = isoDate(date);
      const hasReport = availableDates.has(key);
      const isActive = key === control.dataset.currentDate;
      cells.push(
        `<button type="button" class="${hasReport ? "has-report" : ""} ${isActive ? "active" : ""}" data-report-day="${key}" ${hasReport ? "" : "disabled"}>${day}</button>`,
      );
    }
    calendar.innerHTML = `
      <div class="calendar-head">
        <button type="button" data-calendar-prev aria-label="上个月">‹</button>
        <strong>${year}.${pad(month + 1)}</strong>
        <button type="button" data-calendar-next aria-label="下个月">›</button>
      </div>
      <div class="calendar-week">
        <span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span>
      </div>
      <div class="calendar-grid">${cells.join("")}</div>
    `;
  }

  function placeCalendar() {
    const rect = button.getBoundingClientRect();
    const width = Math.min(236, window.innerWidth - 16);
    const left = clamp(rect.right - width, 8, window.innerWidth - width - 8);
    const top = clamp(rect.bottom + 8, 8, window.innerHeight - 286);
    calendar.style.width = `${width}px`;
    calendar.style.left = `${left}px`;
    calendar.style.top = `${top}px`;
  }

  async function loadReport(dateKey) {
    if (!dateKey) return;
    content.classList.add("loading");
    showToast("正在切换 AI 策略报告...");
    try {
      const response = await fetch(apiUrl(`/api/reports/strategies?date=${encodeURIComponent(dateKey)}`));
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "报告读取失败。");
      if (!payload.combined) throw new Error("当天没有 AI 报告。");
      content.innerHTML = payload.combined;
      control.dataset.currentDate = dateKey;
      activeDate = parseIsoDate(dateKey) || activeDate;
      if (timeLabel) {
        timeLabel.textContent = formatCnDateTime(payload.updated_at);
      }
      renderCalendar();
      showToast("AI 策略已切换。");
    } catch (error) {
      showToast(error.message || "报告切换失败。");
    } finally {
      content.classList.remove("loading");
    }
  }

  renderCalendar();

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    calendar.hidden = !calendar.hidden;
    if (!calendar.hidden) placeCalendar();
  });

  calendar.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.target.closest("[data-calendar-prev]")) {
      activeMonth = new Date(activeMonth.getFullYear(), activeMonth.getMonth() - 1, 1);
      renderCalendar();
      return;
    }
    if (event.target.closest("[data-calendar-next]")) {
      activeMonth = new Date(activeMonth.getFullYear(), activeMonth.getMonth() + 1, 1);
      renderCalendar();
      return;
    }
    const dayButton = event.target.closest("[data-report-day]");
    if (!dayButton || dayButton.disabled) return;
    calendar.hidden = true;
    loadReport(dayButton.dataset.reportDay);
  });

  document.addEventListener("click", (event) => {
    if (!control.contains(event.target)) calendar.hidden = true;
  });

  window.addEventListener("resize", () => {
    if (!calendar.hidden) placeCalendar();
  });

  window.addEventListener("scroll", () => {
    if (!calendar.hidden) placeCalendar();
  }, { passive: true });
}

bindReportDatePicker();

const PANEL_STATE_KEY = "jungle.panelState.v1";
const SCROLL_STATE_KEY = "jungle.scrollY.v1";

function readPanelState() {
  try {
    return JSON.parse(window.localStorage.getItem(PANEL_STATE_KEY) || "{}");
  } catch (_) {
    return {};
  }
}

function bindCollapsiblePanelState() {
  const panels = [...document.querySelectorAll("[data-panel-key]")];
  if (!panels.length) return;
  const state = readPanelState();
  panels.forEach((panel) => {
    const key = panel.dataset.panelKey;
    if (Object.prototype.hasOwnProperty.call(state, key)) {
      panel.open = Boolean(state[key]);
    }
    panel.addEventListener("toggle", () => {
      const nextState = readPanelState();
      nextState[key] = panel.open;
      window.localStorage.setItem(PANEL_STATE_KEY, JSON.stringify(nextState));
    });
  });

  const savedScroll = Number.parseInt(window.sessionStorage.getItem(SCROLL_STATE_KEY) || "", 10);
  if (Number.isFinite(savedScroll)) {
    window.sessionStorage.removeItem(SCROLL_STATE_KEY);
    window.requestAnimationFrame(() => window.scrollTo({ top: savedScroll, behavior: "auto" }));
  }
}

bindCollapsiblePanelState();

function priceY(value, min, max, top, height) {
  if (max <= min) return top + height / 2;
  return top + ((max - value) / (max - min)) * height;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function fmtPrice(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "--";
}

function fmtPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function readKlinePoints(el) {
  let points = [];
  try {
    points = JSON.parse(el.dataset.kline || "[]");
  } catch (_) {
    points = [];
  }
  return points
    .map((item) => ({
      date: item.date,
      open: Number(item.open),
      high: Number(item.high),
      low: Number(item.low),
      close: Number(item.close),
    }))
    .filter(
      (item) =>
        item.date &&
        Number.isFinite(item.open) &&
        Number.isFinite(item.high) &&
        Number.isFinite(item.low) &&
        Number.isFinite(item.close),
    );
}

function ensureKlineState(el) {
  if (el._klineState) return el._klineState;
  const points = readKlinePoints(el);
  const visible = Math.min(points.length, 60);
  el._klineState = {
    points,
    visible,
    start: Math.max(0, points.length - visible),
    selected: points.length ? points.length - 1 : null,
    pointers: new Map(),
    drag: null,
    pinch: null,
    hadMultiTouch: false,
  };
  return el._klineState;
}

function klineGeometry(el, state) {
  const width = Math.max(300, Math.floor(el.clientWidth || 320));
  const height = 120;
  const pad = { top: 8, right: 6, bottom: 20, left: 36 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const visible = clamp(state.visible || 20, Math.min(5, state.points.length || 5), state.points.length || 5);
  const start = clamp(state.start || 0, 0, Math.max(0, state.points.length - visible));
  return { width, height, pad, plotW, plotH, visible, start };
}

function renderKlineChart(el) {
  const state = ensureKlineState(el);
  const points = state.points;
  if (!points.length) {
    el.innerHTML = '<p class="kline-empty">暂无K线数据</p>';
    return;
  }

  const geom = klineGeometry(el, state);
  state.visible = geom.visible;
  state.start = geom.start;

  const { width, height, pad, plotW, plotH } = geom;
  const view = points.slice(state.start, state.start + state.visible);
  const lows = view.map((item) => item.low);
  const highs = view.map((item) => item.high);
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const range = max - min || Math.max(max * 0.02, 1);
  const minY = min - range * 0.08;
  const maxY = max + range * 0.08;
  const step = plotW / Math.max(view.length, 1);
  const candleW = Math.max(2, Math.min(8, step * 0.7));
  const last = view[view.length - 1];

  const grid = [0, 0.5, 1]
    .map((ratio) => {
      const y = pad.top + plotH * ratio;
      const value = maxY - (maxY - minY) * ratio;
      return `<line x1="${pad.left}" y1="${y.toFixed(1)}" x2="${width - pad.right}" y2="${y.toFixed(1)}" class="k-grid"/><text x="4" y="${(y + 4).toFixed(1)}" class="k-axis">${value.toFixed(2)}</text>`;
    })
    .join("");

  const candles = view
    .map((item, index) => {
      const x = pad.left + step * index + step / 2;
      const openY = priceY(item.open, minY, maxY, pad.top, plotH);
      const closeY = priceY(item.close, minY, maxY, pad.top, plotH);
      const highY = priceY(item.high, minY, maxY, pad.top, plotH);
      const lowY = priceY(item.low, minY, maxY, pad.top, plotH);
      const up = item.close >= item.open;
      const cls = up ? "k-up" : "k-down";
      const bodyY = Math.min(openY, closeY);
      const bodyH = Math.max(2, Math.abs(closeY - openY));
      return `<g class="${cls}">
        <line x1="${x.toFixed(1)}" y1="${highY.toFixed(1)}" x2="${x.toFixed(1)}" y2="${lowY.toFixed(1)}"/>
        <rect x="${(x - candleW / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyH.toFixed(1)}" rx="1"/>
      </g>`;
    })
    .join("");

  const selectedIndex = state.selected === null ? -1 : state.selected - state.start;
  const selected = selectedIndex >= 0 && selectedIndex < view.length ? view[selectedIndex] : last;
  const selectedLocal = selectedIndex >= 0 && selectedIndex < view.length ? selectedIndex : view.length - 1;
  const selectedGlobal = state.start + selectedLocal;
  const previous = points[selectedGlobal - 1];
  const changePercent = previous && previous.close ? ((selected.close - previous.close) / previous.close) * 100 : null;
  const changeClass = changePercent === null ? "" : changePercent >= 0 ? "gain" : "loss";
  const selectedX = pad.left + step * selectedLocal + step / 2;
  const selectedY = priceY(selected.close, minY, maxY, pad.top, plotH);
  const lastY = priceY(last.close, minY, maxY, pad.top, plotH);

  el.innerHTML = `
    <div class="kline-tip">
      <strong>${selected.date}</strong>
      <span class="${changeClass}">涨幅 ${fmtPercent(changePercent)}</span>
      <span>开 ${fmtPrice(selected.open)}</span>
      <span>收 ${fmtPrice(selected.close)}</span>
      <span>高 ${fmtPrice(selected.high)}</span>
      <span>低 ${fmtPrice(selected.low)}</span>
    </div>
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="近期K线图">
      <rect x="0" y="0" width="${width}" height="${height}" class="k-bg"/>
      ${grid}
      ${candles}
      <line x1="${pad.left}" y1="${lastY.toFixed(1)}" x2="${width - pad.right}" y2="${lastY.toFixed(1)}" class="k-last"/>
      <text x="${width - pad.right - 4}" y="${(lastY - 5).toFixed(1)}" text-anchor="end" class="k-last-label">${last.close.toFixed(2)}</text>
      <line x1="${selectedX.toFixed(1)}" y1="${pad.top}" x2="${selectedX.toFixed(1)}" y2="${height - pad.bottom}" class="k-select"/>
      <circle cx="${selectedX.toFixed(1)}" cy="${selectedY.toFixed(1)}" r="3.2" class="k-selected-dot"/>
      <text x="${pad.left}" y="${height - 7}" class="k-date">${view[0].date}</text>
      <text x="${width - pad.right}" y="${height - 7}" text-anchor="end" class="k-date">${last.date}</text>
    </svg>
  `;
}

function pointerDistance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function pointerCenterRatio(el, a, b) {
  const rect = el.getBoundingClientRect();
  const x = (a.x + b.x) / 2 - rect.left;
  const geom = klineGeometry(el, ensureKlineState(el));
  return clamp((x - geom.pad.left) / Math.max(geom.plotW, 1), 0, 1);
}

function setKlineVisibleAround(el, visible, ratio) {
  const state = ensureKlineState(el);
  const oldVisible = state.visible;
  const center = state.start + oldVisible * ratio;
  state.visible = clamp(visible, Math.min(5, state.points.length), state.points.length);
  state.start = clamp(Math.round(center - state.visible * ratio), 0, Math.max(0, state.points.length - state.visible));
  renderKlineChart(el);
}

function selectKlineAt(el, clientX) {
  const state = ensureKlineState(el);
  const geom = klineGeometry(el, state);
  const rect = el.getBoundingClientRect();
  const x = clientX - rect.left;
  const local = clamp(Math.floor(((x - geom.pad.left) / Math.max(geom.plotW, 1)) * geom.visible), 0, geom.visible - 1);
  state.selected = clamp(state.start + local, 0, state.points.length - 1);
  renderKlineChart(el);
}

function bindKlineInteraction(el) {
  if (el._klineBound) return;
  el._klineBound = true;
  const state = ensureKlineState(el);

  el.addEventListener("pointerdown", (event) => {
    if (!state.points.length) return;
    el.setPointerCapture?.(event.pointerId);
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    state.hadMultiTouch = state.pointers.size > 1;
    state.drag = { x: event.clientX, y: event.clientY, start: state.start, moved: false };
    if (state.pointers.size === 2) {
      const [a, b] = [...state.pointers.values()];
      state.pinch = {
        distance: pointerDistance(a, b),
        visible: state.visible,
        ratio: pointerCenterRatio(el, a, b),
      };
    }
  });

  el.addEventListener("pointermove", (event) => {
    if (!state.pointers.has(event.pointerId)) return;
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (state.pointers.size >= 2 && state.pinch) {
      const [a, b] = [...state.pointers.values()];
      const distance = pointerDistance(a, b);
      if (distance > 0 && state.pinch.distance > 0) {
        const ratio = distance / state.pinch.distance;
        const nextVisible = Math.round(state.pinch.visible / ratio);
        state.hadMultiTouch = true;
        setKlineVisibleAround(el, nextVisible, state.pinch.ratio);
      }
      return;
    }

    if (!state.drag) return;
    const dx = event.clientX - state.drag.x;
    const dy = event.clientY - state.drag.y;
    if (Math.abs(dx) > 5 || Math.abs(dy) > 5) state.drag.moved = true;
    const geom = klineGeometry(el, state);
    const candleStep = geom.plotW / Math.max(geom.visible, 1);
    const delta = Math.round(-dx / Math.max(candleStep, 1));
    const nextStart = clamp(state.drag.start + delta, 0, Math.max(0, state.points.length - state.visible));
    if (nextStart !== state.start) {
      state.start = nextStart;
      renderKlineChart(el);
    }
  });

  const releasePointer = (event) => {
    const wasTap = state.drag && !state.drag.moved && !state.hadMultiTouch && state.pointers.size <= 1;
    if (wasTap) selectKlineAt(el, event.clientX);
    state.pointers.delete(event.pointerId);
    state.drag = null;
    state.pinch = null;
    if (state.pointers.size < 2) state.hadMultiTouch = false;
  };

  el.addEventListener("pointerup", releasePointer);
  el.addEventListener("pointercancel", (event) => {
    state.pointers.delete(event.pointerId);
    state.drag = null;
    state.pinch = null;
    state.hadMultiTouch = false;
  });

  el.addEventListener(
    "wheel",
    (event) => {
      if (!state.points.length) return;
      event.preventDefault();
      const geom = klineGeometry(el, state);
      const rect = el.getBoundingClientRect();
      const ratio = clamp((event.clientX - rect.left - geom.pad.left) / Math.max(geom.plotW, 1), 0, 1);
      const factor = event.deltaY > 0 ? 1.18 : 0.84;
      setKlineVisibleAround(el, Math.round(state.visible * factor), ratio);
    },
    { passive: false },
  );
}

function bindKlineCharts() {
  const charts = [...document.querySelectorAll(".mini-kline")];
  if (!charts.length) return;
  charts.forEach((chart) => {
    bindKlineInteraction(chart);
    renderKlineChart(chart);
  });
  window.addEventListener("resize", () => charts.forEach(renderKlineChart));
}

bindKlineCharts();

// ETF资金流向
async function loadETFFlow() {
    try {
        const response = await fetch(apiUrl('/api/etf-moneyflow'));
        const data = await response.json();

        if (!data.items || data.items.length === 0) {
            document.getElementById('etf-flow-list').innerHTML = '<div class="etf-loading">暂无数据</div>';
            return;
        }

        // 更新时间
        const now = new Date();
        document.getElementById('etf-update-time').textContent = `更新于 ${now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}`;

        // 计算汇总数据
        const totalInflow = data.items.reduce((sum, item) => sum + (item.main_net_inflow || 0), 0);
        const inflowCount = data.items.filter(item => item.main_net_inflow > 0).length;
        const outflowCount = data.items.filter(item => item.main_net_inflow < 0).length;
        const topInflow = data.items.reduce((max, item) => item.main_net_inflow > max.main_net_inflow ? item : max, data.items[0]);

        // 渲染汇总
        const summaryEl = document.getElementById('etf-summary');
        summaryEl.innerHTML = `
            <div class="etf-summary-card">
                <div class="label">TOP15主力净流入</div>
                <div class="value ${totalInflow > 0 ? 'positive' : 'negative'}">${formatMoney(totalInflow)}</div>
            </div>
            <div class="etf-summary-card">
                <div class="label">流入/流出</div>
                <div class="value">${inflowCount} / ${outflowCount}</div>
            </div>
            <div class="etf-summary-card">
                <div class="label">最强流入</div>
                <div class="value positive" style="font-size: 13px;">${topInflow.name.substring(0, 8)}</div>
            </div>
        `;

        // 渲染列表
        const listEl = document.getElementById('etf-flow-list');
        listEl.innerHTML = data.items.map(item => {
            const changeClass = item.change_pct > 0 ? 'gain' : item.change_pct < 0 ? 'loss' : 'neutral';
            const inflowClass = item.main_net_inflow > 0 ? 'positive' : 'negative';
            const inflowText = formatMoney(item.main_net_inflow);

            return `
                <div class="etf-item">
                    <div>
                        <div class="etf-item-name">${item.name}</div>
                        <div class="etf-item-code">${item.code}</div>
                    </div>
                    <div class="etf-item-price">${item.price.toFixed(3)}</div>
                    <div class="etf-item-change ${changeClass}">
                        ${item.change_pct > 0 ? '+' : ''}${item.change_pct.toFixed(2)}%
                    </div>
                    <div class="etf-item-inflow ${inflowClass}">
                        ${item.main_net_inflow > 0 ? '+' : ''}${inflowText}
                    </div>
                </div>
            `;
        }).join('');
    } catch (err) {
        console.error('加载ETF资金流向失败:', err);
        document.getElementById('etf-flow-list').innerHTML = '<div class="etf-loading">加载失败，请刷新重试</div>';
    }
}

function formatMoney(value) {
    const abs = Math.abs(value);
    if (abs >= 100000000) {
        return (value / 100000000).toFixed(2) + '亿';
    } else if (abs >= 10000) {
        return (value / 10000).toFixed(2) + '万';
    } else {
        return value.toFixed(2);
    }
}

// 页面加载时获取ETF数据
loadETFFlow();

function isCnTradingRefreshWindow() {
  const now = new Date();
  const cn = new Date(now.toLocaleString("en-US", { timeZone: "Asia/Shanghai" }));
  const day = cn.getDay();
  if (day === 0 || day === 6) return false;

  const minutes = cn.getHours() * 60 + cn.getMinutes();
  const morningOpen = 9 * 60 + 30;
  const middayRefresh = 11 * 60 + 31;
  const afternoonOpen = 13 * 60;
  const afterCloseRefresh = 15 * 60 + 31;
  return (minutes >= morningOpen && minutes <= middayRefresh) || (minutes >= afternoonOpen && minutes <= afterCloseRefresh);
}

function bindAutoRefresh() {
  window.setInterval(() => {
    if (isCnTradingRefreshWindow()) {
      window.sessionStorage.setItem(SCROLL_STATE_KEY, String(window.scrollY || 0));
      window.location.reload();
    }
  }, 15 * 60 * 1000);
}

bindAutoRefresh();
