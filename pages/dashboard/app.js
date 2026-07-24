const bridge = window.AstrBotPluginPage;
const numberFormatter = new Intl.NumberFormat(navigator.language);
const dateFormatter = new Intl.DateTimeFormat(navigator.language, {
  dateStyle: "short",
  timeStyle: "medium",
});

let overview = null;
let stagedConfig = null;
let discoveredDomains = [];
let statusTimer = null;

const elements = {
  summary: document.getElementById("summary-text"),
  cards: document.getElementById("metric-cards"),
  jobsBody: document.getElementById("jobs-body"),
  jobsEmpty: document.getElementById("jobs-empty"),
  usersBody: document.getElementById("users-body"),
  usersEmpty: document.getElementById("users-empty"),
  domainsBody: document.getElementById("domains-body"),
  domainsEmpty: document.getElementById("domains-empty"),
  discoveredBody: document.getElementById("discovered-domains-body"),
  discoveredEmpty: document.getElementById("discovered-domains-empty"),
  discoveryErrors: document.getElementById("domain-discovery-errors"),
  applyDomainsButton: document.getElementById("apply-domains-button"),
  loginDomain: document.getElementById("login-domain"),
  loginResult: document.getElementById("cookie-login-result"),
  cacheSummary: document.getElementById("cache-summary"),
  eventsBody: document.getElementById("events-body"),
  eventsEmpty: document.getElementById("events-empty"),
  reasons: document.getElementById("failure-reasons"),
  reasonsEmpty: document.getElementById("reasons-empty"),
  validation: document.getElementById("validation-result"),
  importButton: document.getElementById("import-config-button"),
  configFile: document.getElementById("config-file"),
  status: document.getElementById("status-message"),
};

function formatBytes(value) {
  let size = Math.max(0, Number(value) || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 ? 0 : 1;
  return `${new Intl.NumberFormat(navigator.language, {
    maximumFractionDigits: digits,
  }).format(size)}\u00a0${units[unitIndex]}`;
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  return dateFormatter.format(new Date(Number(timestamp) * 1000));
}

function showStatus(message) {
  window.clearTimeout(statusTimer);
  elements.status.textContent = message;
  elements.status.classList.add("visible");
  statusTimer = window.setTimeout(() => {
    elements.status.classList.remove("visible");
  }, 4200);
}

function setBusy(button, busy, busyLabel) {
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = busyLabel;
    button.disabled = true;
    return;
  }
  button.textContent = button.dataset.label || button.textContent;
  button.disabled = false;
}

function clearElement(element) {
  element.replaceChildren();
}

function cell(text) {
  const element = document.createElement("td");
  element.textContent = text ?? "—";
  return element;
}

function statusPill(text, state) {
  const element = document.createElement("span");
  element.className = `status-pill ${state || ""}`.trim();
  element.textContent = text;
  return element;
}

function renderCards(data) {
  const metrics = data.metrics;
  const cards = [
    ["活动任务", data.active_jobs, `${data.pending_selections} 个章节选择待回复`],
    ["今日下载", metrics.today.downloads, `${metrics.today.users} 个用户`],
    ["今日流量", formatBytes(metrics.today.bytes_sent), "按成功发送文件统计"],
    ["缓存占用", formatBytes(data.cache.bytes), `${data.cache.files} 个 ZIP`],
    ["成功发送", metrics.outcomes.success, "最近统计周期"],
    ["失败任务", metrics.outcomes.failure, "最近统计周期"],
  ];
  clearElement(elements.cards);
  for (const [label, value, detail] of cards) {
    const article = document.createElement("article");
    article.className = "metric-card";
    const labelElement = document.createElement("p");
    labelElement.className = "metric-label";
    labelElement.textContent = label;
    const valueElement = document.createElement("p");
    valueElement.className = "metric-value";
    valueElement.textContent = typeof value === "number" ? numberFormatter.format(value) : value;
    const detailElement = document.createElement("p");
    detailElement.className = "metric-detail";
    detailElement.textContent = detail;
    article.append(labelElement, valueElement, detailElement);
    elements.cards.append(article);
  }
}

function renderJobs(jobs) {
  clearElement(elements.jobsBody);
  elements.jobsEmpty.hidden = jobs.length > 0;
  for (const job of jobs) {
    const row = document.createElement("tr");
    row.append(
      cell(`JM${job.jm_id}`),
      cell(job.selection || "全部"),
      cell(numberFormatter.format(job.requester_count)),
      cell(`${numberFormatter.format(Math.round(job.age_seconds))}\u00a0秒`),
    );
    const action = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button danger";
    button.textContent = `取消 JM${job.jm_id}`;
    button.addEventListener("click", () => cancelJobs(job.jm_id));
    action.append(button);
    row.append(action);
    elements.jobsBody.append(row);
  }
}

function renderUsers(users) {
  clearElement(elements.usersBody);
  elements.usersEmpty.hidden = users.length > 0;
  for (const user of users) {
    const row = document.createElement("tr");
    row.append(
      cell(user.user_id),
      cell(numberFormatter.format(user.downloads)),
      cell(formatBytes(user.bytes_sent)),
    );
    elements.usersBody.append(row);
  }
}

function renderDomains(domains) {
  clearElement(elements.domainsBody);
  elements.domainsEmpty.hidden = domains.length > 0;
  for (const domain of domains) {
    const row = document.createElement("tr");
    const statusCell = document.createElement("td");
    statusCell.append(statusPill(domain.healthy ? "可用" : "不可用", domain.healthy ? "success" : "failure"));
    row.append(
      cell(domain.domain),
      cell(domain.final_domain || domain.domain),
      statusCell,
      cell(domain.latency_ms == null ? "—" : `${numberFormatter.format(domain.latency_ms)}\u00a0ms`),
      cell(numberFormatter.format(domain.redirect_count || 0)),
      cell(domain.status_code),
      cell(formatDate(domain.checked_at)),
      cell(domain.error),
    );
    elements.domainsBody.append(row);
  }
}

function renderDiscoveredDomains(candidates, configuredDomains) {
  discoveredDomains = candidates;
  clearElement(elements.discoveredBody);
  elements.discoveredEmpty.hidden = candidates.length > 0;
  elements.applyDomainsButton.disabled = candidates.length === 0;
  for (const candidate of candidates) {
    const row = document.createElement("tr");
    const choice = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "domain-checkbox";
    checkbox.dataset.domain = candidate.domain;
    checkbox.name = "selected_domain";
    checkbox.setAttribute("aria-label", `选择域名 ${candidate.domain}`);
    checkbox.checked = configuredDomains.includes(candidate.domain);
    checkbox.addEventListener("change", updateApplyDomainsButton);
    choice.append(checkbox);
    const statusCell = document.createElement("td");
    const checked = typeof candidate.healthy === "boolean";
    statusCell.append(
      statusPill(
        checked ? (candidate.healthy ? "可用" : "不可用") : "未检查",
        checked ? (candidate.healthy ? "success" : "failure") : "",
      ),
    );
    row.append(
      choice,
      cell(candidate.domain),
      cell((candidate.sources || []).join("、")),
      cell(candidate.final_domain || candidate.domain),
      statusCell,
      cell(candidate.latency_ms == null ? "—" : `${numberFormatter.format(candidate.latency_ms)}\u00a0ms`),
    );
    elements.discoveredBody.append(row);
  }
  updateApplyDomainsButton();
  populateLoginDomains(configuredDomains, candidates.map((item) => item.domain));
}

function updateApplyDomainsButton() {
  const selected = document.querySelectorAll('input[name="selected_domain"]:checked');
  elements.applyDomainsButton.disabled = selected.length === 0;
}

function populateLoginDomains(configuredDomains, extraDomains = []) {
  const current = elements.loginDomain.value;
  const domains = [...new Set([...configuredDomains, ...extraDomains])];
  clearElement(elements.loginDomain);
  for (const domain of domains) {
    const option = document.createElement("option");
    option.value = domain;
    option.textContent = domain;
    elements.loginDomain.append(option);
  }
  if (domains.includes(current)) elements.loginDomain.value = current;
}

function renderReasons(reasons) {
  clearElement(elements.reasons);
  elements.reasonsEmpty.hidden = reasons.length > 0;
  const maximum = Math.max(1, ...reasons.map((item) => Number(item.count) || 0));
  for (const item of reasons) {
    const row = document.createElement("div");
    row.className = "reason-item";
    const label = document.createElement("span");
    label.className = "reason-label";
    label.textContent = item.reason;
    label.title = item.reason;
    const track = document.createElement("div");
    track.className = "reason-track";
    const bar = document.createElement("div");
    bar.className = "reason-bar";
    bar.style.width = `${Math.max(4, (Number(item.count) / maximum) * 100)}%`;
    track.append(bar);
    const count = document.createElement("span");
    count.className = "reason-count";
    count.textContent = numberFormatter.format(item.count);
    row.append(label, track, count);
    elements.reasons.append(row);
  }
}

function renderEvents(events) {
  clearElement(elements.eventsBody);
  elements.eventsEmpty.hidden = events.length > 0;
  for (const event of events) {
    const row = document.createElement("tr");
    const outcome = document.createElement("td");
    if (event.success == null) {
      outcome.append(statusPill(event.level || "信息"));
    } else {
      outcome.append(statusPill(event.success ? "成功" : "失败", event.success ? "success" : "failure"));
    }
    row.append(
      cell(formatDate(event.created_at)),
      cell(event.event_type),
      outcome,
      cell(event.jm_id ? `JM${event.jm_id}` : "—"),
      cell(event.user_id),
      cell(event.reason),
    );
    elements.eventsBody.append(row);
  }
}

function renderOverview(data) {
  overview = data;
  elements.summary.textContent = `v${String(data.version).replace(/^v/, "")} · ${data.active_jobs} 个活动任务 · ${data.cache.files} 个缓存文件`;
  renderCards(data);
  renderJobs(data.jobs);
  renderUsers(data.metrics.top_users || []);
  renderDomains(data.domains || []);
  renderReasons(data.metrics.failure_reasons || []);
  populateLoginDomains(
    data.configured_domains || [],
    discoveredDomains.map((item) => item.domain),
  );
  const quota = data.cache.max_bytes > 0 ? ` / ${formatBytes(data.cache.max_bytes)}` : "（不限空间）";
  const expiry = data.cache.expire_days > 0 ? `${data.cache.expire_days} 天后过期` : "永不过期";
  elements.cacheSummary.textContent = `${data.cache.files} 个文件，占用 ${formatBytes(data.cache.bytes)}${quota}；${expiry}。`;
}

async function refreshData({ announce = false } = {}) {
  const button = document.getElementById("refresh-button");
  setBusy(button, true, "正在刷新…");
  try {
    const [overviewData, eventData] = await Promise.all([
      bridge.apiGet("overview"),
      bridge.apiGet("events", { limit: 50 }),
    ]);
    renderOverview(overviewData);
    renderEvents(eventData.events || []);
    if (announce) showStatus("运行数据已刷新。");
  } catch (error) {
    showStatus(`刷新失败：${error.message} 请确认插件已启用。`);
  } finally {
    setBusy(button, false, "");
  }
}

async function cancelJobs(target) {
  if (!window.confirm(`确定取消 ${target === "全部" ? "全部任务" : `JM${target}`}吗？`)) return;
  try {
    const result = await bridge.apiPost("jobs/cancel", { target });
    showStatus(`已取消 ${result.cancelled.length} 个任务或待选记录。`);
    await refreshData();
  } catch (error) {
    showStatus(`取消失败：${error.message}`);
  }
}

async function checkDomains() {
  const button = document.getElementById("check-domains-button");
  setBusy(button, true, "正在检查…");
  try {
    const result = await bridge.apiPost("domains/check", {});
    renderDomains(result.domains || []);
    showStatus("域名健康检查已完成，新的优先级立即生效。");
    await refreshData();
  } catch (error) {
    showStatus(`域名检查失败：${error.message}`);
  } finally {
    setBusy(button, false, "");
  }
}

async function discoverDomainCandidates() {
  const button = document.getElementById("discover-domains-button");
  setBusy(button, true, "正在发现…");
  elements.discoveryErrors.textContent = "";
  try {
    const result = await bridge.apiPost("domains/discover", {});
    renderDiscoveredDomains(
      result.candidates || [],
      overview?.configured_domains || [],
    );
    const errors = result.errors || [];
    if (errors.length > 0) {
      elements.discoveryErrors.className = "validation-result failure";
      elements.discoveryErrors.textContent = errors
        .map((item) => `${item.source}：${item.error}`)
        .join("\n");
    } else {
      elements.discoveryErrors.className = "validation-result success";
      elements.discoveryErrors.textContent =
        "候选域名获取完成。为避免页面请求超时，本次不批量检查新候选；应用后请执行域名健康检查。";
    }
    showStatus(`发现 ${result.candidates?.length || 0} 个候选域名。`);
  } catch (error) {
    elements.discoveryErrors.className = "validation-result failure";
    const detail = error.message === "Network Error"
      ? "管理页面请求超时或连接中断；这不代表已配置域名无法访问。"
      : error.message;
    elements.discoveryErrors.textContent = `发现失败：${detail}`;
    showStatus("域名发现失败，请查看页面中的具体原因。");
  } finally {
    setBusy(button, false, "");
  }
}

async function applySelectedDomains() {
  const domains = [
    ...document.querySelectorAll('input[name="selected_domain"]:checked'),
  ].map((input) => input.dataset.domain);
  if (domains.length === 0) {
    showStatus("请至少选择一个候选域名。");
    return;
  }
  if (!window.confirm(`确定应用所选的 ${domains.length} 个域名并热重载插件配置吗？`)) return;
  const button = elements.applyDomainsButton;
  setBusy(button, true, "正在应用…");
  try {
    const result = await bridge.apiPost("domains/apply", { domains });
    const appliedDomains = result.domains || [];
    if (!result.persisted || appliedDomains.length === 0) {
      throw new Error("后端未确认配置已经持久化。");
    }
    renderDiscoveredDomains(discoveredDomains, appliedDomains);
    showStatus(`已持久化并启用 ${appliedDomains.length} 个候选域名。`);
    await refreshData();
  } catch (error) {
    showStatus(`应用域名失败：${error.message}`);
  } finally {
    setBusy(button, false, "");
    updateApplyDomainsButton();
  }
}

async function loginAndRefreshCookie(event) {
  event.preventDefault();
  const button = document.getElementById("cookie-login-button");
  const usernameInput = document.getElementById("login-username");
  const passwordInput = document.getElementById("login-password");
  const payload = {
    domain: elements.loginDomain.value,
    username: usernameInput.value,
    password: passwordInput.value,
  };
  if (!payload.domain || !payload.username || !payload.password) {
    elements.loginResult.className = "validation-result failure";
    elements.loginResult.textContent = "请选择域名并填写账号和密码。";
    return;
  }
  setBusy(button, true, "正在登录…");
  try {
    const result = await bridge.apiPost("auth/login", payload);
    elements.loginResult.className = "validation-result success";
    elements.loginResult.textContent = `Cookie 已更新：${(result.cookie_names || []).join("、")}。账号和密码未保存。`;
    usernameInput.value = "";
    showStatus("JM 登录成功，新 Cookie 已写入插件配置。");
    await refreshData();
  } catch (error) {
    elements.loginResult.className = "validation-result failure";
    elements.loginResult.textContent = `${error.message} 请确认选择的是当前有效域名。`;
  } finally {
    payload.password = "";
    passwordInput.value = "";
    setBusy(button, false, "");
  }
}

async function cleanupCache() {
  if (!window.confirm("确定按当前空间配额和过期策略清理缓存吗？活动任务文件不会被删除。")) return;
  const button = document.getElementById("cleanup-cache-button");
  setBusy(button, true, "正在清理…");
  try {
    const result = await bridge.apiPost("cache/cleanup", {});
    showStatus(`缓存清理完成：删除 ${result.removed_count} 个文件，释放 ${formatBytes(result.removed_bytes)}。`);
    await refreshData();
  } catch (error) {
    showStatus(`缓存清理失败：${error.message}`);
  } finally {
    setBusy(button, false, "");
  }
}

async function exportConfiguration() {
  const includeSecrets = document.getElementById("include-secrets").checked;
  if (includeSecrets && !window.confirm("导出的文件将包含密码、Cookie 和代理配置。确定继续吗？")) return;
  try {
    await bridge.download(
      "config/export",
      { include_secrets: includeSecrets },
      `jmdownloader-config-${overview?.version || "export"}.json`,
    );
    showStatus(includeSecrets ? "敏感配置已导出，请妥善保管文件。" : "脱敏配置已导出。");
  } catch (error) {
    showStatus(`导出失败：${error.message}`);
  }
}

function showValidation(result) {
  elements.validation.className = `validation-result ${result.valid ? "success" : "failure"}`;
  const lines = [result.valid ? "配置有效，可以应用。" : "配置无效，请修正后重试。"];
  for (const warning of result.warnings || []) lines.push(`警告：${warning}`);
  for (const error of result.errors || []) lines.push(`错误：${error}`);
  elements.validation.textContent = lines.join("\n");
}

async function validateConfiguration() {
  const file = elements.configFile.files?.[0];
  stagedConfig = null;
  elements.importButton.disabled = true;
  if (!file) {
    showStatus("请先选择 JSON 配置文件。");
    return;
  }
  if (file.size > 256 * 1024) {
    showValidation({ valid: false, errors: ["配置文件不能超过 256\u00a0KB。"], warnings: [] });
    return;
  }
  let documentData;
  try {
    documentData = JSON.parse(await file.text());
  } catch {
    showValidation({ valid: false, errors: ["文件不是有效的 JSON。"], warnings: [] });
    return;
  }
  const button = document.getElementById("validate-config-button");
  setBusy(button, true, "正在验证…");
  try {
    const result = await bridge.apiPost("config/validate", documentData);
    showValidation(result);
    if (result.valid) {
      stagedConfig = documentData;
      elements.importButton.disabled = false;
    }
  } catch (error) {
    showValidation({ valid: false, errors: [error.message], warnings: [] });
  } finally {
    setBusy(button, false, "");
  }
}

async function importConfiguration() {
  if (!stagedConfig) {
    showStatus("请先验证配置文件。");
    return;
  }
  if (!window.confirm("确定应用已验证配置吗？运行参数会立即热重载。")) return;
  const button = elements.importButton;
  setBusy(button, true, "正在应用…");
  try {
    const result = await bridge.apiPost("config/import", stagedConfig);
    stagedConfig = null;
    elements.configFile.value = "";
    elements.validation.className = "validation-result success";
    elements.validation.textContent = `配置已应用。${(result.warnings || []).join("；")}`;
    showStatus("配置导入成功，运行参数已重载。");
    await refreshData();
  } catch (error) {
    showStatus(`导入失败：${error.message}`);
  } finally {
    setBusy(button, false, "");
    button.disabled = true;
  }
}

function selectTab() {
  const tabName = window.location.hash.slice(1) || "overview";
  const validName = document.getElementById(tabName)?.classList.contains("tab-panel") ? tabName : "overview";
  for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== validName;
  for (const link of document.querySelectorAll("[data-tab]")) {
    if (link.dataset.tab === validName) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function applyTheme() {
  const dark = Boolean(bridge.getContext()?.isDark);
  document.querySelector('meta[name="theme-color"]').content = dark ? "#101620" : "#f4f7fb";
}

document.getElementById("refresh-button").addEventListener("click", () => refreshData({ announce: true }));
document.getElementById("cancel-all-button").addEventListener("click", () => cancelJobs("全部"));
document.getElementById("check-domains-button").addEventListener("click", checkDomains);
document.getElementById("discover-domains-button").addEventListener("click", discoverDomainCandidates);
elements.applyDomainsButton.addEventListener("click", applySelectedDomains);
document.getElementById("cookie-login-form").addEventListener("submit", loginAndRefreshCookie);
document.getElementById("cleanup-cache-button").addEventListener("click", cleanupCache);
document.getElementById("export-config-button").addEventListener("click", exportConfiguration);
document.getElementById("validate-config-button").addEventListener("click", validateConfiguration);
elements.importButton.addEventListener("click", importConfiguration);
window.addEventListener("hashchange", selectTab);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshData();
});

await bridge.ready();
selectTab();
applyTheme();
const removeContextListener = bridge.onContext(applyTheme);
window.addEventListener("beforeunload", (event) => {
  removeContextListener();
  const loginDirty = Boolean(
    document.getElementById("login-username").value ||
      document.getElementById("login-password").value,
  );
  if (stagedConfig || loginDirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
await refreshData();
window.setInterval(() => {
  if (!document.hidden) refreshData();
}, 15000);
