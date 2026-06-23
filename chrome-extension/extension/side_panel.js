// side_panel.js — Traduction extension (side panel)
//
// Talks to the local traduction-daemon on http://127.0.0.1:47318.
// Flow: auto-fill active tab URL → fetch scripts → POST /process → poll /status.

const DAEMON = "http://127.0.0.1:47318";
const POLL_MS = 1000;

const $ = (id) => document.getElementById(id);
const urlInput = $("url");
const scriptSelect = $("script");
const argsInput = $("args");
const goBtn = $("go");
const cancelBtn = $("cancel");
const statusBox = document.querySelector(".status");
const stateEl = $("state");
const fileEl = $("file");
const logEl = $("log");
const daemonPill = $("daemon-status");
const voicemapBox = $("voicemap");
const voicemapSpeakers = $("voicemap-speakers");
const voicemapConfirm = $("voicemap-confirm");

let currentJobId = null;
let pollTimer = null;
let defaultsByScript = {};
let currentPromptKey = null;  // identifie la question affichée (évite de re-render)

// ─── helpers ────────────────────────────────────────────────────────────────

async function daemonFetch(path, opts = {}) {
  const res = await fetch(DAEMON + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch {}
  if (!res.ok) {
    const msg = (data && data.error) || text || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function setDaemonStatus(state) {
  daemonPill.classList.remove("pill-ok", "pill-err", "pill-unknown");
  if (state === "ok") {
    daemonPill.textContent = "daemon ok";
    daemonPill.classList.add("pill-ok");
  } else if (state === "err") {
    daemonPill.textContent = "daemon off";
    daemonPill.classList.add("pill-err");
  } else {
    daemonPill.textContent = "daemon ?";
    daemonPill.classList.add("pill-unknown");
  }
}

function setState(state) {
  stateEl.textContent = state || "—";
  stateEl.className = "state " + (state || "");
}

function showStatus(show) {
  statusBox.hidden = !show;
}

function isVideoUrl(u) {
  if (!u) return false;
  try {
    const { hostname } = new URL(u);
    return /(^|\.)youtube\.com$|^youtu\.be$|(^|\.)x\.com$|(^|\.)twitter\.com$|(^|\.)theepochtimes\.com$|(^|\.)apollohealthco\.com$/.test(hostname);
  } catch { return false; }
}

// ─── init ───────────────────────────────────────────────────────────────────

async function init() {
  // 1) auto-fill the URL from the active tab, if it looks like a supported one.
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.url && isVideoUrl(tab.url)) {
      urlInput.value = tab.url;
    }
  } catch (e) { /* ignore */ }

  // 2) restore last-used script + args.
  const saved = await chrome.storage.local.get(["lastScript", "lastArgs"]);

  // 3) ping + scripts in parallel.
  try {
    const [ping, scripts] = await Promise.all([
      daemonFetch("/ping"),
      daemonFetch("/scripts"),
    ]);
    setDaemonStatus("ok");
    daemonPill.title = `traduction dir: ${ping.traduction_dir}\nyt-dlp: ${ping.ytdlp}`;
    defaultsByScript = scripts.defaults || {};
    populateScripts(scripts.scripts || [], saved.lastScript);
    if (saved.lastArgs !== undefined) argsInput.value = saved.lastArgs;
    else applyDefaultArgs();
  } catch (e) {
    setDaemonStatus("err");
    daemonPill.title = String(e);
    scriptSelect.innerHTML = `<option value="">(daemon injoignable)</option>`;
    goBtn.disabled = true;
  }
}

function populateScripts(scripts, preferred) {
  scriptSelect.innerHTML = "";
  if (!scripts.length) {
    scriptSelect.innerHTML = `<option value="">(aucun script trouvé)</option>`;
    goBtn.disabled = true;
    return;
  }
  // Preferred order: traduire, traduire-pro, doubler, then the rest.
  const priority = ["traduire", "traduire-pro", "doubler", "resumer"];
  scripts.sort((a, b) => {
    const ia = priority.indexOf(a.name);
    const ib = priority.indexOf(b.name);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.name.localeCompare(b.name);
  });
  for (const s of scripts) {
    const opt = document.createElement("option");
    opt.value = s.name;
    opt.textContent = s.name;
    scriptSelect.appendChild(opt);
  }
  if (preferred && scripts.some(s => s.name === preferred)) {
    scriptSelect.value = preferred;
  }
}

function applyDefaultArgs() {
  const name = scriptSelect.value;
  if (defaultsByScript[name] !== undefined) {
    argsInput.value = defaultsByScript[name];
  } else {
    argsInput.value = "-s en -t fr";
  }
}

scriptSelect.addEventListener("change", () => {
  // Only reset args if the user hasn't customised them.
  const current = argsInput.value.trim();
  const allDefaults = Object.values(defaultsByScript);
  if (!current || allDefaults.includes(current) || current === "-s en -t fr") {
    applyDefaultArgs();
  }
});

// ─── run a job ──────────────────────────────────────────────────────────────

goBtn.addEventListener("click", async () => {
  const url = urlInput.value.trim();
  const script = scriptSelect.value;
  const args = argsInput.value;

  if (!url) {
    flashError("URL manquante");
    return;
  }
  if (!script) {
    flashError("aucun script sélectionné");
    return;
  }

  await chrome.storage.local.set({ lastScript: script, lastArgs: args });

  goBtn.disabled = true;
  cancelBtn.hidden = false;
  showStatus(true);
  setState("pending");
  fileEl.textContent = "";
  logEl.textContent = "";
  hideVoicemap();

  try {
    const { job_id } = await daemonFetch("/process", {
      method: "POST",
      body: JSON.stringify({ url, script, args }),
    });
    currentJobId = job_id;
    pollLoop();
  } catch (e) {
    setState("error");
    logEl.textContent = String(e);
    goBtn.disabled = false;
    cancelBtn.hidden = true;
  }
});

cancelBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await daemonFetch(`/cancel/${currentJobId}`, { method: "POST" });
  } catch (e) {
    logEl.textContent += `\n[cancel] ${e}`;
  }
});

// ─── interactive voice mapping ────────────────────────────────────────────

const GENDER_ICON = { female: "♀️", male: "♂️", unknown: "❓" };

function audioUrl(path) {
  return `${DAEMON}/audio?path=${encodeURIComponent(path)}`;
}

function renderVoicemap(prompt) {
  const key = JSON.stringify((prompt.speakers || []).map(s => s.id));
  if (key === currentPromptKey) return;  // déjà affichée, ne pas écraser les choix
  currentPromptKey = key;

  voicemapSpeakers.innerHTML = "";
  const voices = prompt.voices || [];

  for (const sp of prompt.speakers || []) {
    const block = document.createElement("div");
    block.className = "vm-speaker";
    block.dataset.speaker = sp.id;

    const head = document.createElement("div");
    head.className = "vm-speaker-head";
    const idEl = document.createElement("span");
    idEl.className = "vm-speaker-id";
    idEl.textContent = sp.id;
    const meta = document.createElement("span");
    meta.className = "vm-speaker-meta";
    meta.textContent = `${GENDER_ICON[sp.gender_guess] || "❓"} F0≈${Math.round(sp.f0)}Hz · ${Math.round(sp.duration)}s`;
    head.append(idEl, meta);
    block.append(head);

    if (sp.text) {
      const txt = document.createElement("div");
      txt.className = "vm-speaker-text";
      txt.textContent = `« ${sp.text} »`;
      block.append(txt);
    }

    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = audioUrl(sp.sample);
    block.append(audio);

    const row = document.createElement("div");
    row.className = "vm-row";
    const select = document.createElement("select");
    select.className = "vm-select";
    voices.forEach((v, i) => {
      const opt = document.createElement("option");
      opt.value = v.path;
      opt.textContent = `${GENDER_ICON[v.gender] || "❓"} ${v.name}`;
      select.appendChild(opt);
    });
    if (typeof sp.suggested === "number" && voices[sp.suggested]) {
      select.value = voices[sp.suggested].path;
    }

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "vm-preview";
    preview.textContent = "▶︎ voix";
    preview.addEventListener("click", () => {
      const a = new Audio(audioUrl(select.value));
      a.play().catch(() => {});
    });

    row.append(select, preview);
    block.append(row);
    voicemapSpeakers.append(block);
  }

  voicemapBox.hidden = false;
  voicemapBox.classList.remove("busy");
  voicemapConfirm.disabled = false;
}

function hideVoicemap() {
  voicemapBox.hidden = true;
  currentPromptKey = null;
}

voicemapConfirm.addEventListener("click", async () => {
  if (!currentJobId) return;
  const map = {};
  voicemapSpeakers.querySelectorAll(".vm-speaker").forEach(block => {
    const sel = block.querySelector(".vm-select");
    if (sel && sel.value) map[block.dataset.speaker] = sel.value;
  });
  voicemapBox.classList.add("busy");
  voicemapConfirm.disabled = true;
  try {
    await daemonFetch(`/respond/${currentJobId}`, {
      method: "POST",
      body: JSON.stringify({ map }),
    });
    hideVoicemap();
  } catch (e) {
    voicemapBox.classList.remove("busy");
    voicemapConfirm.disabled = false;
    logEl.textContent += `\n[voicemap] ${e}`;
  }
});

async function pollLoop() {
  if (!currentJobId) return;
  try {
    const snap = await daemonFetch(`/status/${currentJobId}`);
    setState(snap.state);
    if (snap.file) {
      fileEl.textContent = snap.file.split("/").pop();
      fileEl.title = snap.file;
    }
    logEl.textContent = snap.log || "";
    logEl.scrollTop = logEl.scrollHeight;

    if (snap.prompt && snap.prompt.type === "voicemap_request") {
      renderVoicemap(snap.prompt);
    } else if (!voicemapBox.classList.contains("busy")) {
      hideVoicemap();
    }

    if (snap.state === "done" || snap.state === "error" || snap.state === "cancelled") {
      goBtn.disabled = false;
      cancelBtn.hidden = true;
      hideVoicemap();
      currentJobId = null;
      return;
    }
  } catch (e) {
    logEl.textContent += `\n[poll] ${e}`;
  }
  pollTimer = setTimeout(pollLoop, POLL_MS);
}

function flashError(msg) {
  showStatus(true);
  setState("error");
  logEl.textContent = msg;
}

init();
