const state = {
  account: "",
  target: "",
  query: "",
  sort: "last_changed_at",
  order: "desc",
  offset: 0,
  limit: 50,
  total: 0,
  items: [],
};

const elements = {
  scope: document.querySelector("#scopeSelect"),
  search: document.querySelector("#searchInput"),
  sort: document.querySelector("#sortSelect"),
  refresh: document.querySelector("#refreshButton"),
  previous: document.querySelector("#previousButton"),
  next: document.querySelector("#nextButton"),
  pageLabel: document.querySelector("#pageLabel"),
  resultCount: document.querySelector("#resultCount"),
  itemsBody: document.querySelector("#itemsBody"),
  empty: document.querySelector("#emptyState"),
  runs: document.querySelector("#runsList"),
  total: document.querySelector("#totalStat"),
  stable: document.querySelector("#stableStat"),
  fallback: document.querySelector("#fallbackStat"),
  changed: document.querySelector("#changedStat"),
  runState: document.querySelector("#runStateStat"),
  latestSeen: document.querySelector("#latestSeenStat"),
  dialog: document.querySelector("#detailDialog"),
  dialogTitle: document.querySelector("#dialogTitle"),
  detailMeta: document.querySelector("#detailMeta"),
  detailJson: document.querySelector("#detailJson"),
  closeDialog: document.querySelector("#closeDialogButton"),
  toast: document.querySelector("#toast"),
};

function params(extra = {}) {
  const query = new URLSearchParams();
  if (state.account) query.set("account", state.account);
  if (state.target) query.set("target", state.target);
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== "" && value !== undefined) query.set(key, String(value));
  });
  return query.toString();
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const value = await response.json();
  if (!response.ok) throw new Error(value.message || value.error || `HTTP ${response.status}`);
  return value;
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function runStatus(run) {
  if (run.run_status === "running") return "运行中";
  if (run.run_status === "interrupted") return "已中断";
  return run.success ? "成功" : "失败";
}

function runMode(run) {
  if (run.scan_mode === "full") return "深度扫描";
  if (run.scan_mode === "manual_verification") return "人工验证";
  return "增量扫描";
}

function textCell(value, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value || "—";
  return cell;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  window.setTimeout(() => elements.toast.classList.remove("visible"), 2200);
}

function renderItems(payload) {
  state.items = payload.items;
  state.total = payload.total;
  elements.itemsBody.replaceChildren();
  elements.resultCount.textContent = `${formatNumber(payload.total)} 条`;
  elements.empty.hidden = payload.items.length !== 0;

  payload.items.forEach((item, index) => {
    const row = document.createElement("tr");
    const itemCell = document.createElement("td");
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.name || "未命名项目";
    const id = document.createElement("span");
    id.className = "item-id";
    id.textContent = item.id || item.identity;
    itemCell.append(name, id);
    row.append(itemCell);
    row.append(textCell(item.price, "price"));
    row.append(textCell(item.level));

    const sourceCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `source-badge${item.identity_stable ? "" : " fallback"}`;
    badge.textContent = item.identity_stable ? (item.source || "API") : "降级键";
    sourceCell.append(badge);
    row.append(sourceCell);
    row.append(textCell(formatTime(item.first_seen_at)));
    row.append(textCell(formatTime(item.last_seen_at)));
    row.append(textCell(formatTime(item.last_changed_at)));

    const actionCell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "details-button";
    button.textContent = "详情";
    button.addEventListener("click", () => openDetail(state.items[index]));
    actionCell.append(button);
    row.append(actionCell);
    elements.itemsBody.append(row);
  });

  const currentPage = Math.floor(state.offset / state.limit) + 1;
  const totalPages = Math.max(1, Math.ceil(state.total / state.limit));
  elements.pageLabel.textContent = `第 ${currentPage} / ${totalPages} 页`;
  elements.previous.disabled = state.offset === 0;
  elements.next.disabled = state.offset + state.limit >= state.total;
}

function renderSummary(summary) {
  elements.total.textContent = formatNumber(summary.total);
  elements.stable.textContent = formatNumber(summary.stable);
  elements.fallback.textContent = `降级键 ${formatNumber(summary.fallback)}`;
  elements.changed.textContent = formatNumber(summary.changed_24h);
  const run = summary.last_run;
  elements.runState.textContent = run ? runStatus(run) : "暂无";
  elements.latestSeen.textContent = summary.latest_seen_at
    ? `观察于 ${formatTime(summary.latest_seen_at)}`
    : "等待首次采集";
}

function renderRuns(runs) {
  elements.runs.replaceChildren();
  if (!runs.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "暂无采集记录";
    elements.runs.append(empty);
    return;
  }
  runs.slice(0, 12).forEach((run) => {
    const row = document.createElement("article");
    row.className = "run-row";
    const time = document.createElement("strong");
    time.textContent = formatTime(run.run_status === "running" ? run.started_at : run.finished_at);
    const mode = document.createElement("span");
    mode.textContent = runMode(run);
    const status = document.createElement("span");
    const statusClass = run.run_status === "running"
      ? "running"
      : (run.success ? "success" : "failure");
    status.className = `state-badge ${statusClass}`;
    status.textContent = runStatus(run);
    const summary = document.createElement("span");
    summary.className = "run-error";
    summary.textContent = run.run_status === "running"
      ? `第 ${run.cycle} 轮正在采集`
      : (run.success
        ? `观察 ${run.observed_count} · 新增 ${run.inserted_count} · 更新 ${run.updated_count}`
        : (run.error || run.auth_state || "未知错误"));
    const reason = document.createElement("span");
    reason.textContent = `${run.pages_scanned} 轮 · ${run.termination_reason || "—"}`;
    row.append(time, mode, status, summary, reason);
    elements.runs.append(row);
  });
}

function addMeta(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value || "—";
  wrapper.append(term, description);
  elements.detailMeta.append(wrapper);
}

function openDetail(item) {
  elements.dialogTitle.textContent = item.name || "未命名项目";
  elements.detailMeta.replaceChildren();
  addMeta("唯一键", item.identity);
  addMeta("业务 ID", item.id);
  addMeta("首次发现", formatTime(item.first_seen_at));
  addMeta("最近变化", formatTime(item.last_changed_at));
  elements.detailJson.textContent = JSON.stringify(item.detail || {}, null, 2);
  elements.dialog.showModal();
}

async function loadOptions() {
  const payload = await getJson("/api/options");
  payload.scopes.forEach((scope) => {
    const option = document.createElement("option");
    option.value = `${scope.account_key}|${scope.target_key}`;
    option.textContent = `${scope.account_key} · ${scope.target_key.slice(0, 8)}`;
    elements.scope.append(option);
  });
}

async function refresh() {
  elements.refresh.disabled = true;
  elements.refresh.textContent = "刷新中…";
  try {
    const itemQuery = params({
      q: state.query,
      limit: state.limit,
      offset: state.offset,
      sort: state.sort,
      order: state.order,
    });
    const [summary, items, runs] = await Promise.all([
      getJson(`/api/summary?${params()}`),
      getJson(`/api/items?${itemQuery}`),
      getJson(`/api/runs?${params({ limit: 30 })}`),
    ]);
    renderSummary(summary);
    renderItems(items);
    renderRuns(runs.runs);
  } catch (error) {
    showToast(`加载失败：${error.message}`);
  } finally {
    elements.refresh.disabled = false;
    elements.refresh.textContent = "刷新数据";
  }
}

let searchTimer;
elements.search.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    state.query = elements.search.value.trim();
    state.offset = 0;
    refresh();
  }, 260);
});
elements.scope.addEventListener("change", () => {
  const [account = "", target = ""] = elements.scope.value.split("|");
  state.account = account;
  state.target = target;
  state.offset = 0;
  refresh();
});
elements.sort.addEventListener("change", () => {
  [state.sort, state.order] = elements.sort.value.split(":");
  state.offset = 0;
  refresh();
});
elements.refresh.addEventListener("click", () => refresh().then(() => showToast("数据已刷新")));
elements.previous.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  refresh();
});
elements.next.addEventListener("click", () => {
  if (state.offset + state.limit < state.total) state.offset += state.limit;
  refresh();
});
elements.closeDialog.addEventListener("click", () => elements.dialog.close());
elements.dialog.addEventListener("click", (event) => {
  if (event.target === elements.dialog) elements.dialog.close();
});

loadOptions().then(refresh).catch((error) => showToast(`初始化失败：${error.message}`));
