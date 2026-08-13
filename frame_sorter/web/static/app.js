"use strict";

const folderInput = document.querySelector("#folder");
const pickButton = document.querySelector("#pick");
const refreshButton = document.querySelector("#refresh");
const originalVideoInput = document.querySelector("#original-video");
const outputVideoInput = document.querySelector("#output-video");
const pickOriginalVideoButton = document.querySelector("#pick-original-video");
const pickOutputVideoButton = document.querySelector("#pick-output-video");
const analyzeButton = document.querySelector("#analyze");
const renameButton = document.querySelector("#rename");
const undoButton = document.querySelector("#undo");
const rebuildButton = document.querySelector("#rebuild");
const workflowSummary = document.querySelector("#workflowSummary");
const statusNode = document.querySelector("#status");
const progressNode = document.querySelector("#progress");
const progressBar = document.querySelector("#bar");
const logNode = document.querySelector("#log");
const terminalStatus = document.querySelector("#terminalStatus");
const clearLogsButton = document.querySelector("#clearLogs");

const MAX_UI_LOG_LINES = 500;
const JOB_ID_PATTERN = /^[0-9a-f]{32}$/u;
const SUCCESS_STATES = new Set(["ready", "completed", "done", "cached", "applied", "available"]);
const ACTIVE_STATES = new Set(["queued", "running", "active", "checking", "processing"]);
const WARNING_STATES = new Set(["missing", "blocked", "stale", "incomplete", "pending"]);
const ERROR_STATES = new Set(["failed", "error", "invalid", "interrupted"]);

const STAGES = Object.freeze({
  analysis: {
    card: document.querySelector("#analysisStage"),
    state: document.querySelector("#analysisState"),
    detail: document.querySelector("#analysisDetail"),
    artifact: document.querySelector("#analysisArtifact"),
    emptyLabel: "не проверено",
    emptyDetail: "Выберите каталог, чтобы проверить CSV и метаданные.",
  },
  rename: {
    card: document.querySelector("#renameStage"),
    state: document.querySelector("#renameState"),
    detail: document.querySelector("#renameDetail"),
    artifact: document.querySelector("#renameArtifact"),
    emptyLabel: "не проверено",
    emptyDetail: "Состояние появится после проверки каталога.",
  },
  rebuild: {
    card: document.querySelector("#rebuildStage"),
    state: document.querySelector("#rebuildState"),
    detail: document.querySelector("#rebuildDetail"),
    artifact: document.querySelector("#rebuildArtifact"),
    emptyLabel: "не проверено",
    emptyDetail: "Сборка станет доступна после подтверждения сортировки кадров.",
  },
});

const state = {
  workflow: null,
  busy: false,
  refreshing: false,
  pathPickerBusy: false,
  refreshGeneration: 0,
  lastRefreshedFolder: "",
  currentJob: null,
  eventSource: null,
  eventCursor: 0,
  pollTimer: null,
  refreshedTerminalJobId: null,
  logLines: [],
};

function folderPath() {
  return folderInput.value.trim();
}

function pathKey(value) {
  return String(value || "").trim().toLocaleLowerCase("ru-RU");
}

function clampProgress(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

function stageTone(value) {
  const normalized = String(value || "idle").trim().toLowerCase();
  if (SUCCESS_STATES.has(normalized)) return "success";
  if (ACTIVE_STATES.has(normalized)) return "active";
  if (WARNING_STATES.has(normalized)) return "warning";
  if (ERROR_STATES.has(normalized)) return "error";
  return "neutral";
}

function safeState(value) {
  const normalized = String(value || "idle").trim().toLowerCase();
  return /^[a-z0-9_-]+$/u.test(normalized) ? normalized : "idle";
}

function stageStateLabel(value, fallback) {
  const labels = {
    completed: "готово",
    cached: "кеш найден",
    ready: "доступно",
    blocked: "ожидает",
    stale: "устарело",
    invalid: "ошибка",
  };
  return labels[value] || fallback;
}

function stageArtifact(key, stage) {
  if (stage?.artifact_path) return String(stage.artifact_path);
  const artifacts = state.workflow?.artifacts || {};
  if (key === "analysis") return String(artifacts.plan_csv || "");
  if (key === "rename") return String(artifacts.undo_csv || "");
  return String(artifacts.output_video || artifacts.rebuilt_video || "");
}

function renderStage(key) {
  const view = STAGES[key];
  const stage = state.workflow?.stages?.[key] || null;
  const stageState = safeState(stage?.state);
  const artifact = stageArtifact(key, stage);
  view.card.dataset.state = stageState;
  view.card.dataset.tone = stageTone(stageState);
  view.state.textContent = String(stage?.label || stageStateLabel(stageState, view.emptyLabel));
  view.detail.textContent = String(stage?.detail || view.emptyDetail);
  view.artifact.hidden = !artifact;
  view.artifact.textContent = artifact;
  view.artifact.title = artifact;
}

function updateActionLabels(actions) {
  const analysisReady = stageTone(state.workflow?.stages?.analysis?.state) === "success";
  const renameReady = stageTone(state.workflow?.stages?.rename?.state) === "success";
  const rebuildDone = stageTone(state.workflow?.stages?.rebuild?.state) === "success";
  let analysisLabel = "Создать CSV";
  if (analysisReady) {
    analysisLabel = actions.analyze ? "Пересоздать CSV" : "CSV готов";
  }
  analyzeButton.textContent = analysisLabel;
  renameButton.textContent = renameReady && !actions.rename ? "Переименовано" : "Переименовать";
  rebuildButton.textContent = rebuildDone && actions.rebuild ? "Собрать ещё MP4" : "Собрать MP4";
}

function updateInteractionState() {
  const locked = state.busy || state.refreshing || state.pathPickerBusy;
  const hasFolder = folderPath().length > 0;
  const actions = state.workflow?.actions || {};
  folderInput.readOnly = state.busy;
  originalVideoInput.readOnly = locked;
  outputVideoInput.readOnly = locked;
  pickButton.disabled = locked;
  refreshButton.disabled = locked || !hasFolder;
  pickOriginalVideoButton.disabled = locked || !hasFolder;
  pickOutputVideoButton.disabled = locked || !hasFolder;
  analyzeButton.disabled = locked || !hasFolder || actions.analyze !== true;
  renameButton.disabled = locked || !hasFolder || actions.rename !== true;
  const canUndo = actions.undo === true;
  undoButton.hidden = !canUndo;
  undoButton.disabled = locked || !hasFolder || !canUndo;
  rebuildButton.disabled = locked || !hasFolder || actions.rebuild !== true;
  updateActionLabels(actions);
}

function renderWorkflow() {
  Object.keys(STAGES).forEach(renderStage);
  if (state.refreshing) {
    workflowSummary.textContent = "Проверка артефактов…";
  } else if (!state.workflow) {
    workflowSummary.textContent = folderPath() ? "Требуется обновление" : "Каталог не выбран";
  } else {
    const ready = Object.keys(STAGES).filter((key) => (
      stageTone(state.workflow?.stages?.[key]?.state) === "success"
    )).length;
    workflowSummary.textContent = `Подтверждено этапов: ${ready} из 3`;
  }
  updateInteractionState();
}

function renderLogs() {
  logNode.textContent = state.logLines.length > 0
    ? `${state.logLines.join("\n")}\n`
    : "Журнал пока пуст.";
  logNode.scrollTop = logNode.scrollHeight;
}

function appendLog(message) {
  if (message === undefined || message === null) return;
  const lines = String(message).replaceAll("\r\n", "\n").split("\n");
  if (lines.at(-1) === "") lines.pop();
  state.logLines.push(...lines);
  if (state.logLines.length > MAX_UI_LOG_LINES) {
    state.logLines.splice(0, state.logLines.length - MAX_UI_LOG_LINES);
  }
  renderLogs();
}

function setTerminalState(value) {
  const normalized = String(value || "idle").trim().toLowerCase();
  const labels = {
    idle: "ожидание",
    queued: "в очереди",
    running: "в работе",
    completed: "успешно",
    ok: "успешно",
    failed: "ошибка",
    error: "ошибка",
    interrupted: "прервано",
  };
  const visible = Object.hasOwn(labels, normalized) ? normalized : "idle";
  terminalStatus.className = `terminal-status ${visible}`;
  terminalStatus.textContent = labels[visible];
}

function setJobProgress(progress, message) {
  const normalized = clampProgress(progress);
  progressNode.textContent = `${normalized}%`;
  progressBar.value = normalized;
  if (message) statusNode.textContent = String(message);
}

function responseDetail(body, fallback) {
  if (typeof body?.detail === "string") return body.detail;
  if (Array.isArray(body?.detail)) {
    return body.detail.map((item) => item?.msg || String(item)).join("; ");
  }
  return fallback;
}

async function requestJson(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(options.body ? {"Content-Type": "application/json"} : {}),
    },
    ...options,
  });
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (!response.ok) {
    throw new Error(responseDetail(body, response.statusText || "Не удалось выполнить запрос."));
  }
  return body;
}

function post(path, payload = {}) {
  return requestJson(path, {method: "POST", body: JSON.stringify(payload)});
}

function applyWorkflowPayload(payload) {
  const workflow = payload?.workflow;
  if (!workflow || typeof workflow !== "object") {
    throw new Error("Сервис не вернул состояние этапов каталога.");
  }
  const selectedFolder = String(payload.folder || workflow.folder || folderPath()).trim();
  if (selectedFolder) folderInput.value = selectedFolder;
  state.workflow = workflow;
  state.lastRefreshedFolder = pathKey(selectedFolder);
  renderWorkflow();
}

function clearWorkflow(clearVideoPaths = false) {
  state.workflow = null;
  state.lastRefreshedFolder = "";
  if (clearVideoPaths) {
    originalVideoInput.value = "";
    outputVideoInput.value = "";
  }
  renderWorkflow();
}

function showError(error) {
  clearPollTimer();
  closeEventStream();
  state.busy = false;
  state.refreshing = false;
  state.pathPickerBusy = false;
  setTerminalState("error");
  setJobProgress(progressBar.value, `Ошибка: ${error.message}`);
  appendLog(`Ошибка: ${error.message}`);
  renderWorkflow();
}

async function refreshWorkflow(options = {}) {
  const selectedFolder = folderPath();
  if (!selectedFolder) {
    state.refreshing = false;
    clearWorkflow(false);
    return false;
  }
  const generation = state.refreshGeneration + 1;
  state.refreshGeneration = generation;
  state.refreshing = true;
  renderWorkflow();
  if (!options.silent) appendLog("Проверяю артефакты выбранного каталога.");
  if (!options.preserveStatus) {
    setTerminalState("running");
    setJobProgress(0, "Проверяются результаты предыдущих этапов.");
  }
  try {
    const result = await post("/api/refresh", {folder: selectedFolder});
    if (generation !== state.refreshGeneration || pathKey(selectedFolder) !== pathKey(folderPath())) {
      return false;
    }
    applyWorkflowPayload(result);
    if (!options.preserveStatus) {
      setTerminalState("idle");
      setJobProgress(0, "Состояние каталога обновлено.");
    }
    if (!options.silent) appendLog("Состояние этапов обновлено по найденным артефактам.");
    return true;
  } finally {
    if (generation === state.refreshGeneration) {
      state.refreshing = false;
      renderWorkflow();
    }
  }
}

async function runRefresh(options = {}) {
  try {
    await refreshWorkflow(options);
  } catch (error) {
    showError(error);
  }
}

async function pickFolder() {
  if (state.busy || state.refreshing || state.pathPickerBusy) return;
  state.refreshing = true;
  renderWorkflow();
  setTerminalState("running");
  setJobProgress(0, "Ожидается выбор каталога.");
  appendLog("Открываю системный диалог выбора каталога.");
  try {
    const result = await post("/api/pick");
    if (!result.folder) {
      setTerminalState("idle");
      setJobProgress(0, "Выбор каталога отменён.");
      appendLog("Выбор каталога отменён.");
      return;
    }
    folderInput.value = String(result.folder);
    clearWorkflow(true);
    if (result.workflow) applyWorkflowPayload(result);
  } finally {
    state.refreshing = false;
    renderWorkflow();
  }
  await refreshWorkflow({silent: true});
  setTerminalState("idle");
  setJobProgress(0, "Каталог выбран, артефакты проверены.");
  appendLog("Каталог выбран, состояние этапов обновлено.");
}

async function chooseVideo(kind, target) {
  if (state.busy || state.refreshing || state.pathPickerBusy) return;
  state.pathPickerBusy = true;
  updateInteractionState();
  try {
    const result = await post("/api/pick-video", {kind, folder: folderPath()});
    if (result.path) {
      target.value = String(result.path);
      const message = kind === "source" ? "Исходный MP4 выбран." : "Выходной MP4 выбран.";
      statusNode.textContent = message;
      appendLog(message);
    }
  } finally {
    state.pathPickerBusy = false;
    updateInteractionState();
  }
}

async function loadJob(jobId) {
  return requestJson(`/api/job/${encodeURIComponent(jobId)}`);
}

function renderJob(job) {
  state.currentJob = job;
  const logs = Array.isArray(job.logs) ? job.logs.map(String) : [];
  state.logLines = logs.slice(-MAX_UI_LOG_LINES);
  renderLogs();
  setJobProgress(job.progress, job.message || "Задача выполняется.");
  state.busy = Boolean(job.active) && !job.terminal;
  setTerminalState(state.busy ? "running" : job.status);
  updateInteractionState();
}

function closeEventStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function clearPollTimer() {
  if (state.pollTimer !== null) {
    clearTimeout(state.pollTimer);
    state.pollTimer = null;
  }
}

async function refreshAfterTerminal(job) {
  const jobId = String(job?.job_id || "");
  if (!jobId || state.refreshedTerminalJobId === jobId) return;
  state.refreshedTerminalJobId = jobId;
  await refreshWorkflow({silent: true, preserveStatus: true});
  appendLog("Артефакты каталога повторно проверены после завершения задачи.");
}

async function finishJob(jobId) {
  if (String(state.currentJob?.job_id || "") !== String(jobId)) return;
  closeEventStream();
  const job = await loadJob(jobId);
  renderJob(job);
  await refreshAfterTerminal(job);
}

function applyStreamEvent(jobId, event) {
  if (String(state.currentJob?.job_id || "") !== String(jobId)) return;
  if (event.type === "log") {
    appendLog(event.message);
    return;
  }
  if (event.type === "job") {
    const logs = state.currentJob.logs || [];
    state.currentJob = {...state.currentJob, ...event, logs};
    setJobProgress(event.progress, event.message);
    state.busy = event.active !== false && event.terminal !== true;
    setTerminalState(state.busy ? "running" : event.status);
    updateInteractionState();
    return;
  }
  if (event.type === "done") {
    finishJob(jobId).catch(showError);
  }
}

function validatedJobId(value) {
  const jobId = String(value || "");
  if (!JOB_ID_PATTERN.test(jobId)) {
    throw new Error("Сервис вернул некорректный идентификатор задачи.");
  }
  return jobId;
}

function listenJob(jobId, cursor = 0) {
  closeEventStream();
  const safeJobId = validatedJobId(jobId);
  state.eventCursor = Math.max(0, Number(cursor) || 0);
  const safeCursor = Math.floor(state.eventCursor);
  const streamPath = `/api/stream/${safeJobId}?cursor=${safeCursor}`;
  const stream = new EventSource(streamPath);
  state.eventSource = stream;
  let terminalReceived = false;
  stream.onmessage = (message) => {
    if (!message.data || String(state.currentJob?.job_id || "") !== safeJobId) return;
    const receivedId = Number.parseInt(message.lastEventId, 10);
    if (Number.isFinite(receivedId)) state.eventCursor = Math.max(state.eventCursor, receivedId);
    try {
      const event = JSON.parse(message.data);
      terminalReceived = event.type === "done";
      applyStreamEvent(safeJobId, event);
    } catch {
      appendLog("Получено некорректное событие задачи.");
    }
  };
  stream.onerror = () => {
    stream.close();
    if (state.eventSource === stream) state.eventSource = null;
    if (!terminalReceived && state.busy && String(state.currentJob?.job_id || "") === safeJobId) {
      appendLog("Поток событий прерван, включена проверка состояния задачи.");
      state.pollTimer = setTimeout(() => pollJob(safeJobId), 900);
    }
  };
}

async function pollJob(jobId) {
  clearPollTimer();
  try {
    const job = await loadJob(jobId);
    renderJob(job);
    state.eventCursor = Math.max(state.eventCursor, Number(job.latest_event_id) || 0);
    if (job.terminal) {
      await refreshAfterTerminal(job);
    } else {
      state.pollTimer = setTimeout(() => pollJob(jobId), 600);
    }
  } catch (error) {
    showError(error);
  }
}

async function watchJob(jobId) {
  clearPollTimer();
  closeEventStream();
  const safeJobId = validatedJobId(jobId);
  const job = await loadJob(safeJobId);
  renderJob(job);
  state.eventCursor = Number(job.latest_event_id) || 0;
  if (job.terminal) {
    await refreshAfterTerminal(job);
  } else {
    listenJob(safeJobId, state.eventCursor);
  }
}

async function startJob(path, payload, message) {
  if (state.busy || state.refreshing || state.pathPickerBusy) return;
  state.busy = true;
  state.refreshedTerminalJobId = null;
  setTerminalState("running");
  setJobProgress(0, message);
  appendLog(message);
  updateInteractionState();
  try {
    const job = await post(path, payload);
    renderJob(job);
    await watchJob(job.job_id);
  } catch (error) {
    showError(error);
  }
}

function planCsvPath() {
  return String(
    state.workflow?.artifacts?.plan_csv
    || state.workflow?.stages?.analysis?.artifact_path
    || "",
  );
}

pickButton.addEventListener("click", () => {
  pickFolder().catch(showError);
});

refreshButton.addEventListener("click", () => {
  runRefresh();
});

pickOriginalVideoButton.addEventListener("click", () => {
  chooseVideo("source", originalVideoInput).catch(showError);
});

pickOutputVideoButton.addEventListener("click", () => {
  chooseVideo("output", outputVideoInput).catch(showError);
});

analyzeButton.addEventListener("click", () => {
  startJob("/api/analyze", {folder: folderPath()}, "Создаётся таблица сортировки.");
});

renameButton.addEventListener("click", () => {
  if (!window.confirm("Переименовать исходные файлы по проверенному CSV?")) return;
  startJob(
    "/api/rename",
    {folder: folderPath(), csv_path: planCsvPath(), confirm: true},
    "Проверяется план переименования.",
  );
});

undoButton.addEventListener("click", () => {
  if (!window.confirm("Вернуть исходные имена файлов? Результат сортировки кадров будет отменён.")) return;
  startJob(
    "/api/undo",
    {folder: folderPath(), confirm: true},
    "Проверяется журнал для возврата исходных имён.",
  );
});

rebuildButton.addEventListener("click", () => {
  if (!window.confirm("Собрать новый MP4 из отсортированных кадров?")) return;
  startJob(
    "/api/rebuild",
    {
      folder: folderPath(),
      original_video: originalVideoInput.value.trim(),
      output_video: outputVideoInput.value.trim(),
      confirm: true,
    },
    "Запускается сборка MP4.",
  );
});

folderInput.addEventListener("input", () => {
  if (pathKey(folderPath()) !== state.lastRefreshedFolder) {
    state.refreshGeneration += 1;
    state.refreshing = false;
    clearWorkflow(true);
    if (!state.busy) {
      setTerminalState("idle");
      setJobProgress(0, folderPath() ? "Путь изменён, ожидается проверка артефактов." : "Готово к работе.");
    }
  }
});

folderInput.addEventListener("blur", () => {
  if (folderPath() && pathKey(folderPath()) !== state.lastRefreshedFolder) runRefresh();
});

folderInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    folderInput.blur();
  }
});

clearLogsButton.addEventListener("click", () => {
  state.logLines = [];
  renderLogs();
});

window.addEventListener("error", (event) => {
  appendLog(`Необработанная ошибка интерфейса: ${event.message || "причина не указана"}`);
  setTerminalState("error");
});

window.addEventListener("unhandledrejection", (event) => {
  const message = event.reason instanceof Error ? event.reason.message : String(event.reason || "причина не указана");
  appendLog(`Необработанная ошибка операции: ${message}`);
  setTerminalState("error");
});

window.addEventListener("beforeunload", () => {
  clearPollTimer();
  closeEventStream();
});

async function initialize() {
  renderWorkflow();
  setTerminalState("idle");
  const job = await requestJson("/api/active-job");
  if (!job?.job_id) return;
  if (job.folder) {
    folderInput.value = String(job.folder);
  }
  renderJob(job);
  if (folderPath()) {
    await refreshWorkflow({silent: true, preserveStatus: true});
  }
  if (!job.terminal) {
    listenJob(job.job_id, Number(job.latest_event_id) || 0);
  } else {
    state.refreshedTerminalJobId = String(job.job_id);
  }
}

try {
  await initialize();
} catch (error) {
  showError(error);
}
