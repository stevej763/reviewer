"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { providers: [], defaultProvider: null, styles: {} };
const DRAFT_KEY = "reviewer-draft";

// ---------- helpers ----------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 409 && path !== "/api/setup") {
    boot();
    throw new Error((data && data.detail) || "Setup needed");
  }
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Please unlock again.");
  }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return data;
}

function isoDate(d) {
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}
const today = () => isoDate(new Date());

function prettyDate(iso) {
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
}

async function busy(button, label, fn) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await fn(); } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function fillProviders(select) {
  select.replaceChildren(...state.providers.map((p) =>
    el("option", { value: p.name, selected: p.name === state.defaultProvider },
      `${p.name === "claude" ? "Claude" : "Ollama"} · ${p.model}`)));
  select.hidden = state.providers.length < 2;
}

// ---------- auth ----------

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#pin").value = "";
  setTimeout(() => $("#pin").focus(), 50);
}

async function refreshSession() {
  const s = await api("/api/session");
  state.providers = s.providers || [];
  state.defaultProvider = s.default_provider;
  state.styles = s.report_styles;
  fillProviders($("#entry-provider"));
  fillProviders($("#rep-provider"));
  $("#log-error").textContent = s.authenticated && !state.providers.length
    ? "No AI model is set up yet, so entries will be saved without a summary. Add one in Settings." : "";
  return s;
}

async function boot() {
  const s = await refreshSession();
  if (s.setup_required) return showSetup(s.defaults);
  if (!s.authenticated) return showLogin();
  $("#login").hidden = true;
  $("#setup").hidden = true;
  $("#app").hidden = false;
  $("#rep-style").replaceChildren(...Object.entries(state.styles).map(([k, v]) => el("option", { value: k }, v)));
  showView(location.hash.slice(1) || "log");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { pin: $("#pin").value } });
    await boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
    $("#pin").value = "";
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// ---------- navigation ----------

const TITLES = { log: "Today", history: "History", reports: "Reports", settings: "Settings" };

function showView(name) {
  if (!TITLES[name]) name = "log";
  for (const v of Object.keys(TITLES)) $(`#view-${v}`).hidden = v !== name;
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("#view-title").textContent = TITLES[name];
  history.replaceState(null, "", `#${name}`);
  if (name === "log") loadLatest();
  if (name === "history") loadHistory();
  if (name === "reports") loadReports();
  if (name === "settings") loadSettings();
  $("#open-settings").classList.toggle("active", name === "settings");
}
$("#open-settings").addEventListener("click", () => showView("settings"));
document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

// ---------- recording ----------

const rec = { recorder: null, chunks: [], stream: null, started: 0, timer: null, mime: "" };

function pickMime() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return options.find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

function setRecordUI(mode, label) {
  const btn = $("#record");
  btn.classList.toggle("recording", mode === "recording");
  btn.classList.toggle("busy", mode === "busy");
  btn.disabled = mode === "busy";
  btn.setAttribute("aria-label", mode === "recording" ? "Stop recording" : "Start recording");
  $("#record-label").textContent = label;
  if (mode !== "recording") $("#record-timer").textContent = "";
}

async function startRecording() {
  $("#log-error").textContent = "";
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    $("#log-error").textContent = "Recording needs HTTPS (or localhost) and a modern browser. You can still type.";
    return;
  }
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    $("#log-error").textContent = "Microphone permission was denied.";
    return;
  }
  rec.mime = pickMime();
  rec.chunks = [];
  rec.recorder = new MediaRecorder(rec.stream, rec.mime ? { mimeType: rec.mime } : undefined);
  rec.recorder.ondataavailable = (e) => { if (e.data.size) rec.chunks.push(e.data); };
  rec.recorder.onstop = uploadRecording;
  rec.recorder.start(1000);
  rec.started = Date.now();
  setRecordUI("recording", "Listening… tap to stop");
  rec.timer = setInterval(() => {
    const s = Math.floor((Date.now() - rec.started) / 1000);
    $("#record-timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }, 250);
}

function stopRecording() {
  clearInterval(rec.timer);
  rec.recorder.stop();
  rec.stream.getTracks().forEach((t) => t.stop());
}

async function uploadRecording() {
  const type = rec.recorder.mimeType || rec.mime || "audio/webm";
  rec.pending = new Blob(rec.chunks, { type });
  rec.recorder = null;
  await sendRecording();
}

// Uploads the last recording. The server stores it and answers straight away;
// transcription and the summary happen in the background.
async function sendRecording() {
  const blob = rec.pending;
  if (!blob) return;
  $("#retry-upload").hidden = true;
  setRecordUI("busy", "Saving your recording…");
  const form = new FormData();
  form.append("audio", blob, blob.type.includes("mp4") ? "recording.mp4" : "recording.webm");
  form.append("date", $("#entry-date").value || today());
  if ($("#entry-provider").value) form.append("provider", $("#entry-provider").value);
  try {
    const entry = await api("/api/recordings", { method: "POST", form });
    rec.pending = null;
    $("#latest").prepend(renderSummary(entry, { onChange: loadLatest }));
    setRecordUI("idle", "Saved ✓ Tap to record more");
    toast("Recording saved. Processing in the background");
  } catch (err) {
    $("#log-error").textContent = `Couldn't save the recording: ${err.message}`;
    setRecordUI("idle", "Recording not saved yet");
    $("#retry-upload").hidden = false;
  }
}
$("#retry-upload").addEventListener("click", sendRecording);

$("#record").addEventListener("click", () => {
  if (rec.recorder && rec.recorder.state === "recording") stopRecording();
  else startRecording();
});

// ---------- entries ----------

const PENDING = { transcribing: "Transcribing your recording…", summarising: "Writing your highlights…" };

function renderSummary(entry, { onChange } = {}) {
  const s = entry.summary;
  const card = el("article", { class: "card entry", "data-entry-id": entry.id, "data-status": entry.status });
  card._opts = { onChange };
  const meta = PENDING[entry.status] ? "processing" : entry.provider ? `${entry.provider} · ${entry.model}` : "not summarised";
  card.append(el("div", { class: "entry-head" },
    el("span", { class: "entry-date" }, prettyDate(entry.date)),
    el("span", { class: "entry-meta" }, meta)));

  if (PENDING[entry.status]) {
    card.append(el("div", { class: "processing" }, el("span", { class: "spinner", "aria-hidden": "true" }),
      el("div", {}, el("b", {}, PENDING[entry.status]),
        el("div", { class: "muted small" }, "Saved safely. You can leave this page; it carries on in the background."))));
    watchPending();
  }
  if (entry.status === "failed") {
    card.append(el("div", { class: "warn" }, el("div", {}, entry.error || "Something went wrong."),
      el("button", {
        class: "btn small",
        onclick: async (e) => {
          await busy(e.currentTarget, "Retrying…", async () => {
            try {
              const updated = await api(`/api/entries/${entry.id}/retry`, { method: "POST" });
              card.replaceWith(renderSummary(updated, { onChange }));
            } catch (err) { toast(err.message); }
          });
        },
      }, "Try again")));
  }

  if (s) {
    if (s.headline) card.append(el("div", { class: "headline" }, s.headline));
    if (s.highlights.length) {
      card.append(el("ul", { class: "highlights" }, s.highlights.map((h) => el("li", {},
        el("div", {}, el("span", { class: "tag" }, h.category), h.text),
        h.impact ? el("div", { class: "impact" }, h.impact) : null))));
    }
    const facts = [["Wins", s.wins], ["Projects", s.projects], ["People", s.people],
      ["Challenges", s.challenges], ["Follow-ups", s.follow_ups]].filter(([, v]) => v && v.length);
    if (facts.length) {
      card.append(el("div", { class: "facts" }, facts.map(([k, v]) => el("div", {}, el("b", {}, `${k}: `), v.join(" · ")))));
    }
    if (s.tags.length) card.append(el("div", {}, s.tags.map((t) => el("span", { class: "tag" }, t))));
  }

  if (entry.text) card.append(el("details", {}, el("summary", {}, "What you said"), el("pre", {}, entry.text)));

  const actions = el("div", { class: "entry-actions" });
  const busyNow = Boolean(PENDING[entry.status]);
  if (state.providers.length && !busyNow && entry.text) {
    for (const p of state.providers) {
      actions.append(el("button", {
        class: "btn",
        onclick: async (e) => {
          try {
            const updated = await api(`/api/entries/${entry.id}/summarize`, { method: "POST", body: { provider: p.name } });
            card.replaceWith(renderSummary(updated, { onChange }));
          } catch (err) { toast(err.message); }
        },
      }, state.providers.length > 1 ? `Redo with ${p.name === "claude" ? "Claude" : "Ollama"}` : "Redo summary"));
    }
  }
  if (!busyNow && entry.text) {
    actions.append(el("button", {
      class: "btn",
      onclick: () => editEntry(entry, card, onChange),
    }, "Edit"));
  }
  actions.append(el("button", {
    class: "btn danger",
    onclick: async () => {
      if (!confirm("Delete this entry permanently?")) return;
      try {
        await api(`/api/entries/${entry.id}`, { method: "DELETE" });
        card.remove();
        toast("Deleted");
        onChange && onChange();
      } catch (err) { toast(err.message); }
    },
  }, "Delete"));
  card.append(actions);
  return card;
}

// While any entry on screen is still processing, check on it every few seconds
// and swap in the finished card.
let pendingTimer = null;
function watchPending() {
  if (pendingTimer) return;
  pendingTimer = setTimeout(async () => {
    pendingTimer = null;
    const cards = [...document.querySelectorAll('.entry[data-status="transcribing"], .entry[data-status="summarising"]')];
    if (!cards.length) return;
    for (const card of cards) {
      try {
        const entry = await api(`/api/entries/${card.dataset.entryId}`);
        if (entry.status !== card.dataset.status) {
          card.replaceWith(renderSummary(entry, card._opts));
          if (entry.status === "done") toast("Highlights ready");
          if (entry.status === "failed") toast("An entry couldn't be processed");
        }
      } catch { /* try again next round */ }
    }
    if (document.querySelector('.entry[data-status="transcribing"], .entry[data-status="summarising"]')) watchPending();
  }, 3000);
}

function editEntry(entry, card, onChange) {
  const date = el("input", { type: "date", value: entry.date });
  const text = el("textarea", { rows: 10 });
  text.value = entry.text;
  const editor = el("article", { class: "card" },
    el("label", {}, "Day", date),
    el("label", {}, "What you said", text),
    el("div", { class: "actions" },
      el("button", { class: "btn", onclick: () => editor.replaceWith(card) }, "Cancel"),
      el("button", {
        class: "btn primary",
        onclick: async (e) => {
          await busy(e.currentTarget, "Saving…", async () => {
            try {
              let updated = await api(`/api/entries/${entry.id}`, { method: "PUT", body: { date: date.value, text: text.value } });
              if (text.value !== entry.text && state.providers.length) {
                updated = await api(`/api/entries/${entry.id}/summarize`, { method: "POST", body: { provider: entry.provider || state.defaultProvider } });
              }
              editor.replaceWith(renderSummary(updated, { onChange }));
              toast(updated.status === "summarising" ? "Saved. Updating highlights…" : "Saved");
            } catch (err) { toast(err.message); }
          });
        },
      }, "Save")));
  card.replaceWith(editor);
}

$("#save-entry").addEventListener("click", async (e) => {
  $("#log-error").textContent = "";
  const text = $("#entry-text").value.trim();
  if (!text) { $("#log-error").textContent = "Say or type something first."; return; }
  await busy(e.currentTarget, "Saving…", async () => {
    try {
      const entry = await api("/api/entries", {
        method: "POST",
        body: { date: $("#entry-date").value || today(), text, provider: $("#entry-provider").value || null },
      });
      $("#entry-text").value = "";
      try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
      $("#latest").prepend(renderSummary(entry, { onChange: loadLatest }));
      toast("Saved. Writing highlights in the background");
    } catch (err) {
      $("#log-error").textContent = err.message;
    }
  });
});

// Keep unsent text if the page reloads or the phone kills the tab.
$("#entry-text").addEventListener("input", (e) => {
  try { localStorage.setItem(DRAFT_KEY, e.target.value); } catch { /* storage unavailable */ }
});

// The selected day's entries, so you can see what's still processing when you come back.
async function loadLatest() {
  const day = $("#entry-date").value || today();
  try {
    const entries = await api(`/api/entries?start=${day}&end=${day}`);
    $("#latest").replaceChildren(...entries.map((e) => renderSummary(e, { onChange: loadLatest })));
  } catch { /* shown elsewhere */ }
}
$("#entry-date").addEventListener("change", loadLatest);

// ---------- history ----------

async function loadHistory() {
  const params = new URLSearchParams();
  if ($("#hist-start").value) params.set("start", $("#hist-start").value);
  if ($("#hist-end").value) params.set("end", $("#hist-end").value);
  if (!params.toString()) params.set("limit", "100");
  $("#export-csv").href = `/api/export/highlights.csv?${new URLSearchParams(
    [...params].filter(([k]) => k !== "limit"))}`;
  const list = $("#history-list");
  try {
    const entries = await api(`/api/entries?${params}`);
    $("#hist-count").textContent = `${entries.length} entr${entries.length === 1 ? "y" : "ies"}`;
    list.replaceChildren(...(entries.length
      ? entries.map((e) => renderSummary(e, { onChange: loadHistory }))
      : [el("p", { class: "empty" }, "Nothing logged yet. Head to Log and talk about your day.")]));
  } catch (err) { toast(err.message); }
}
$("#hist-apply").addEventListener("click", loadHistory);

// ---------- reports ----------

function setRange(kind) {
  const now = new Date();
  let start = new Date(now);
  if (kind === "week") start.setDate(now.getDate() - 6);
  if (kind === "month") start.setDate(now.getDate() - 29);
  if (kind === "quarter") start = new Date(now.getFullYear(), Math.floor(now.getMonth() / 3) * 3, 1);
  if (kind === "half") start.setMonth(now.getMonth() - 6);
  if (kind === "year") start = new Date(now.getFullYear(), 0, 1);
  $("#rep-start").value = isoDate(start);
  $("#rep-end").value = isoDate(now);
  document.querySelectorAll("#range-presets .chip").forEach((c) => c.classList.toggle("active", c.dataset.range === kind));
}
document.querySelectorAll("#range-presets .chip").forEach((c) => c.addEventListener("click", () => setRange(c.dataset.range)));

function renderReport(report) {
  const body = el("div", { class: "report-body" });
  body.innerHTML = report.html; // rendered server-side with raw HTML escaped
  const exp = (fmt, label) => el("a", { class: "btn small", href: `/api/reports/${report.id}/export?format=${fmt}` }, label);
  return el("article", { class: "card" },
    el("div", { class: "entry-meta" },
      `${prettyDate(report.start)} – ${prettyDate(report.end)} · ${state.styles[report.style] || report.style} · ${report.provider} · ${report.model}`),
    el("div", { class: "exports" }, exp("docx", "Word"), exp("md", "Markdown"), exp("html", "Web page"),
      el("a", { class: "btn small", href: `/api/export/highlights.csv?start=${report.start}&end=${report.end}` }, "Spreadsheet (CSV)")),
    body);
}

async function openReport(id) {
  try {
    const report = await api(`/api/reports/${id}`);
    $("#report-view").replaceChildren(renderReport(report));
    $("#report-view").scrollIntoView({ behavior: "smooth" });
  } catch (err) { toast(err.message); }
}

async function loadReports() {
  try {
    const items = await api("/api/reports");
    $("#report-list").replaceChildren(...(items.length ? items.map((r) => el("div", {
      class: "card report-item",
      onclick: () => openReport(r.id),
    },
      el("div", {}, el("div", { class: "t" }, r.title),
        el("div", { class: "entry-meta" }, `${r.start} → ${r.end} · ${state.styles[r.style] || r.style}`)),
      el("button", {
        class: "btn small danger",
        onclick: async (e) => {
          e.stopPropagation();
          if (!confirm("Delete this report?")) return;
          await api(`/api/reports/${r.id}`, { method: "DELETE" });
          loadReports();
        },
      }, "Delete"))) : [el("p", { class: "empty" }, "No reports yet.")]));
  } catch (err) { toast(err.message); }
}

$("#make-report").addEventListener("click", async (e) => {
  $("#rep-error").textContent = "";
  await busy(e.currentTarget, "Writing… this can take a minute", async () => {
    try {
      const report = await api("/api/reports", {
        method: "POST",
        body: {
          start: $("#rep-start").value,
          end: $("#rep-end").value,
          style: $("#rep-style").value,
          notes: $("#rep-notes").value,
          provider: $("#rep-provider").value || null,
        },
      });
      await openReport(report.id);
      loadReports();
    } catch (err) { $("#rep-error").textContent = err.message; }
  });
});

// ---------- setup & settings ----------

const RECOMMENDED_MODELS = [
  { name: "qwen3:8b", size: "5.2 GB", note: "Recommended for a 16 GB Mac: good summaries, quick" },
  { name: "gemma3:12b", size: "8.1 GB", note: "Better writing, slower, uses more memory" },
  { name: "gemma3:4b", size: "3.3 GB", note: "Fastest and lightest, simpler summaries" },
  { name: "llama3.1:8b", size: "4.9 GB", note: "Solid all-rounder" },
];

const LANGUAGES = [["en", "English"], ["", "Detect automatically"], ["es", "Spanish"], ["fr", "French"],
  ["de", "German"], ["it", "Italian"], ["pt", "Portuguese"], ["nl", "Dutch"]];

function field(label, input, hint) {
  return el("label", {}, label, input, hint ? el("span", { class: "muted small" }, hint) : null);
}

function input(attrs, value) {
  const node = el(attrs.rows ? "textarea" : "input", attrs);
  node.value = value || "";
  return node;
}

function formatBytes(n) {
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`;
}

// Builds the profile / AI / speech form used by both first-time setup and Settings.
function settingsForm(cfg, { setup = false, onSaved } = {}) {
  const p = cfg.profile;
  const ai = cfg.ai;
  const f = {
    name: input({ autocomplete: "name", placeholder: "e.g. Steve" }, p.name),
    role: input({ placeholder: "e.g. Senior Software Engineer" }, p.role),
    company: input({ placeholder: "e.g. Platform team at Acme" }, p.company),
    context: input({ rows: 6, placeholder: "e.g.\nProjects: Apollo (payments rewrite), Atlas (internal admin tool)\nPeople: Sam (junior dev I mentor), Priya (product manager)\nAcronyms: PSP = payment service provider\nImpact that matters here: reliability, unblocking other teams" }, p.context),
    goals: input({ rows: 3, placeholder: "e.g. Working towards Staff Engineer; my review focuses on technical leadership and cross-team impact" }, p.goals),
    pin: input({ type: "password", inputmode: "numeric", autocomplete: "new-password", placeholder: "4 to 12 digits" }),
    pin2: input({ type: "password", inputmode: "numeric", autocomplete: "new-password" }),
    ollamaUrl: input({ placeholder: "http://host.docker.internal:11434" }, ai.ollama_url),
    ollamaModel: input({ list: "model-suggestions", autocapitalize: "off", spellcheck: "false" }, ai.ollama_model),
    claudeKey: input({ type: "password", autocomplete: "off",
      placeholder: ai.anthropic_key_set ? `Saved (${ai.anthropic_key_hint || "hidden"}). Leave blank to keep` : "sk-ant-… (optional)" }),
    claudeModel: input({}, ai.anthropic_model),
    language: el("select", {}, LANGUAGES.map(([v, l]) => el("option", { value: v, selected: v === cfg.speech.language }, l))),
  };

  const providerRadio = (value, title, sub) => el("label", { class: "radio-card" },
    el("input", { type: "radio", name: setup ? "provider-setup" : "provider", value, checked: ai.provider === value }),
    el("b", {}, title), el("span", {}, sub));
  const providers = el("div", { class: "radio-row" },
    providerRadio("ollama", "Local (Ollama)", "Private and free. Runs on your Mac"),
    providerRadio("claude", "Claude", "Best quality. Needs an API key"));

  // Ollama connection check, installed models and downloads
  const status = el("p", { class: "status-line" });
  const progress = el("div", { class: "progress", hidden: true }, el("div"));
  const downloadBtn = el("button", { type: "button", class: "btn small", hidden: true }, "Download model");
  const suggestions = el("datalist", { id: "model-suggestions" });
  const picks = el("div", { class: "model-list" });
  let installed = [];

  function renderPicks() {
    picks.replaceChildren(...RECOMMENDED_MODELS.map((m) => {
      const have = installed.some((i) => i.name === m.name || i.name === `${m.name}:latest`);
      return el("button", {
        type: "button", class: "chip model-option" + (f.ollamaModel.value === m.name ? " active" : ""),
        onclick: () => { f.ollamaModel.value = m.name; renderPicks(); updateDownload(); },
      }, `${m.name} · ${m.size}${have ? " · installed" : ""} — ${m.note}`);
    }));
    suggestions.replaceChildren(...[...new Set([...installed.map((i) => i.name), ...RECOMMENDED_MODELS.map((m) => m.name)])]
      .map((n) => el("option", { value: n })));
  }

  function updateDownload() {
    const name = f.ollamaModel.value.trim();
    const have = installed.some((i) => i.name === name || i.name === `${name}:latest`);
    downloadBtn.hidden = !name || have || !status.classList.contains("ok");
    downloadBtn.textContent = `Download ${name}`;
  }

  async function checkOllama() {
    status.className = "status-line";
    status.textContent = "Checking…";
    try {
      const res = await api("/api/ollama/models", { method: "POST", body: { url: f.ollamaUrl.value.trim() } });
      installed = res.models;
      status.className = "status-line ok";
      status.textContent = installed.length
        ? `Connected. Installed: ${installed.map((m) => `${m.name} (${formatBytes(m.size)})`).join(", ")}`
        : "Connected. No models downloaded yet; pick one below.";
    } catch (err) {
      installed = [];
      status.className = "status-line bad";
      status.textContent = err.message;
    }
    renderPicks();
    updateDownload();
  }

  async function pollDownload() {
    const st = await api("/api/ollama/pull");
    const bar = progress.firstChild;
    if (st.status === "downloading") {
      progress.hidden = false;
      bar.style.width = st.total ? `${Math.round((st.completed / st.total) * 100)}%` : "2%";
      status.className = "status-line";
      status.textContent = st.total
        ? `Downloading ${st.model}: ${formatBytes(st.completed)} of ${formatBytes(st.total)}`
        : `Downloading ${st.model}: ${st.detail || "starting"}…`;
      setTimeout(pollDownload, 1000);
    } else if (st.status === "done") {
      progress.hidden = true;
      toast(`${st.model} is ready`);
      checkOllama();
    } else if (st.status === "error") {
      progress.hidden = true;
      status.className = "status-line bad";
      status.textContent = `Download failed: ${st.error}`;
    }
  }

  downloadBtn.addEventListener("click", async () => {
    downloadBtn.hidden = true;
    try {
      await api("/api/ollama/pull", { method: "POST", body: { url: f.ollamaUrl.value.trim(), model: f.ollamaModel.value.trim() } });
      pollDownload();
    } catch (err) { status.className = "status-line bad"; status.textContent = err.message; }
  });
  f.ollamaModel.addEventListener("input", () => { renderPicks(); updateDownload(); });

  const ollamaBox = el("div", { class: "settings-form" },
    field("Ollama address", el("div", { class: "inline" }, f.ollamaUrl,
      el("button", { type: "button", class: "btn small", onclick: checkOllama }, "Check")),
    "The Ollama app on this Mac is reachable from Docker at http://host.docker.internal:11434"),
    status, progress,
    field("Model", f.ollamaModel), suggestions, picks, downloadBtn);
  const claudeBox = el("div", { class: "settings-form" },
    field("Anthropic API key", f.claudeKey, "From console.anthropic.com. Separate from a Claude subscription."),
    field("Claude model", f.claudeModel));

  const error = el("p", { class: "error", role: "alert" });
  const save = el("button", { type: "submit", class: "btn primary" }, setup ? "Finish setup" : "Save settings");

  const form = el("form", { class: "settings-form", autocomplete: "off" },
    el("section", { class: "card" },
      el("h2", {}, "About you"),
      el("p", { class: "muted small" }, "Used to tailor your summaries and reports."),
      field("Your name", f.name),
      field("Role", f.role),
      field("Team / company", f.company),
      field("Goals and what matters in your role", f.goals, "Optional. Reports will emphasise these."),
      field("Background for the AI", f.context,
        "Optional. Projects, people, team names and acronyms you mention often. Included with every summary and report, so it can make sense of what you say and spell names right.")),
    setup ? el("section", { class: "card" },
      el("h2", {}, "Choose a PIN"),
      el("p", { class: "muted small" }, "You'll type this to unlock the app. Use 6+ digits."),
      el("div", { class: "grid2" }, field("PIN", f.pin), field("Confirm PIN", f.pin2))) : null,
    el("section", { class: "card" },
      el("h2", {}, "AI model"),
      el("p", { class: "muted small" }, "Turns what you say into highlights and writes your reports."),
      providers,
      el("h3", { class: "subhead" }, "Local model (Ollama)"), ollamaBox,
      el("h3", { class: "subhead" }, "Claude (optional)"), claudeBox),
    el("section", { class: "card" },
      el("h2", {}, "Speech"),
      el("p", { class: "muted small" }, "Recordings are transcribed on this machine and never stored."),
      field("Language you speak in", f.language)),
    el("div", { class: "actions" }, save),
    error);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const provider = form.querySelector("input[type=radio]:checked")?.value || "ollama";
    const body = {
      profile: {
        name: f.name.value.trim(), role: f.role.value.trim(), company: f.company.value.trim(),
        goals: f.goals.value.trim(), context: f.context.value.trim(),
      },
      ai: {
        provider,
        ollama_url: f.ollamaUrl.value.trim(),
        ollama_model: f.ollamaModel.value.trim(),
        anthropic_api_key: f.claudeKey.value.trim() || null,
        anthropic_model: f.claudeModel.value.trim(),
      },
      speech: { language: f.language.value },
    };
    if (provider === "claude" && !body.ai.anthropic_api_key && !ai.anthropic_key_set) {
      error.textContent = "Add an Anthropic API key to use Claude, or pick the local model.";
      return;
    }
    if (setup) {
      if (f.pin.value !== f.pin2.value) { error.textContent = "The PINs don't match."; return; }
      body.pin = f.pin.value;
    }
    await busy(save, "Saving…", async () => {
      try {
        const saved = await api(setup ? "/api/setup" : "/api/settings", { method: setup ? "POST" : "PUT", body });
        onSaved && onSaved(saved);
      } catch (err) { error.textContent = err.message; }
    });
  });

  renderPicks();
  if (ai.ollama_url) checkOllama();
  // Resume showing progress if a download is already running.
  api("/api/ollama/pull").then((st) => { if (st.status === "downloading") pollDownload(); }).catch(() => {});
  return form;
}

function showSetup(defaults) {
  $("#login").hidden = true;
  $("#app").hidden = true;
  $("#setup").hidden = false;
  $("#setup-form").replaceChildren(settingsForm(defaults, {
    setup: true,
    onSaved: async () => { $("#setup").hidden = true; toast("All set"); await boot(); },
  }));
}

async function loadSettings() {
  try {
    const cfg = await api("/api/settings");
    $("#settings-form").replaceChildren(settingsForm(cfg, {
      onSaved: async () => { toast("Settings saved"); await refreshSession(); },
    }));
  } catch (err) { toast(err.message); }
}

$("#change-pin").addEventListener("click", async (e) => {
  $("#pin-error").textContent = "";
  await busy(e.currentTarget, "Changing…", async () => {
    try {
      await api("/api/settings/pin", { method: "POST", body: { current: $("#pin-current").value, new: $("#pin-new").value } });
      $("#pin-current").value = "";
      $("#pin-new").value = "";
      toast("PIN changed");
    } catch (err) { $("#pin-error").textContent = err.message; }
  });
});

// ---------- start ----------

$("#entry-date").value = today();
try { $("#entry-text").value = localStorage.getItem(DRAFT_KEY) || ""; } catch { /* ignore */ }
setRange("month");
boot().catch((err) => { document.body.prepend(el("p", { class: "error" }, err.message)); });
