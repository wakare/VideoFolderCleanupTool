const state = {
  jobs: [],
  plan: null,
  evidence: null,
  lastCompleted: {},
};

const $ = (id) => document.getElementById(id);

function textValue(id) {
  return $(id).value.trim();
}

function numberValue(id) {
  return Number($(id).value);
}

function checked(id) {
  return $(id).checked;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch {
      message = await response.text();
    }
    throw new Error(message);
  }
  return response.json();
}

function payloadJson(payload) {
  return { method: "POST", body: JSON.stringify(payload) };
}

function artifactUrl(path) {
  return `/api/artifact?path=${encodeURIComponent(path)}`;
}

function setOutput(value) {
  $("latestOutput").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function setActive(value) {
  $("activeJob").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function setProgress(value) {
  const target = $("activeProgress");
  target.replaceChildren();
  if (!value) {
    target.className = "progress-empty";
    target.textContent = "No active job.";
    return;
  }
  target.className = "";
  target.append(progressCard(value));
}

async function submitScan() {
  const payload = {
    paths: textValue("scanPaths"),
    db: textValue("dbPath"),
    interval: numberValue("interval"),
    max_frames: numberValue("maxFrames"),
    fingerprint_mode: textValue("fingerprintMode"),
    profile_name: textValue("profileName"),
    hash_mode: textValue("hashMode"),
    workers: numberValue("workers"),
    seek_workers: numberValue("seekWorkers"),
    ffmpeg_workers: numberValue("ffmpegWorkers"),
    force: checked("forceScan"),
    prune_missing: checked("pruneMissing"),
  };
  const job = await api("/api/scan", payloadJson(payload));
  setOutput(job);
  await refreshJobs();
}

async function submitPlan() {
  const payload = {
    db: textValue("dbPath"),
    fingerprint_profile: textValue("planProfile"),
    output: textValue("planPath"),
    min_overlap: numberValue("minOverlap"),
    partial_overlap: numberValue("partialOverlap"),
    near_duplicate_similarity: numberValue("nearDuplicate"),
    hash_distance: numberValue("hashDistance"),
    candidate_mode: textValue("candidateMode"),
  };
  const job = await api("/api/plan", payloadJson(payload));
  setOutput(job);
  await refreshJobs();
}

async function submitEvidence() {
  const payload = {
    plan: textValue("planPath"),
    db: textValue("dbPath"),
    fingerprint_profile: textValue("planProfile"),
    output_dir: textValue("evidenceDir"),
    max_samples: numberValue("maxSamples"),
    hash_distance: numberValue("hashDistance"),
    screenshots: checked("screenshots"),
    screenshot_height: numberValue("screenshotHeight"),
    screenshot_workers: numberValue("screenshotWorkers"),
    include_manual: checked("includeManual"),
  };
  const job = await api("/api/evidence", payloadJson(payload));
  setOutput(job);
  await refreshJobs();
}

async function submitMove(apply) {
  if (apply && !confirm("Move selected candidates into quarantine?")) {
    return;
  }
  const payload = {
    plan: textValue("planPath"),
    quarantine: textValue("quarantinePath"),
    manifest: textValue("manifestPath"),
    apply,
  };
  const job = await api("/api/move", payloadJson(payload));
  setOutput(job);
  await refreshJobs();
}

async function refreshJobs() {
  const payload = await api("/api/jobs");
  state.jobs = payload.jobs || [];
  renderJobs();
  reconcileCompletedJobs();
}

function reconcileCompletedJobs() {
  for (const job of state.jobs) {
    if (job.status !== "completed" || state.lastCompleted[job.id]) {
      continue;
    }
    state.lastCompleted[job.id] = true;
    setOutput(job.result || job);
    if (job.kind === "scan" && job.result && job.result.profile) {
      $("planProfile").value = job.result.profile;
    }
    if (job.kind === "plan" && job.result && job.result.plan) {
      $("planPath").value = job.result.plan;
      loadPlan().catch(showError);
    }
    if (job.kind === "evidence") {
      loadEvidence().catch(showError);
    }
  }
}

async function loadPlan() {
  const path = textValue("planPath");
  if (!path) {
    return;
  }
  state.plan = await api(`/api/plan-preview?path=${encodeURIComponent(path)}`);
  renderPlan();
  updateMetrics();
}

async function loadEvidence() {
  const path = textValue("evidenceDir");
  if (!path) {
    return;
  }
  state.evidence = await api(`/api/evidence-preview?path=${encodeURIComponent(path)}`);
  renderEvidence();
  updateMetrics();
}

function renderJobs() {
  $("metricJobs").textContent = state.jobs.length;
  const active = state.jobs.find((job) => job.status === "running" || job.status === "queued");
  setActive(active || "No active job.");
  setProgress(active || null);

  const list = $("jobsList");
  list.replaceChildren();
  for (const job of state.jobs) {
    const item = document.createElement("article");
    item.className = "job-item";

    const head = document.createElement("div");
    head.className = "job-head";
    const title = document.createElement("strong");
    title.textContent = `${job.kind} ${job.id}`;
    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = job.status;
    head.append(title, badge);

    const progressDetails = progressCard(job);
    const progress = document.createElement("pre");
    progress.textContent = JSON.stringify(
      {
        created_at: job.created_at,
        started_at: job.started_at,
        finished_at: job.finished_at,
        progress: job.progress,
        result: job.result,
        error: job.error,
        logs: job.logs,
      },
      null,
      2,
    );

    item.append(head, progressDetails, progress);
    list.append(item);
  }
  updateMetrics();
}

function progressCard(job) {
  const card = document.createElement("div");
  card.className = "progress-card";

  const progress = job.progress || {};
  const stats = jobProgressStats(job);

  const header = document.createElement("div");
  header.className = "progress-header";
  const title = document.createElement("strong");
  title.textContent = `${job.kind} ${job.status}`;
  const percent = document.createElement("span");
  percent.textContent = stats.percent === null ? progress.phase || "queued" : `${stats.percent.toFixed(1)}%`;
  header.append(title, percent);

  const bar = document.createElement("div");
  bar.className = "progress-bar";
  const fill = document.createElement("div");
  fill.className = `progress-bar-fill ${job.status}`;
  fill.style.width = `${stats.percent === null ? 0 : Math.max(0, Math.min(100, stats.percent))}%`;
  bar.append(fill);

  const grid = document.createElement("div");
  grid.className = "progress-grid";
  grid.append(
    metricLine("Phase", progress.phase || job.status),
    metricLine("Processed", stats.processedLabel),
    metricLine("Failed", progress.failed ?? job.result?.failed ?? 0),
    metricLine("Remaining", stats.remainingLabel),
    metricLine("Elapsed", stats.elapsedLabel),
    metricLine("Avg speed", stats.rateLabel),
    metricLine("ETA", stats.etaLabel),
    metricLine("Profile", progress.profile || job.result?.profile || "n/a"),
    metricLine("Active files", activeFiles(progress).length || "n/a"),
    metricLine("Relation", stats.relationLabel),
    metricLine("Sample", stats.sampleLabel),
    metricLine("Screenshots", stats.screenshotLabel),
  );

  const current = document.createElement("div");
  current.className = "progress-current path-text";
  const active = activeFiles(progress);
  if (active.length) {
    current.append(...active.map(activeFileLine));
  } else if (progress.current) {
    current.textContent = `Current: ${progress.current}`;
  } else if (progress.last_completed) {
    current.textContent = `Last completed: ${progress.last_completed}`;
  } else {
    current.textContent = "Current: n/a";
  }

  card.append(header, bar, grid, current);
  return card;
}

function activeFiles(progress) {
  return Array.isArray(progress.active_files) ? progress.active_files : [];
}

function activeFileLine(file) {
  const line = document.createElement("div");
  const parts = [`Current: ${file.path || "unknown"}`];
  if (file.sample_index && file.sample_total) {
    parts.push(`sample ${file.sample_index}/${file.sample_total}`);
  }
  if (file.timestamp_seconds !== undefined && file.duration_seconds !== undefined) {
    parts.push(`${formatTimestamp(file.timestamp_seconds)} / ${formatTimestamp(file.duration_seconds)}`);
  }
  if (file.phase) {
    parts.push(file.phase);
  }
  line.textContent = parts.join(" | ");
  return line;
}

function jobProgressStats(job) {
  const progress = job.progress || {};
  const processed = numeric(progress.scanned ?? progress.processed ?? progress.records ?? progress.total_done);
  const total = numeric(progress.total);
  const failed = numeric(progress.failed);
  const skipped = numeric(progress.skipped);
  const percent = total > 0 && processed >= 0 ? (processed / total) * 100 : null;
  const remaining = total > 0 && processed >= 0 ? Math.max(0, total - processed) : null;
  const started = Date.parse(job.started_at || job.created_at || "");
  const finished = Date.parse(job.finished_at || "");
  const end = Number.isNaN(finished) ? Date.now() : finished;
  const elapsedSeconds = Number.isNaN(started) ? null : Math.max(0, (end - started) / 1000);
  const rate = elapsedSeconds && processed > 0 ? processed / elapsedSeconds : null;
  const etaSeconds = rate && remaining !== null ? remaining / rate : null;

  return {
    percent,
    processedLabel: total > 0
      ? `${processed} / ${total}${skipped ? `, skipped ${skipped}` : ""}`
      : `${processed}${skipped ? `, skipped ${skipped}` : ""}`,
    remainingLabel: remaining === null ? "n/a" : remaining,
    elapsedLabel: elapsedSeconds === null ? "n/a" : formatDuration(elapsedSeconds),
    rateLabel: rate ? `${rate.toFixed(3)} files/s` : "n/a",
    etaLabel: etaSeconds === null || !Number.isFinite(etaSeconds) ? "n/a" : formatDuration(etaSeconds),
    relationLabel: progress.relation_index && progress.relations_total
      ? `${progress.relation_index} / ${progress.relations_total}${progress.reason ? ` ${progress.reason}` : ""}`
      : "n/a",
    sampleLabel: progress.sample_index && progress.sample_total
      ? `${progress.sample_index} / ${progress.sample_total}`
      : "n/a",
    screenshotLabel: progress.screenshot_ok !== undefined
      ? `${progress.screenshot_ok}${progress.samples !== undefined ? ` / ${progress.samples}` : ""}`
      : "n/a",
    failed,
  };
}

function formatTimestamp(seconds) {
  const rounded = Math.max(0, Math.round(Number(seconds) || 0));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function numeric(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatDuration(seconds) {
  const rounded = Math.max(0, Math.round(seconds));
  const days = Math.floor(rounded / 86400);
  const hours = Math.floor((rounded % 86400) / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours || parts.length) parts.push(`${hours}h`);
  if (minutes || parts.length) parts.push(`${minutes}m`);
  parts.push(`${secs}s`);
  return parts.join(" ");
}

function metricLine(label, value) {
  const item = document.createElement("div");
  item.className = "progress-metric";
  const name = document.createElement("span");
  name.textContent = label;
  const data = document.createElement("strong");
  data.textContent = value;
  item.append(name, data);
  return item;
}

function renderPlan() {
  const rows = $("planRows");
  rows.replaceChildren();
  if (!state.plan) {
    return;
  }
  const filter = textValue("planFilter").toLowerCase();
  const items = state.plan.items || [];
  for (const item of items) {
    const haystack = JSON.stringify(item).toLowerCase();
    if (filter && !haystack.includes(filter)) {
      continue;
    }
    const tr = document.createElement("tr");
    addCell(tr, item.reason || "");
    addCell(tr, item.confidence ?? "");
    addCell(tr, item.offset_seconds ?? "");
    addCell(tr, item.victim || "", "path-cell");
    addCell(tr, item.keeper || "", "path-cell");
    rows.append(tr);
  }
}

function renderEvidence() {
  const grid = $("evidenceGrid");
  grid.replaceChildren();
  if (!state.evidence) {
    return;
  }
  const markdown = state.evidence.report_markdown;
  if (markdown) {
    $("markdownLink").href = artifactUrl(markdown);
  }
  const filter = textValue("evidenceFilter").toLowerCase();
  const samples = state.evidence.samples || [];
  for (const sample of samples) {
    const haystack = JSON.stringify(sample).toLowerCase();
    if (filter && !haystack.includes(filter)) {
      continue;
    }
    const item = document.createElement("article");
    item.className = "evidence-item";

    const meta = document.createElement("div");
    meta.className = "evidence-meta";
    meta.append(
      metaLine("Relation", `${sample.relation_id} / ${sample.reason}`),
      metaLine("Candidate", `${sample.candidate_hms} ${sample.candidate || ""}`),
      metaLine("Keeper", `${sample.keeper_hms} ${sample.keeper || ""}`),
      metaLine("Hamming", sample.hamming_distance === "" ? "n/a" : sample.hamming_distance),
      metaLine("Status", sample.screenshot_status || ""),
    );
    item.append(meta);

    if (sample.screenshot_status === "ok" && sample.screenshot_path) {
      const image = document.createElement("img");
      image.loading = "lazy";
      image.alt = "side-by-side frame comparison";
      image.src = artifactUrl(sample.screenshot_path);
      item.append(image);
    }

    grid.append(item);
  }
}

function addCell(row, value, className) {
  const cell = document.createElement("td");
  if (className) {
    cell.className = className;
  }
  cell.textContent = value;
  row.append(cell);
}

function metaLine(label, value) {
  const line = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = `${label}: `;
  const span = document.createElement("span");
  span.className = label === "Candidate" || label === "Keeper" ? "path-text" : "";
  span.textContent = value;
  line.append(strong, span);
  return line;
}

function updateMetrics() {
  $("metricCandidates").textContent = state.plan ? state.plan.item_count || 0 : 0;
  $("metricManual").textContent = state.plan ? state.plan.manual_review_count || 0 : 0;
  const evidenceCount = state.evidence
    ? (state.evidence.summary && state.evidence.summary.samples) || (state.evidence.samples || []).length
    : 0;
  $("metricEvidence").textContent = evidenceCount;
}

function showError(error) {
  setOutput(`Error: ${error.message || error}`);
}

function bindTabs() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const item of document.querySelectorAll(".tab")) {
        item.classList.toggle("active", item === tab);
      }
      for (const panel of document.querySelectorAll(".tab-panel")) {
        panel.classList.toggle("active", panel.id === tab.dataset.tab);
      }
    });
  }
}

async function checkHealth() {
  try {
    await api("/api/health");
    $("serverStatus").className = "status-dot ok";
    $("serverStatusText").textContent = "Ready";
  } catch (error) {
    $("serverStatus").className = "status-dot error";
    $("serverStatusText").textContent = "Offline";
  }
}

function bindActions() {
  $("scanButton").addEventListener("click", () => submitScan().catch(showError));
  $("planButton").addEventListener("click", () => submitPlan().catch(showError));
  $("evidenceButton").addEventListener("click", () => submitEvidence().catch(showError));
  $("dryRunButton").addEventListener("click", () => submitMove(false).catch(showError));
  $("applyButton").addEventListener("click", () => submitMove(true).catch(showError));
  $("reloadPlanButton").addEventListener("click", () => loadPlan().catch(showError));
  $("reloadEvidenceButton").addEventListener("click", () => loadEvidence().catch(showError));
  $("planFilter").addEventListener("input", renderPlan);
  $("evidenceFilter").addEventListener("input", renderEvidence);
}

bindTabs();
bindActions();
checkHealth();
refreshJobs().catch(showError);
setInterval(refreshJobs, 1500);
