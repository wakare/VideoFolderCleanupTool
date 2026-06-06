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

    item.append(head, progress);
    list.append(item);
  }
  updateMetrics();
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
