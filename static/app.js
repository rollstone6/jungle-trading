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

function readDecimal(value) {
  const cleaned = String(value || "").replace(/,/g, "").trim();
  if (!cleaned) return null;
  const parsed = Number.parseFloat(cleaned);
  return Number.isFinite(parsed) ? parsed : null;
}

function pmEsc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function bindManualPositionForm() {
  const form = document.querySelector("[data-manual-position-form]");
  if (!form) return;
  const modal = document.querySelector("[data-position-modal]");
  const rows = form.querySelector("[data-position-rows]");
  const addButton = form.querySelector("[data-add-position-row]");
  const searchInput = form.querySelector("[data-pm-search-input]");
  const suggestBox = form.querySelector("[data-pm-suggest]");
  const poolBox = form.querySelector("[data-pm-pool]");
  const changesBox = form.querySelector("[data-pm-changes]");
  const submitBtn = form.querySelector("[data-pm-submit]");
  const draftBar = form.querySelector("[data-pm-draft]");

  // 服务端注入的当前持仓/账户/股票池
  const EDIT = (() => {
    try { return JSON.parse(document.getElementById("position-edit-data").textContent); }
    catch (e) { return { account: {}, positions: [], pool: [] }; }
  })();
  const ORIGINAL = new Map(EDIT.positions.map((p) => [p.code, p]));
  const POOL = EDIT.pool || [];
  const DRAFT_KEY = "jungle-pm-draft-v1";

  let searchTimer = null;
  let searchSeq = 0;

  // ---- 行构造 ----
  function createPositionRow(item = {}) {
    const row = document.createElement("div");
    row.className = "manual-row";
    row.dataset.positionRow = "";
    const manual = item.manual === true || !item.code;
    const qtyVal = item.quantity > 0 ? item.quantity : "";
    const costVal = item.cost_price > 0 ? item.cost_price : "";
    const canFill = !costVal && item.latest_price > 0;
    row.innerHTML = `
      <div class="pm-id">${manual
        ? `<input name="code" class="pm-code" inputmode="numeric" maxlength="6" pattern="\\d{6}" placeholder="6位代码" aria-label="股票代码" value="${pmEsc(item.code || "")}" />`
        : `<strong>${pmEsc(item.name || "?")}</strong><small>${pmEsc(item.code)}</small>`}</div>
      <input name="quantity" class="pm-qty" inputmode="numeric" value="${qtyVal}" placeholder="数量" aria-label="${pmEsc(item.name || item.code || "该行")} 持仓数量" />
      <div class="pm-costwrap">
        <input name="cost_price" class="pm-cost" inputmode="decimal" value="${costVal}" placeholder="成本价" aria-label="${pmEsc(item.name || item.code || "该行")} 成本价格" />
        ${canFill ? `<button type="button" class="pm-fill" data-fill-cost="${item.latest_price}" title="按最新价 ${item.latest_price} 填入">现价</button>` : ""}
      </div>
      <button type="button" class="icon-button" data-remove-row title="清仓（从持仓移除）" aria-label="清仓 ${pmEsc(item.name || item.code || "")}"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12 M18 6L6 18"/></svg></button>
    `;
    if (!manual) {
      row.dataset.code = item.code;
      if (item.name) row.dataset.name = item.name;
      if (item.latest_price) row.dataset.latest = item.latest_price;
    }
    return row;
  }

  function existingCodes() {
    return new Set(
      [...rows.querySelectorAll("[data-position-row][data-code]")].map((r) => r.dataset.code)
    );
  }

  function addPosition(item) {
    if (existingCodes().has(item.code)) {
      showToast(`${item.name || item.code} 已在列表中`);
      return;
    }
    rows.appendChild(createPositionRow(item));
    syncPoolChips();
    updateChanges();
    saveDraft();
  }

  // ---- 快捷股票池 ----
  function renderPool() {
    if (!poolBox || !POOL.length) return;
    poolBox.innerHTML = POOL.map((s) =>
      `<button type="button" class="pm-pool-chip" data-pool-code="${s.code}" data-pool-name="${pmEsc(s.name)}">${pmEsc(s.name)}</button>`
    ).join("");
  }

  function syncPoolChips() {
    const held = existingCodes();
    poolBox.querySelectorAll("[data-pool-code]").forEach((chip) => {
      const on = held.has(chip.dataset.poolCode);
      chip.classList.toggle("on", on);
      chip.setAttribute("aria-pressed", String(on));
    });
  }

  poolBox.addEventListener("click", (event) => {
    const chip = event.target.closest("[data-pool-code]");
    if (!chip) return;
    if (chip.classList.contains("on")) return; // 已在列表，不重复加
    addPosition({ code: chip.dataset.poolCode, name: chip.dataset.poolName, quantity: 0, cost_price: 0 });
  });

  // ---- 搜索选股（名称/代码/拼音首字母） ----
  function closeSuggest() {
    if (suggestBox) { suggestBox.hidden = true; suggestBox.innerHTML = ""; }
  }

  function renderSuggest(results) {
    if (!results.length) {
      suggestBox.innerHTML = '<div class="pm-suggest-empty">未找到匹配的股票</div>';
    } else {
      suggestBox.innerHTML = results.map((r) => {
        const tag = ORIGINAL.has(r.code) ? "已持仓" : (POOL.some((s) => s.code === r.code) ? "股票池" : "池外");
        return `<div class="pm-suggest-item" role="button" tabindex="0" data-code="${r.code}" data-name="${pmEsc(r.name)}">
          <span class="pm-s-name">${pmEsc(r.name)}</span>
          <span class="pm-s-code">${r.code}</span>
          <span class="pm-s-tag ${tag === "已持仓" ? "held" : ""}">${tag}</span>
        </div>`;
      }).join("");
    }
    suggestBox.hidden = false;
  }

  async function runSearch(q) {
    const seq = ++searchSeq;
    try {
      const resp = await fetch(apiUrl(`/api/backtest/search?q=${encodeURIComponent(q)}`));
      if (!resp.ok) throw new Error();
      const data = await resp.json();
      if (seq !== searchSeq) return; // 丢弃过期响应
      renderSuggest(data.results || []);
    } catch (e) {
      if (seq !== searchSeq) return;
      // suggest 无结果且是 6 位代码 → lookup 兜底
      if (/^\d{6}$/.test(q)) {
        try {
          const r = await fetch(apiUrl(`/api/backtest/lookup?code=${q}`));
          if (r.ok && seq === searchSeq) {
            const info = await r.json();
            renderSuggest([{ code: info.code, name: info.name }]);
            return;
          }
        } catch (e2) { /* 忽略 */ }
      }
      if (seq === searchSeq) closeSuggest();
    }
  }

  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = searchInput.value.trim();
    if (!q) { closeSuggest(); return; }
    searchTimer = setTimeout(() => runSearch(q), 250);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSuggest();
  });

  function pickSuggest(item) {
    addPosition({ code: item.dataset.code, name: item.dataset.name, quantity: 0, cost_price: 0 });
    searchInput.value = "";
    closeSuggest();
  }

  suggestBox.addEventListener("mousedown", (event) => {
    const item = event.target.closest("[data-code]");
    if (!item) return;
    event.preventDefault(); // 防 blur 先关闭
    pickSuggest(item);
  });
  suggestBox.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    const item = event.target.closest("[data-code]");
    if (item) pickSuggest(item);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-pm-search]")) closeSuggest();
  });

  // ---- 变更摘要（新增/修改/清仓） ----
  function collectRows() {
    const out = [];
    rows.querySelectorAll("[data-position-row]").forEach((row) => {
      const codeInput = row.querySelector(".pm-code");
      const qtyRaw = row.querySelector(".pm-qty").value.trim();
      const costRaw = row.querySelector(".pm-cost").value.trim();
      const code = (codeInput ? codeInput.value : row.dataset.code || "").trim();
      // 全空的手动行直接忽略（不拦提交）
      if (codeInput && !code && !qtyRaw && !costRaw) return;
      out.push({ row, code, qty: readDecimal(qtyRaw), cost: readDecimal(costRaw), name: row.dataset.name || "" });
    });
    return out;
  }

  function validate(items) {
    const seen = new Set();
    for (const it of items) {
      if (!it.code) { it.row.classList.add("pm-bad"); return "有行缺少股票代码"; }
      if (!/^\d{6}$/.test(it.code)) { it.row.classList.add("pm-bad"); return `代码「${it.code}」不是 6 位数字`; }
      if (seen.has(it.code)) { it.row.classList.add("pm-bad"); return `${it.code} 重复出现`; }
      seen.add(it.code);
      if (it.qty !== null && it.qty < 0) { it.row.classList.add("pm-bad"); return `${it.code} 数量不能为负`; }
      if (it.cost !== null && it.cost < 0) { it.row.classList.add("pm-bad"); return `${it.code} 成本不能为负`; }
    }
    return null;
  }

  function updateChanges() {
    rows.querySelectorAll(".pm-bad").forEach((r) => r.classList.remove("pm-bad"));
    const items = collectRows();
    if (!items.length) {
      changesBox.innerHTML = '<span class="pm-change-warn">列表为空：保存将清空全部持仓，请确认这确实是清仓操作。</span>';
      submitBtn.textContent = "保存（清仓全部）";
      submitBtn.disabled = false;
      syncPoolChips();
      return;
    }
    const err = validate(items);
    if (err) {
      changesBox.innerHTML = `<span class="pm-change-warn">${pmEsc(err)}</span>`;
      submitBtn.disabled = true;
      syncPoolChips();
      return;
    }
    submitBtn.disabled = false;
    const newCodes = new Set(items.map((i) => i.code));
    const added = items.filter((i) => !ORIGINAL.has(i.code));
    const modified = items.filter((i) => {
      const o = ORIGINAL.get(i.code);
      if (!o) return false;
      const q = i.qty === null ? 0 : i.qty;
      const c = i.cost === null ? 0 : i.cost;
      return q !== o.quantity || Math.abs(c - (o.cost_price || 0)) > 1e-6;
    });
    const removedNames = [...ORIGINAL.values()].filter((p) => !newCodes.has(p.code)).map((p) => p.name || p.code);
    const parts = [];
    if (added.length) parts.push(`<span class="pm-tag add">+ 新增 ${added.length}</span>`);
    if (modified.length) parts.push(`<span class="pm-tag mod">± 修改 ${modified.length}</span>`);
    if (removedNames.length) parts.push(`<span class="pm-tag del">− 清仓 ${removedNames.length}（${pmEsc(removedNames.join("、"))}）</span>`);
    changesBox.innerHTML = parts.length
      ? parts.join(" ")
      : '<span class="pm-change-none">无变更</span>';
    submitBtn.textContent = removedNames.length
      ? `保存变更（含清仓 ${removedNames.length} 只）`
      : "保存变更";
    syncPoolChips();
  }

  rows.addEventListener("input", (event) => {
    const row = event.target.closest("[data-position-row]");
    if (row) row.classList.remove("pm-bad");
    updateChanges();
    saveDraft();
  });

  // 「现价」一键回填
  rows.addEventListener("click", (event) => {
    const fill = event.target.closest("[data-fill-cost]");
    if (!fill) return;
    const input = fill.closest(".pm-costwrap").querySelector(".pm-cost");
    input.value = fill.dataset.fillCost;
    fill.remove();
    updateChanges();
    saveDraft();
  });

  // ---- 草稿 ----
  function rowSnapshot(row) {
    const codeInput = row.querySelector(".pm-code");
    return {
      manual: !!codeInput,
      code: codeInput ? codeInput.value.trim() : row.dataset.code,
      name: row.dataset.name || "",
      latest: row.dataset.latest || 0,
      qty: row.querySelector(".pm-qty").value,
      cost: row.querySelector(".pm-cost").value,
    };
  }

  function saveDraft() {
    try {
      const items = [...rows.querySelectorAll("[data-position-row]")].map(rowSnapshot)
        .filter((i) => i.code || i.qty || i.cost); // 忽略全空手动行
      if (!items.length || !draftDiffers(items)) { localStorage.removeItem(DRAFT_KEY); draftBar.hidden = true; return; }
      localStorage.setItem(DRAFT_KEY, JSON.stringify(items));
    } catch (e) { /* 配额/隐私模式忽略 */ }
  }

  function draftDiffers(items) {
    if (items.length !== ORIGINAL.size) return true;
    return items.some((i) => {
      if (i.manual) return true;
      const o = ORIGINAL.get(i.code);
      if (!o) return true;
      const q = (i.qty || "").trim();
      const c = (i.cost || "").trim();
      return (q ? Number(q) : 0) !== (o.quantity || 0) || (c ? Number(c) : 0) !== (o.cost_price || 0);
    });
  }

  function loadDraft() {
    let raw = null;
    try { raw = localStorage.getItem(DRAFT_KEY); } catch (e) { return; }
    if (!raw) return;
    let items;
    try { items = JSON.parse(raw); } catch (e) { localStorage.removeItem(DRAFT_KEY); return; }
    if (!Array.isArray(items) || !items.length) return;
    if (!draftDiffers(items)) { localStorage.removeItem(DRAFT_KEY); return; }
    draftBar.querySelector("[data-pm-draft-text]").textContent =
      `发现上次未保存的编辑（${items.length} 行），是否恢复？`;
    draftBar.hidden = false;
    draftBar.dataset.draft = raw;
  }

  form.querySelector("[data-draft-restore]").addEventListener("click", () => {
    try {
      const items = JSON.parse(draftBar.dataset.draft || "[]");
      rows.innerHTML = "";
      items.forEach((i) => rows.appendChild(createPositionRow({
        manual: i.manual,
        code: i.code,
        name: i.name || ORIGINAL.get(i.code)?.name || "",
        quantity: readDecimal(i.qty),
        cost_price: readDecimal(i.cost),
        latest_price: ORIGINAL.get(i.code)?.latest_price || i.latest || 0,
      })));
    } catch (e) { /* ignore */ }
    draftBar.hidden = true;
    updateChanges();
  });
  form.querySelector("[data-draft-discard]").addEventListener("click", () => {
    localStorage.removeItem(DRAFT_KEY);
    draftBar.hidden = true;
  });

  // ---- 打开/关闭 ----
  function fillServerState() {
    rows.innerHTML = "";
    ORIGINAL.forEach((p) => rows.appendChild(createPositionRow(p)));
    form.elements.principal.value = EDIT.account?.principal ?? "";
    form.elements.total_asset.value = EDIT.account?.total_asset ?? "";
    updateChanges();
  }

  function openModal() {
    if (!modal) return;
    fillServerState();
    loadDraft();
    modal.hidden = false;
    document.body.classList.add("modal-open");
    searchInput.focus();
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    closeSuggest();
  }

  document.querySelectorAll("[data-open-position-modal]").forEach((b) => b.addEventListener("click", openModal));
  form.querySelectorAll("[data-close-position-modal]").forEach((b) => b.addEventListener("click", closeModal));
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
    const inputs = rows.querySelectorAll("[data-position-row]");
    const last = inputs[inputs.length - 1];
    const codeInput = last.querySelector(".pm-code");
    if (codeInput) codeInput.focus();
    updateChanges();
  });

  rows.addEventListener("click", (event) => {
    const removeButton = event.target.closest("[data-remove-row]");
    if (!removeButton) return;
    const row = removeButton.closest("[data-position-row]");
    if (!row) return;
    row.remove();
    updateChanges();
    saveDraft();
  });

  // ---- 提交 ----
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const items = collectRows();
    const err = validate(items);
    if (err) { showToast(err); return; }
    const removals = [...ORIGINAL.values()].filter((p) => !items.some((i) => i.code === p.code));
    if (removals.length && !window.confirm(`将清仓 ${removals.length} 只：${removals.map((r) => r.name || r.code).join("、")}。确认保存？`)) return;
    const payload = {
      account: {
        principal: readDecimal(form.elements.principal.value),
        total_asset: readDecimal(form.elements.total_asset.value),
      },
      positions: items.map((i) => ({
        code: i.code, name: i.name,
        quantity: Math.max(i.qty === null ? 0 : Math.floor(i.qty), 0),
        cost_price: Math.max(i.cost === null ? 0 : i.cost, 0),
      })),
    };
    submitBtn.disabled = true;
    submitBtn.textContent = "保存中…";
    try {
      const response = await fetch(apiUrl("/api/positions/manual"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || "持仓更新失败。");
      localStorage.removeItem(DRAFT_KEY);
      showToast(result.message || "持仓已更新。");
      window.sessionStorage.setItem("jungle-scroll-restore", String(window.scrollY || 0));
      window.setTimeout(() => window.location.reload(), 1200);
    } catch (error) {
      showToast(error.message || "持仓更新失败，请检查输入。");
      submitBtn.disabled = false;
      submitBtn.textContent = "保存变更";
    }
  });

  renderPool();

  // 维护模式直达：/?update=1 自动打开弹窗（成本补录横幅链接）
  if (new URLSearchParams(window.location.search).get("update") === "1") openModal();
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
      volume: Number(item.volume) || 0,
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

// ===== K线主图指标：计算（纯前端，基于已加载的日K序列） =====
const K_MA_COLORS = { 5: "#111827", 10: "#8b5cf6", 20: "#f59e0b", 60: "#3b82f6", 120: "#a16207", 250: "#64748b" };
const K_MA_PERIODS = [5, 10, 20, 60, 120, 250];

function kSMA(points, period) {
  const out = new Array(points.length).fill(null);
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    sum += points[i].close;
    if (i >= period) sum -= points[i - period].close;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function kEMA(points, period) {
  const out = new Array(points.length).fill(null);
  if (!points.length) return out;
  const k = 2 / (period + 1);
  let prev = points[0].close;
  out[0] = prev;
  for (let i = 1; i < points.length; i++) {
    prev = points[i].close * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

function kSTDEV(points, period, maArr) {
  const out = new Array(points.length).fill(null);
  for (let i = period - 1; i < points.length; i++) {
    let s = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const d = points[j].close - maArr[i];
      s += d * d;
    }
    out[i] = Math.sqrt(s / period);
  }
  return out;
}

// 神奇九转（TD Setup 简化版）：连续9根收盘低于/高于4根前收盘 → 低九(看多)/高九(看空)
function kNineTurn(points) {
  const low = new Array(points.length).fill(0);
  const high = new Array(points.length).fill(0);
  let lc = 0, hc = 0;
  for (let i = 0; i < points.length; i++) {
    if (i < 4) { lc = 0; hc = 0; continue; }
    if (points[i].close < points[i - 4].close) { lc++; hc = 0; }
    else if (points[i].close > points[i - 4].close) { hc++; lc = 0; }
    else { lc = 0; hc = 0; }
    low[i] = lc; high[i] = hc;
  }
  return { low, high };
}

// 多空信号：MA5 上穿 MA20 → 多；下穿 → 空
function kCrossSignals(points) {
  const m5 = kSMA(points, 5), m20 = kSMA(points, 20);
  const marks = [];
  for (let i = 1; i < points.length; i++) {
    if (m5[i] == null || m20[i] == null || m5[i - 1] == null || m20[i - 1] == null) continue;
    const prev = m5[i - 1] - m20[i - 1], cur = m5[i] - m20[i];
    if (prev <= 0 && cur > 0) marks.push({ i, type: "B" });
    else if (prev >= 0 && cur < 0) marks.push({ i, type: "S" });
  }
  return marks;
}

// ===== 策略K线（复刻同花顺GS策略指标版思路，规则透明可回测） =====
// 状态机与回测引擎 MAStrategy 同口径：MA5×MA20 金叉转多持有、死叉转空观望。
// K线颜色按策略状态染色（而非当日涨跌）；B/S 标在状态切换点；
// T = 持仓期间偏离 MA5 过远的日内高抛低吸提示（正偏离高抛/负偏离低吸）。
function kStrategyState(points, fast = 5, slow = 20) {
  const mf = kSMA(points, fast), ms = kSMA(points, slow);
  const n = points.length;
  const state = new Array(n).fill(0); // 1=多头持有 0=空头观望
  const marks = [];
  let holding = false, entryIdx = -1;
  for (let i = 1; i < n; i++) {
    if (mf[i] == null || ms[i] == null || mf[i - 1] == null || ms[i - 1] == null) { state[i] = holding ? 1 : 0; continue; }
    const prev = mf[i - 1] - ms[i - 1], cur = mf[i] - ms[i];
    if (!holding && prev <= 0 && cur > 0) {
      holding = true; entryIdx = i; marks.push({ i, type: "B" });
    } else if (holding && prev >= 0 && cur < 0) {
      holding = false; marks.push({ i, type: "S" }); entryIdx = -1;
    }
    state[i] = holding ? 1 : 0;
    // T 提示：持仓中收盘偏离 MA5 ≥4%，且距入场/上个T ≥3根
    if (holding && mf[i] > 0 && i - entryIdx >= 3) {
      const dev = (points[i].close - mf[i]) / mf[i];
      const lastT = marks.length && marks[marks.length - 1].type === "T" ? marks[marks.length - 1].i : -99;
      if (Math.abs(dev) >= 0.04 && i - lastT >= 3) {
        marks.push({ i, type: "T", side: dev > 0 ? "high" : "low" });
      }
    }
  }
  return { state, marks };
}

// 主图指标线组：返回 [{name,color,values}]，values 与 points 等长（不足期=null）
function kIndicatorLines(points, mode) {
  const n = points.length;
  const lines = [];
  if (!n) return lines;
  if (mode === "ma") {
    for (const p of K_MA_PERIODS) lines.push({ name: `MA${p}`, color: K_MA_COLORS[p], values: kSMA(points, p) });
  } else if (mode === "boll") {
    const mid = kSMA(points, 20), sd = kSTDEV(points, 20, mid);
    const up = mid.map((v, i) => (v == null ? null : v + 2 * sd[i]));
    const dn = mid.map((v, i) => (v == null ? null : v - 2 * sd[i]));
    lines.push({ name: "上轨", color: "#ef4444", values: up });
    lines.push({ name: "中轨", color: "#f59e0b", values: mid });
    lines.push({ name: "下轨", color: "#22c55e", values: dn });
  } else if (mode === "ene") {
    const mid = kSMA(points, 25);
    const up = mid.map((v) => (v == null ? null : v * 1.06));
    const dn = mid.map((v) => (v == null ? null : v * 0.94));
    lines.push({ name: "上轨", color: "#ef4444", values: up });
    lines.push({ name: "中轨", color: "#f59e0b", values: mid });
    lines.push({ name: "下轨", color: "#22c55e", values: dn });
  } else if (mode === "expma") {
    lines.push({ name: "EMA12", color: "#111827", values: kEMA(points, 12) });
    lines.push({ name: "EMA50", color: "#f59e0b", values: kEMA(points, 50) });
  } else if (mode === "bullbear") {
    const m20 = kSMA(points, 20), m60 = kSMA(points, 60);
    const bb = m20.map((v, i) => (v == null || m60[i] == null ? null : (v + m60[i]) / 2));
    lines.push({ name: "牛熊线", color: "#8b5cf6", values: bb, width: 2 });
  }
  return lines;
}

// ===== K线设置（全局记忆，localStorage） =====
const K_SETTINGS_KEY = "jungle-kline-settings-v1";
const K_DEFAULTS = { indicator: "ma", form: "basic", nine: true, bs: false, chips: true };
let K_SETTINGS = (() => {
  try { return { ...K_DEFAULTS, ...JSON.parse(localStorage.getItem(K_SETTINGS_KEY) || "{}") }; }
  catch (_) { return { ...K_DEFAULTS }; }
})();
function saveKSettings() { try { localStorage.setItem(K_SETTINGS_KEY, JSON.stringify(K_SETTINGS)); } catch (_) {} }
function rerenderAllKlines() {
  document.querySelectorAll(".mini-kline").forEach((c) => {
    const card = c.closest(".kline-card");
    if (!card || !card.classList.contains("collapsed")) renderKlineChart(c);
  });
}

// 底部抽屉：K线主图快捷设置（仿同花顺交互，单例）
function openKlineSettings() {
  let sheet = document.getElementById("k-settings-sheet");
  if (!sheet) {
    sheet = document.createElement("div");
    sheet.id = "k-settings-sheet";
    sheet.className = "k-sheet-backdrop";
    sheet.innerHTML = `
      <div class="k-sheet" role="dialog" aria-modal="true" aria-label="K线主图快捷设置">
        <div class="k-sheet-head"><strong>K线主图快捷设置</strong><button type="button" class="k-sheet-close" data-k-close aria-label="关闭">×</button></div>
        <div class="k-sheet-body">
          <div class="k-sheet-group">主图指标</div>
          <div class="k-sheet-grid" data-k-indicators>
            <button type="button" data-k-ind="none">不显示指标</button>
            <button type="button" data-k-ind="ma">均线</button>
            <button type="button" data-k-ind="bullbear">牛熊线</button>
            <button type="button" data-k-ind="boll">BOLL</button>
            <button type="button" data-k-ind="ene">ENE</button>
            <button type="button" data-k-ind="expma">EXPMA</button>
          </div>
          <div class="k-sheet-group">K线形态</div>
          <div class="k-sheet-grid k-sheet-grid-2" data-k-forms>
            <button type="button" data-k-form="basic">基本K线</button>
            <button type="button" data-k-form="strategy">策略指标版</button>
          </div>
          <div class="k-sheet-note" data-k-form-note hidden>按 MA5×MA20 策略状态染色：红=多头持有、绿=空头观望；B买/S卖/T做T。与回测页均线策略同口径。</div>
          <div class="k-sheet-group">提示信号</div>
          <div class="k-sheet-toggle-row"><span>神奇九转 <i class="k-info" title="连续9根收盘低于/高于4根前收盘：低九看多、高九看空">i</i></span><button type="button" class="k-switch" data-k-toggle="nine" role="switch" aria-label="神奇九转"></button></div>
          <div class="k-sheet-toggle-row"><span>多空信号 <i class="k-info" title="MA5 上穿 MA20 提示「多」，下穿提示「空」">i</i></span><button type="button" class="k-switch" data-k-toggle="bs" role="switch" aria-label="多空信号"></button></div>
          <div class="k-sheet-toggle-row"><span>筹码分布 <i class="k-info" title="按成交量分布估算的持仓成本峰，铺在K线背后">i</i></span><button type="button" class="k-switch" data-k-toggle="chips" role="switch" aria-label="筹码分布"></button></div>
          <div class="k-sheet-foot">设置自动保存，对所有持仓K线生效</div>
        </div>
      </div>`;
    document.body.appendChild(sheet);
    sheet.addEventListener("click", (e) => { if (e.target === sheet) closeKlineSettings(); });
    sheet.querySelector("[data-k-close]").addEventListener("click", closeKlineSettings);
    sheet.querySelectorAll("[data-k-ind]").forEach((btn) => {
      btn.addEventListener("click", () => {
        K_SETTINGS.indicator = btn.dataset.kInd;
        saveKSettings(); syncKSettingsUI(); rerenderAllKlines();
      });
    });
    sheet.querySelectorAll("[data-k-form]").forEach((btn) => {
      btn.addEventListener("click", () => {
        K_SETTINGS.form = btn.dataset.kForm;
        saveKSettings(); syncKSettingsUI(); rerenderAllKlines();
      });
    });
    sheet.querySelectorAll("[data-k-toggle]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.kToggle;
        K_SETTINGS[key] = !K_SETTINGS[key];
        saveKSettings(); syncKSettingsUI(); rerenderAllKlines();
      });
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeKlineSettings(); });
  }
  syncKSettingsUI();
  sheet.classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeKlineSettings() {
  const sheet = document.getElementById("k-settings-sheet");
  if (sheet) sheet.classList.remove("open");
  document.body.style.overflow = "";
}
function syncKSettingsUI() {
  const sheet = document.getElementById("k-settings-sheet");
  if (!sheet) return;
  sheet.querySelectorAll("[data-k-ind]").forEach((b) => b.classList.toggle("active", b.dataset.kInd === K_SETTINGS.indicator));
  sheet.querySelectorAll("[data-k-form]").forEach((b) => b.classList.toggle("active", b.dataset.kForm === K_SETTINGS.form));
  const note = sheet.querySelector("[data-k-form-note]");
  if (note) note.hidden = K_SETTINGS.form !== "strategy";
  sheet.querySelectorAll("[data-k-toggle]").forEach((b) => {
    const on = !!K_SETTINGS[b.dataset.kToggle];
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
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
  const height = 176;
  const pad = { top: 8, right: 6, bottom: 20, left: 36 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const visible = clamp(state.visible || 20, Math.min(5, state.points.length || 5), state.points.length || 5);
  const start = clamp(state.start || 0, 0, Math.max(0, state.points.length - visible));
  return { width, height, pad, plotW, plotH, visible, start };
}

// 筹码分布（成本分布直方图）：把每根K线的量按三角权重摊到 [low, high] 的价格区间上
function calcChipDistribution(points, binCount) {
  const empty = { dist: [], max: 0, lo: 0, hi: 0, width: 0, peakPrice: 0, avgCost: 0 };
  if (!points || points.length < 5) return empty;
  let lo = Infinity, hi = -Infinity;
  for (const p of points) {
    if (p.low > 0) lo = Math.min(lo, p.low);
    if (p.high > 0) hi = Math.max(hi, p.high);
  }
  if (!(hi > lo) || !isFinite(lo) || !isFinite(hi)) return empty;
  const binW = (hi - lo) / binCount;
  const dist = new Array(binCount).fill(0);
  for (const p of points) {
    if (!p.volume || !(p.high > p.low)) continue;
    const center = (p.low + p.high + p.close) / 3; // 日内成交重心近似
    const half = Math.max((p.high - p.low) / 2, 1e-6);
    const i0 = clamp(Math.floor((p.low - lo) / binW), 0, binCount - 1);
    const i1 = clamp(Math.floor((p.high - lo) / binW), 0, binCount - 1);
    for (let i = i0; i <= i1; i++) {
      const bp = lo + (i + 0.5) * binW;
      const w = Math.max(0, 1 - Math.abs(bp - center) / half);
      dist[i] += p.volume * (w + 0.12); // 底部0.12保证区间边缘也有分布
    }
  }
  const max = Math.max(...dist);
  if (!(max > 0)) return empty;
  let peakPrice = 0, peakVal = -1, sumV = 0, sumVC = 0;
  for (let i = 0; i < binCount; i++) {
    const price = lo + (i + 0.5) * binW;
    sumV += dist[i];
    sumVC += dist[i] * price;
    if (dist[i] > peakVal) { peakVal = dist[i]; peakPrice = price; }
  }
  return { dist, max, lo, hi, width: binW, peakPrice, avgCost: sumV ? sumVC / sumV : 0 };
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

  const { width, pad, plotW, plotH } = geom;
  const height = 176;
  const view = points.slice(state.start, state.start + state.visible);
  const lows = view.map((item) => item.low);
  const highs = view.map((item) => item.high);
  let min = Math.min(...lows);
  let max = Math.max(...highs);

  // 主图指标线（全序列计算，视窗切片绘制；MA250 等长周期依赖完整历史）
  const indLines = kIndicatorLines(points, K_SETTINGS.indicator);
  const viewLines = indLines.map((l) => ({ ...l, slice: l.values.slice(state.start, state.start + state.visible) }));
  for (const l of viewLines) {
    for (const v of l.slice) {
      if (v != null && Number.isFinite(v)) { min = Math.min(min, v); max = Math.max(max, v); }
    }
  }
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

  // 筹码峰层：灰度横条铺在K线背后（可在设置中关闭）
  const chips = K_SETTINGS.chips ? calcChipDistribution(points, 40) : { dist: [], max: 0, lo: 0, hi: 0, width: 0, peakPrice: 0, avgCost: 0 };
  let chipLayer = "";
  if (chips.max > 0) {
    const maxBarW = plotW * 0.62;
    chipLayer = chips.dist
      .map((v, i) => {
        if (v <= 0) return "";
        const price = chips.lo + (i + 0.5) * chips.width;
        if (price < minY || price > maxY) return "";
        const yTop = priceY(price + chips.width / 2, minY, maxY, pad.top, plotH);
        const yBot = priceY(price - chips.width / 2, minY, maxY, pad.top, plotH);
        const w = (v / chips.max) * maxBarW;
        const g = Math.round(232 - (v / chips.max) * 148); // 密度越高灰度越深
        const isPeak = price >= chips.peakPrice - chips.width && price <= chips.peakPrice + chips.width;
        return `<rect x="${pad.left}" y="${yTop.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(1, yBot - yTop - 0.6).toFixed(1)}" fill="rgb(${g},${g},${g})" opacity="${isPeak ? 0.75 : 0.5}"/>`;
      })
      .join("");
  }

  // 策略K线形态：状态机染色 + B/S/T 标记（与基本K线互斥）
  const strat = K_SETTINGS.form === "strategy" ? kStrategyState(points) : null;

  const candles = view
    .map((item, index) => {
      const x = pad.left + step * index + step / 2;
      const openY = priceY(item.open, minY, maxY, pad.top, plotH);
      const closeY = priceY(item.close, minY, maxY, pad.top, plotH);
      const highY = priceY(item.high, minY, maxY, pad.top, plotH);
      const lowY = priceY(item.low, minY, maxY, pad.top, plotH);
      const up = strat ? strat.state[state.start + index] === 1 : item.close >= item.open;
      const cls = up ? "k-up" : "k-down";
      const bodyY = Math.min(openY, closeY);
      const bodyH = Math.max(2, Math.abs(closeY - openY));
      return `<g class="${cls}">
        <line x1="${x.toFixed(1)}" y1="${highY.toFixed(1)}" x2="${x.toFixed(1)}" y2="${lowY.toFixed(1)}"/>
        <rect x="${(x - candleW / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyH.toFixed(1)}" rx="1"/>
      </g>`;
    })
    .join("");

  // 指标线层（画在蜡烛之上）
  const indicatorLayer = viewLines
    .map((l) => {
      const pts = [];
      l.slice.forEach((v, i) => {
        if (v == null || !Number.isFinite(v)) return;
        const x = pad.left + step * i + step / 2;
        const y = priceY(v, minY, maxY, pad.top, plotH);
        pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      });
      if (pts.length < 2) return "";
      return `<polyline points="${pts.join(" ")}" fill="none" stroke="${l.color}" stroke-width="${l.width || 1.2}" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>`;
    })
    .join("");

  // 提示信号层：神奇九转（1-8 小数字，9 高亮）+ 多空 B/S
  let signalLayer = "";
  const nine = K_SETTINGS.nine ? kNineTurn(points) : null;
  if (nine) {
    view.forEach((item, i) => {
      const gi = state.start + i;
      const x = pad.left + step * i + step / 2;
      const lc = nine.low[gi], hc = nine.high[gi];
      if (lc > 0) {
        const y = priceY(item.low, minY, maxY, pad.top, plotH) + (lc === 9 ? 13 : 10);
        const big = lc === 9;
        signalLayer += `<text x="${x.toFixed(1)}" y="${Math.min(y, height - pad.bottom - 2).toFixed(1)}" text-anchor="middle" class="k-nine k-nine-low${big ? " nine-big" : ""}">${lc}</text>`;
      } else if (hc > 0) {
        const y = priceY(item.high, minY, maxY, pad.top, plotH) - (hc === 9 ? 8 : 5);
        const big = hc === 9;
        signalLayer += `<text x="${x.toFixed(1)}" y="${Math.max(y, pad.top + 8).toFixed(1)}" text-anchor="middle" class="k-nine k-nine-high${big ? " nine-big" : ""}">${hc}</text>`;
      }
    });
  }
  if (K_SETTINGS.bs && !strat) {
    for (const m of kCrossSignals(points)) {
      if (m.i < state.start || m.i >= state.start + state.visible) continue;
      const i = m.i - state.start;
      const item = view[i];
      const x = pad.left + step * i + step / 2;
      const isB = m.type === "B";
      const y = isB ? priceY(item.low, minY, maxY, pad.top, plotH) + 14 : priceY(item.high, minY, maxY, pad.top, plotH) - 14;
      const cy = clamp(y, pad.top + 7, height - pad.bottom - 7);
      signalLayer += `<g class="k-bs ${isB ? "k-bs-b" : "k-bs-s"}"><circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="6.5"/><text x="${x.toFixed(1)}" y="${(cy + 3).toFixed(1)}" text-anchor="middle">${isB ? "多" : "空"}</text></g>`;
    }
  }
  // 策略形态 B/S/T 标记（方块，仿同花顺）
  if (strat) {
    for (const m of strat.marks) {
      if (m.i < state.start || m.i >= state.start + state.visible) continue;
      const i = m.i - state.start;
      const item = view[i];
      const x = pad.left + step * i + step / 2;
      let y, cls, label;
      if (m.type === "B") { y = priceY(item.low, minY, maxY, pad.top, plotH) + 15; cls = "k-st-b"; label = "B"; }
      else if (m.type === "S") { y = priceY(item.high, minY, maxY, pad.top, plotH) - 15; cls = "k-st-s"; label = "S"; }
      else { y = m.side === "high" ? priceY(item.high, minY, maxY, pad.top, plotH) - 15 : priceY(item.low, minY, maxY, pad.top, plotH) + 15; cls = "k-st-t"; label = "T"; }
      const cy = clamp(y, pad.top + 7, height - pad.bottom - 7);
      signalLayer += `<g class="k-st ${cls}"><rect x="${(x - 6).toFixed(1)}" y="${(cy - 6).toFixed(1)}" width="12" height="12" rx="2.5"/><text x="${x.toFixed(1)}" y="${(cy + 3.2).toFixed(1)}" text-anchor="middle">${label}</text></g>`;
    }
  }

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

  // 指标读数图例（跟随选中K线）
  const legend = viewLines
    .map((l) => {
      const v = l.slice[selectedLocal];
      if (v == null || !Number.isFinite(v)) return "";
      return `<span style="color:${l.color}">${l.name}:${v.toFixed(2)}</span>`;
    })
    .join("");
  // 策略形态状态读数
  const stratLegend = strat
    ? `<span class="k-strat-state ${strat.state[selectedGlobal] === 1 ? "st-long" : "st-short"}">${strat.state[selectedGlobal] === 1 ? "策略·多头持有" : "策略·空头观望"}</span>`
    : "";

  el.innerHTML = `
    <div class="kline-tip">
      <strong>${selected.date}</strong>
      <span class="${changeClass}">涨幅 ${fmtPercent(changePercent)}</span>
      <span>开 ${fmtPrice(selected.open)}</span>
      <span>收 ${fmtPrice(selected.close)}</span>
      <span>高 ${fmtPrice(selected.high)}</span>
      <span>低 ${fmtPrice(selected.low)}</span>
      ${chips.max > 0 ? `<span class="k-chip-hint">峰 ${chips.peakPrice.toFixed(2)} · 均 ${chips.avgCost.toFixed(2)}</span>` : ""}
    </div>
    ${legend || stratLegend ? `<div class="k-legend">${stratLegend}${legend}</div>` : ""}
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="近期K线图（含主图指标与信号）">
      <rect x="0" y="0" width="${width}" height="${height}" class="k-bg"/>
      ${grid}
      ${chipLayer}
      ${candles}
      ${indicatorLayer}
      ${signalLayer}
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
  const KEY = "jungle-kline-expand-v1";
  let expanded = {};
  try { expanded = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { expanded = {}; }
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(expanded)); } catch (e) {} };

  const bindOne = (chart) => {
    chart._klineInteractBound = true;
    bindKlineInteraction(chart);
  };

  // ⚙ 设置入口：展开时才显示，点击打开底部抽屉（不触发折叠）
  charts.forEach((chart) => {
    const card = chart.closest(".kline-card");
    const head = card && card.querySelector(".kline-head");
    if (head && !head.querySelector(".k-gear")) {
      const gear = document.createElement("span");
      gear.className = "k-gear";
      gear.setAttribute("role", "button");
      gear.setAttribute("aria-label", "K线主图快捷设置");
      gear.title = "K线主图快捷设置";
      gear.textContent = "⚙";
      gear.addEventListener("click", (e) => { e.stopPropagation(); openKlineSettings(); });
      head.appendChild(gear);
    }
  });

  charts.forEach((chart) => {
    const card = chart.closest(".kline-card");
    const toggle = card && card.querySelector(".kline-toggle");
    const code = card ? card.id.replace(/^kcard-/, "") : "";
    const chev = toggle && toggle.querySelector(".kchev");

    // 默认收缩：只有用户展开过（localStorage 记忆）才渲染
    const open = (isExpanded) => {
      if (isExpanded) {
        card.classList.remove("collapsed");
        if (chev) chev.textContent = "▾ 收起";
        if (toggle) toggle.setAttribute("aria-expanded", "true");
        if (!chart._klineInteractBound) bindOne(chart);
        renderKlineChart(chart);
      } else {
        card.classList.add("collapsed");
        if (chev) chev.textContent = "▸ 展开";
        if (toggle) toggle.setAttribute("aria-expanded", "false");
      }
    };

    if (toggle) {
      toggle.addEventListener("click", () => {
        expanded[code] = !card.classList.contains("collapsed") ? false : true;
        save();
        open(expanded[code]);
      });
    }
    open(!!expanded[code]);
  });
  window.addEventListener("resize", () =>
    charts.forEach((c) => {
      const card = c.closest(".kline-card");
      if (!card || !card.classList.contains("collapsed")) renderKlineChart(c);
    })
  );
}

bindKlineCharts();

// ETF资金流向（双维度：盘中主力净流入 + 当日申购净流入T+1）
let _etfData = null;
let _etfTab = 'realtime';

async function loadETFFlow() {
    const listEl = document.getElementById('etf-flow-list');
    try {
        const response = await fetch(apiUrl('/api/etf-moneyflow'));
        const data = await response.json();
        _etfData = data;
        renderETFTab();
    } catch (err) {
        console.error('加载ETF资金流向失败:', err);
        if (listEl) listEl.innerHTML = '<div class="etf-loading">加载失败，请刷新重试</div>';
    }
}

function renderETFTab() {
    const data = _etfData;
    if (!data) return;
    const listEl = document.getElementById('etf-flow-list');
    const summaryEl = document.getElementById('etf-summary');
    const timeEl = document.getElementById('etf-update-time');
    const noteEl = document.getElementById('etf-note');

    if (_etfTab === 'realtime') {
        const items = data.items || [];
        if (!items.length) {
            listEl.innerHTML = '<div class="etf-loading">暂无数据</div>';
            summaryEl.innerHTML = '';
            noteEl.textContent = '';
            return;
        }
        const now = new Date();
        timeEl.textContent = `更新于 ${now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} · 盘中实时（3分钟缓存）`;
        const totalInflow = items.filter(x => x.main_net_inflow > 0).reduce((s, x) => s + x.main_net_inflow, 0);
        const top = items[0];
        summaryEl.innerHTML = `
            <div class="etf-summary-card"><div class="label">TOP15净流入合计</div><div class="value ${totalInflow > 0 ? 'positive' : 'negative'}">${formatMoney(totalInflow)}</div></div>
            <div class="etf-summary-card"><div class="label">最强流入</div><div class="value positive" style="font-size:13px;">${(top.name || '').slice(0, 8)}</div></div>
            <div class="etf-summary-card"><div class="label">统计口径</div><div class="value" style="font-size:13px;">超大单+大单净额</div></div>
        `;
        listEl.innerHTML = `
            <table class="etf-flow-table">
                <thead><tr><th>ETF</th><th>现价</th><th>涨跌幅</th><th class="etf-hide-mobile">成交额</th><th>主力净流入</th></tr></thead>
                <tbody>
                ${items.map(it => {
                    const chgCls = it.change_pct > 0 ? 'gain' : it.change_pct < 0 ? 'loss' : 'neutral';
                    const flowCls = it.main_net_inflow > 0 ? 'positive' : 'negative';
                    return `<tr>
                        <td class="etf-name-cell"><span class="n">${it.name}</span><span class="c">${it.code}</span></td>
                        <td>${(it.price || 0).toFixed(3)}</td>
                        <td class="${chgCls}">${it.change_pct > 0 ? '+' : ''}${(it.change_pct || 0).toFixed(2)}%</td>
                        <td class="etf-hide-mobile neutral">${formatMoney(it.amount || 0)}</td>
                        <td class="inflow-cell ${flowCls}">${it.main_net_inflow > 0 ? '+' : ''}${formatMoney(it.main_net_inflow)}</td>
                    </tr>`;
                }).join('')}
                </tbody>
            </table>`;
        noteEl.textContent = '盘中主力净流入＝当日超大单+大单买卖净额，反映二级市场大资金动向（股票/跨境/商品型ETF，剔除债基）。';
    } else {
        const apply = data.apply;
        if (!apply || !apply.top_inflow || !apply.top_inflow.length) {
            listEl.innerHTML = '<div class="etf-loading">申购数据未生成，等待每日刷新任务…</div>';
            summaryEl.innerHTML = '';
            noteEl.textContent = '';
            return;
        }
        timeEl.textContent = `数据日期 ${apply.data_date}（T+1官方份额）`;
        summaryEl.innerHTML = `
            <div class="etf-summary-card"><div class="label">净申购ETF数</div><div class="value positive">${apply.inflow_count} 只</div></div>
            <div class="etf-summary-card"><div class="label">净申购总额</div><div class="value positive">${formatMoney(apply.total_inflow)}</div></div>
            <div class="etf-summary-card"><div class="label">净赎回总额</div><div class="value negative">${formatMoney(apply.total_outflow)}</div></div>
        `;
        listEl.innerHTML = `
            <table class="etf-flow-table">
                <thead><tr><th>ETF</th><th>现价</th><th class="etf-hide-mobile">份额变动</th><th>申购净流入</th></tr></thead>
                <tbody>
                ${apply.top_inflow.map(it => {
                    const chgCls = it.change_pct > 0 ? 'gain' : it.change_pct < 0 ? 'loss' : 'neutral';
                    const flowCls = it.net_inflow > 0 ? 'positive' : 'negative';
                    const deltaTxt = it.delta_shares > 0 ? '+' + (it.delta_shares / 1e8).toFixed(2) + '亿份' : (it.delta_shares / 1e8).toFixed(2) + '亿份';
                    return `<tr>
                        <td class="etf-name-cell"><span class="n">${it.name}</span><span class="c">${it.code}</span></td>
                        <td>${(it.price || 0).toFixed(3)}</td>
                        <td class="etf-hide-mobile ${flowCls}">${deltaTxt}</td>
                        <td class="inflow-cell ${flowCls}">${it.net_inflow > 0 ? '+' : ''}${formatMoney(it.net_inflow)}</td>
                    </tr>`;
                }).join('')}
                </tbody>
            </table>`;
        noteEl.textContent = '申购净流入＝交易所官方份额变动 × 单位净值，一级市场申赎（机构建仓），为收盘后确认的 T+1 数据。';
    }
}

function bindETFTabs() {
    document.querySelectorAll('.etf-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            _etfTab = btn.dataset.etfTab;
            document.querySelectorAll('.etf-tab').forEach(b => b.classList.toggle('active', b === btn));
            renderETFTab();
        });
    });
}

function formatMoney(value) {
    value = value || 0;
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
bindETFTabs();
loadETFFlow();

// === 持仓走势分类（日线+30分钟双周期共振） ===
const REGIME_LABELS = { squeeze: '挤压待变', trending: '趋势型', ranging: '区间型', neutral: '中性' };

/* ===== 持仓走势分类筛选 ===== */
const RF_FILTER_KEY = 'jungle-rf-filter-v1';

function initRegimeFilter() {
    const bar = document.querySelector('.regime-filter');
    if (!bar) return;
    bar.querySelectorAll('.rf-btn').forEach(btn => {
        btn.addEventListener('click', () => applyRegimeFilter(btn.dataset.filter, true));
    });
    const saved = localStorage.getItem(RF_FILTER_KEY) || 'all';
    const exists = bar.querySelector(`.rf-btn[data-filter="${CSS.escape(saved)}"]`);
    applyRegimeFilter(exists ? saved : 'all', false);
}

function applyRegimeFilter(filter, remember) {
    const bar = document.querySelector('.regime-filter');
    if (!bar) return;
    if (remember) localStorage.setItem(RF_FILTER_KEY, filter);
    bar.querySelectorAll('.rf-btn').forEach(b => {
        const on = b.dataset.filter === filter;
        b.classList.toggle('on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const list = document.querySelector('.position-list');
    if (!list) return;
    let shown = 0;
    list.querySelectorAll('.position-card[data-code]').forEach(card => {
        const hide = filter !== 'all' && card.dataset.regime !== filter;
        card.classList.toggle('hidden-by-filter', hide);
        if (!hide) shown++;
    });
    const oldEmpty = list.querySelector('.regime-filter-empty');
    if (oldEmpty) oldEmpty.remove();
    if (!shown && filter !== 'all') {
        const div = document.createElement('div');
        div.className = 'regime-filter-empty';
        div.textContent = `暂无「${filter}」持仓`;
        list.appendChild(div);
    }
}

function updateRegimeFilterCounts() {
    const bar = document.querySelector('.regime-filter');
    if (!bar) return;
    const counts = { 'all': 0 };
    document.querySelectorAll('.position-card[data-code]').forEach(card => {
        counts['all']++;
        counts[card.dataset.regime] = (counts[card.dataset.regime] || 0) + 1;
    });
    bar.querySelectorAll('.rf-btn').forEach(b => {
        const el = b.querySelector('small');
        if (el) el.textContent = counts[b.dataset.filter] || 0;
    });
    const active = bar.querySelector('.rf-btn.on');
    if (active && active.dataset.filter !== 'all' && !(counts[active.dataset.filter] > 0)) {
        applyRegimeFilter('all', true);   // 当前筛选已无票（如重扫后分类变化）→ 回落到全部
    }
}

async function loadPositionRegime(refresh = false) {
    const cards = Array.from(document.querySelectorAll('.position-card[data-code]'));
    if (!cards.length) return;
    const posCodes = cards.map(c => c.dataset.code);
    const listEl = document.getElementById('regime-panel-list');
    const timeEl = document.getElementById('regime-panel-time');
    const countEl = document.getElementById('regime-countbar');
    const alertEl = document.getElementById('regime-alerts');
    const poolEl = document.getElementById('regime-pool');
    try {
        const response = await fetch(apiUrl('/api/regime/latest') + (refresh ? '&refresh=1' : ''));
        const data = await response.json();
        const byCode = {};
        (data.stocks || []).forEach(s => { byCode[s.code] = s; });
        const alertByCode = {};
        (data.alerts || []).forEach(a => { alertByCode[a.code] = a; });

        // 更新时间
        if (data.fetchedAt) {
            timeEl.textContent = `更新于 ${data.fetchedAt.slice(11, 16)}`;
        }

        // 0) 计数条：全池四分类统计 + 操作按钮
        const c = data.counts || {};
        const ORDER = { squeeze: 0, trending: 1, ranging: 2, neutral: 3 };
        const LABELS = { squeeze: '挤压待变', trending: '趋势型', ranging: '区间型', neutral: '中性' };
        if (countEl) {
            countEl.innerHTML = Object.keys(LABELS).map(k =>
                `<span class="rc-cnt ${k}">${LABELS[k]} ${c[k] || 0}</span>`).join('') +
                `<span class="rc-actions">
                    <button type="button" class="regime-mini-btn" id="regime-rescan-btn">重新扫描</button>
                    <button type="button" class="regime-mini-btn" id="regime-pool-btn">股票池</button>
                </span>`;
            document.getElementById('regime-rescan-btn').addEventListener('click', (e) => {
                e.currentTarget.disabled = true;
                e.currentTarget.textContent = '扫描中...';
                loadPositionRegime(true);
            });
            document.getElementById('regime-pool-btn').addEventListener('click', () => {
                if (!poolEl) return;
                const show = poolEl.hidden;
                poolEl.hidden = !show;
                if (show && !poolEl.dataset.rendered) renderPool();
            });
        }

        // 0.5) 带宽张开预警（全池）
        if (alertEl) {
            alertEl.innerHTML = (data.alerts || []).map(a =>
                `<div class="ra-row ${a.type === 'squeeze_break_up' ? 'up' : 'down'}">${a.name}（${a.code}）：${a.text}</div>`
            ).join('');
        }

        // chip 渲染器（持仓与股票池共用）
        function chipHtml(s, al) {
            const alertHtml = al ? `<div class="rc-alert"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8Z"/></svg> ${al.text || '带宽张开预警'}</div>` : '';
            return `
                <div class="regime-chip ${s.regime}" title="日线 ${s.label_daily || s.label} / 30分钟 ${s.label_30m}；轨外${(s.outside_ratio*100).toFixed(0)}% 带宽分位${(s.bandwidth_pct*100).toFixed(0)}%">
                    <div class="rc-top">
                        <span class="rc-name">${s.name}</span>
                        <span class="rc-label">${s.label}</span>
                    </div>
                    <div class="rc-tf">日K ${s.label_daily || '-'} · 30分 ${s.label_30m}</div>
                    <div class="rc-evidence">轨外${(s.outside_ratio*100).toFixed(0)}% · 带宽分位${(s.bandwidth_pct*100).toFixed(0)}% · 偏离MA20 ${s.ma_dev_pct > 0 ? '+' : ''}${s.ma_dev_pct.toFixed(1)}%</div>
                    ${alertHtml}
                </div>`;
        }

        // 1) 面板：持仓 chip（按分类排序）+ 更新时间
        const posChips = posCodes.map(code => {
            const s = byCode[code];
            if (!s) return `<div class="regime-chip neutral"><div class="rc-top"><span class="rc-name">${code}</span><span class="rc-label">计算中</span></div></div>`;
            return chipHtml(s, alertByCode[code]);
        });
        listEl.innerHTML = posChips.join('');

        // 1.5) 股票池其余票（默认折叠，点开渲染）
        function renderPool() {
            if (!poolEl) return;
            const others = (data.stocks || []).filter(s => !posCodes.includes(s.code))
                .sort((a, b) => (ORDER[a.regime] ?? 9) - (ORDER[b.regime] ?? 9));
            if (!others.length) { poolEl.hidden = true; return; }
            poolEl.classList.add('regime-panel-list');
            poolEl.innerHTML = others.map(s => chipHtml(s, alertByCode[s.code])).join('');
            poolEl.dataset.rendered = '1';
        }
        // 2) 卡片徽章：升级为双周期结果 + 子标签显示分周期
        cards.forEach(card => {
            const s = byCode[card.dataset.code];
            if (!s) return;
            const badge = card.querySelector('.regime-badge');
            const sub = card.querySelector('.regime-sub');
            if (badge) {
                badge.textContent = s.label;
                badge.dataset.fallback = s.label;
                const grad = {
                    squeeze: 'linear-gradient(135deg,#f59e0b,#ef4444)',
                    trending: 'linear-gradient(135deg,#10b981,#0ea5e9)',
                    ranging: 'linear-gradient(135deg,#3b82f6,#6366f1)',
                    neutral: 'linear-gradient(135deg,#64748b,#94a3b8)',
                }[s.regime] || 'linear-gradient(135deg,#64748b,#94a3b8)';
                badge.style.background = grad;
                badge.style.color = '#fff';
            }
            if (sub) sub.textContent = `日K ${s.label_daily || '-'} / 30分 ${s.label_30m}`;
            card.dataset.regime = s.label;   // 双周期结果同步给筛选条
        });
        updateRegimeFilterCounts();
        const activeBtn = document.querySelector('.regime-filter .rf-btn.on');
        if (activeBtn) applyRegimeFilter(activeBtn.dataset.filter, false);
    } catch (err) {
        console.error('加载走势分类失败:', err);
        listEl.innerHTML = '<div class="etf-loading">加载失败，稍后自动重试</div>';
        if (countEl) countEl.innerHTML = '<span style="font-size:12px;color:#ef4444">扫描数据未就绪 · <button type="button" class="regime-mini-btn" onclick="loadPositionRegime(true)">重试</button></span>';
        // 回退：保留 SSR 渲染的日线徽章
        document.querySelectorAll('.regime-badge[data-fallback]').forEach(b => {
            if (!b.textContent.trim()) b.textContent = b.dataset.fallback;
        });
        updateRegimeFilterCounts();   // 降级：用 SSR 日线口径的计数与筛选
        const rb = document.querySelector('.regime-filter .rf-btn.on');
        if (rb) applyRegimeFilter(rb.dataset.filter, false);
    }
}

initRegimeFilter();
loadPositionRegime();

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
