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
  idle.busy++;  // a long request (like writing a report) counts as using the app
  let res;
  try { res = await fetch(path, opts); } finally { idle.busy--; }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 409 && data && data.detail === "Finish setting up the app first.") {
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
    if (msg && typeof msg === "object") msg = msg.message;
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
  if (s.authenticated) idle.minutes = s.auto_lock_minutes ?? 5;
  state.providers = s.providers || [];
  state.defaultProvider = s.default_provider;
  state.styles = s.report_styles;
  state.speechModels = s.speech_models || {};
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
  markActive(true);
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

$("#logout").addEventListener("click", () => lockApp());

// ---------- auto-lock ----------
//
// After a few minutes with no taps, clicks, scrolling or typing (Settings → PIN & data) the
// app locks and asks for the PIN again. The server enforces the same limit: the session
// cookie is only refreshed while you're active, so a tab left open or reopened later can't
// skip the PIN. A recording or a slow request (like writing a report) counts as activity.

const ACTIVE_KEY = "braindump-last-active";
const idle = { minutes: 5, last: Date.now(), refreshed: 0, busy: 0, locking: false };

function markActive(force = false) {
  const now = Date.now();
  if (!force && now - idle.last < 1000) return;  // pointermove fires constantly
  idle.last = now;
  try { localStorage.setItem(ACTIVE_KEY, String(now)); } catch { /* ignore */ }  // other tabs see it
  // Tell the server, at most every minute (more often for very short limits).
  const every = Math.min(60_000, (idle.minutes || 60) * 15_000);
  if ($("#app").hidden || (!force && now - idle.refreshed < every)) return;
  idle.refreshed = now;
  fetch("/api/session/active", { method: "POST", credentials: "same-origin" }).catch(() => {});
}

function checkIdle() {
  if (rec.id || idle.busy) return markActive();
  if ($("#app").hidden || !idle.minutes) return;
  let last = idle.last;
  try { last = Math.max(last, Number(localStorage.getItem(ACTIVE_KEY)) || 0); } catch { /* ignore */ }
  if (Date.now() - last >= idle.minutes * 60_000) lockApp();
}

async function lockApp() {
  if (idle.locking) return;
  idle.locking = true;
  showLogin();
  try {
    // A recording in progress is stopped and finishes saving before the session ends.
    if (rec.recorder && rec.recorder.state === "recording") stopRecording();
    while (rec.id) await new Promise((r) => setTimeout(r, 500));
    // Skip signing out if you unlocked again while the recording was saving.
    if ($("#app").hidden) await fetch("/api/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  } finally { idle.locking = false; }
}
window.braindumpLock = lockApp;  // the Mac app calls this when its window is closed

for (const type of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "scroll"]) {
  document.addEventListener(type, () => markActive(), { capture: true, passive: true });
}
setInterval(checkIdle, 15_000);
// Timers don't run while a phone is locked or a laptop sleeps, so check straight away on return.
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkIdle(); });

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
//
// A recording is uploaded in pieces while you talk: the server starts an entry, each
// CHUNK_MS of audio is sent as soon as the browser has it (retrying until it gets through),
// and stopping just tells the server to put the pieces together. If the tab closes or the
// phone locks mid-way, everything already uploaded is kept and transcribed.

const CHUNK_MS = 10_000;
const rec = {
  recorder: null, stream: null, started: 0, timer: null, mime: "",
  id: null, seq: 0, queue: [], uploading: false, stopped: false, sent: 0, retryDelay: 0, wakeLock: null,
};

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

const clock = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

function recordingStatus() {
  const waiting = rec.queue.length;
  const saved = `saved up to ${clock(rec.sent * CHUNK_MS)}`;
  if (rec.retryDelay) return `Connection problem, retrying… (${waiting} piece${waiting === 1 ? "" : "s"} waiting)`;
  return rec.stopped ? `Finishing… ${saved}` : `Listening… tap to stop · ${saved}`;
}

// ----- keep the screen on, and warn before leaving, while a recording is in flight -----

async function keepAwake() {
  try { rec.wakeLock = await navigator.wakeLock?.request("screen"); } catch { /* not supported or refused */ }
}
document.addEventListener("visibilitychange", () => {
  // The browser drops the wake lock when the page is hidden; take it again on return.
  if (document.visibilityState === "visible" && rec.id && (!rec.wakeLock || rec.wakeLock.released)) keepAwake();
});
function letSleep() {
  try { rec.wakeLock?.release(); } catch { /* ignore */ }
  rec.wakeLock = null;
}
window.addEventListener("beforeunload", (e) => {
  if (rec.id) { e.preventDefault(); e.returnValue = ""; }  // recording or pieces still uploading
});

// ----- recording -----

async function startRecording() {
  $("#log-error").textContent = "";
  $("#retry-upload").hidden = true;
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
  setRecordUI("busy", "Starting…");
  try {
    const entry = await api("/api/recordings/start", {
      method: "POST",
      body: { date: $("#entry-date").value || today(), provider: $("#entry-provider").value || null, mime: rec.mime },
    });
    Object.assign(rec, { id: entry.id, seq: 0, queue: [], sent: 0, stopped: false, retryDelay: 0 });
  } catch (err) {
    rec.stream.getTracks().forEach((t) => t.stop());
    setRecordUI("idle", "Tap to talk about your day");
    $("#log-error").textContent = `Couldn't start recording: ${err.message}`;
    return;
  }
  rec.recorder = new MediaRecorder(rec.stream, rec.mime ? { mimeType: rec.mime } : undefined);
  rec.recorder.ondataavailable = (e) => {
    if (e.data.size) { rec.queue.push({ seq: rec.seq++, blob: e.data }); pumpChunks(); }
  };
  rec.recorder.onstop = () => { rec.stopped = true; pumpChunks(); };
  rec.recorder.start(CHUNK_MS);
  rec.started = Date.now();
  keepAwake();
  setRecordUI("recording", recordingStatus());
  rec.timer = setInterval(() => {
    $("#record-timer").textContent = clock(Date.now() - rec.started);
    $("#record-label").textContent = recordingStatus();
  }, 250);
}

function stopRecording() {
  clearInterval(rec.timer);
  setRecordUI("busy", "Finishing…");
  rec.recorder.stop();  // fires a last dataavailable, then onstop
  rec.stream.getTracks().forEach((t) => t.stop());
}

// Sends queued pieces one at a time, in order. A failed piece is retried with backoff
// (2s, 4s… up to 30s) until it gets through; nothing is dropped while the page is open.
async function pumpChunks() {
  if (rec.uploading || !rec.id) return;
  rec.uploading = true;
  try {
    while (rec.queue.length) {
      const { seq, blob } = rec.queue[0];
      try {
        const res = await fetch(`/api/recordings/${rec.id}/chunks/${seq}`, {
          method: "PUT", body: blob, credentials: "same-origin",
          headers: { "Content-Type": "application/octet-stream" },
        });
        if (res.status === 401) { showLogin(); throw new Error("signed out"); }
        if (res.status === 409) {
          // The server already wrapped this recording up (it looked abandoned); stop sending.
          rec.queue = [];
          $("#log-error").textContent = "This recording was saved up to the point the connection dropped. Start a new one to add more.";
          break;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        rec.queue.shift();
        rec.sent = seq + 1;
        rec.retryDelay = 0;
      } catch {
        rec.retryDelay = Math.min((rec.retryDelay || 1000) * 2, 30_000);
        if (rec.stopped) setRecordUI("busy", recordingStatus());
        await new Promise((r) => setTimeout(r, rec.retryDelay));
      }
    }
  } finally {
    rec.uploading = false;
  }
  if (rec.stopped && !rec.queue.length) finishRecording();
}

async function finishRecording() {
  if (!rec.id) return;
  setRecordUI("busy", "Saving your recording…");
  try {
    const entry = await api(`/api/recordings/${rec.id}/finish`, { method: "POST", body: { chunks: rec.seq } });
    rec.id = null;
    letSleep();
    addToLatest(renderSummary(entry, { onChange: loadLatest }));
    setRecordUI("idle", "Saved ✓ Tap to record more");
    toast("Recording saved. Processing in the background");
  } catch (err) {
    if (/already been finished/.test(err.message)) {
      rec.id = null;
      letSleep();
      setRecordUI("idle", "Tap to talk about your day");
      loadLatest();
      return;
    }
    $("#log-error").textContent = `Couldn't finish saving the recording: ${err.message}`;
    setRecordUI("idle", "Recording uploaded, not finished yet");
    $("#retry-upload").hidden = false;
  }
}
$("#retry-upload").addEventListener("click", () => { $("#retry-upload").hidden = true; finishRecording(); });

$("#record").addEventListener("click", () => {
  if (rec.recorder && rec.recorder.state === "recording") stopRecording();
  else if (!rec.id) startRecording();
});

// ---------- entries ----------

const PENDING = { transcribing: "Transcribing your recording…", summarising: "Writing your highlights…" };

function summaryParts(s) {
  const parts = [];
  if (s.headline) parts.push(el("div", { class: "headline" }, s.headline));
  if (s.highlights && s.highlights.length) {
    parts.push(el("ul", { class: "highlights" }, s.highlights.map((h) => el("li", {},
      el("div", {}, el("span", { class: "tag" }, h.category), h.text),
      h.impact ? el("div", { class: "impact" }, h.impact) : null))));
  }
  const facts = [["Wins", s.wins], ["Projects", s.projects], ["People", s.people],
    ["Challenges", s.challenges], ["Follow-ups", s.follow_ups]].filter(([, v]) => v && v.length);
  if (facts.length) {
    parts.push(el("div", { class: "facts" }, facts.map(([k, v]) => el("div", {}, el("b", {}, `${k}: `), v.join(" · ")))));
  }
  if (s.tags && s.tags.length) parts.push(el("div", {}, s.tags.map((t) => el("span", { class: "tag" }, t))));
  return parts;
}

// The end-of-day summary combining all of a day's entries. It's only written when you ask.
// `day` is the stored summary, or null when there isn't one yet (then a "create" card is shown).
function renderDay(day, date, entryCount) {
  date = day ? day.date : date;
  const card = el("article", { class: "card entry day-summary", "data-day": date,
    "data-status": day ? day.status : "none" });
  const busyNow = day && day.status === "summarising";
  const count = day && !day.stale ? day.entry_count : entryCount ?? day?.entry_count;
  card.append(el("div", { class: "entry-head" },
    el("span", { class: "day-label" }, "End of day"),
    el("span", { class: "entry-meta" }, `${prettyDate(date)} · ${count} entries`)));

  const generate = (label, primary) => el("button", {
    class: primary ? "btn primary" : "btn",
    onclick: async (e) => {
      await busy(e.currentTarget, "Starting…", async () => {
        try { card.replaceWith(renderDay(await api(`/api/days/${date}/generate`, { method: "POST" }))); }
        catch (err) { toast(err.message); }
      });
    },
  }, label);

  if (!day) {
    card.append(el("p", { class: "muted small" }, "Combine this day's entries into one end-of-day summary."));
    card.append(el("div", { class: "entry-actions" }, generate("Create end-of-day summary", true)));
    return card;
  }
  if (busyNow) {
    card.append(el("div", { class: "processing" }, el("span", { class: "spinner", "aria-hidden": "true" }),
      el("b", {}, day.summary ? "Updating the end-of-day summary…" : "Combining this day's entries…", " ",
        etaLabel(day.eta_seconds)),
      el("button", {
        class: "btn small",
        onclick: async (e) => {
          await busy(e.currentTarget, "Cancelling…", async () => {
            try { await api(`/api/days/${date}/cancel`, { method: "POST" }); watchPending(); }
            catch (err) { toast(err.message); }
          });
        },
      }, "Cancel")));
    watchPending();
  }
  if (day.status === "failed") card.append(el("div", { class: "warn" }, day.error || "Couldn't build the end-of-day summary."));
  if (day.stale) {
    card.append(el("div", { class: "warn" }, "Entries have changed since this was written."));
  }
  if (day.summary) card.append(...summaryParts(day.summary));
  if (!busyNow) {
    card.append(el("div", { class: "entry-actions" },
      day.status === "failed" ? generate("Try again", true) : day.stale ? generate("Update summary", true) : generate("Rebuild")));
  }
  return card;
}

// The day card for a date: the stored summary, a "create" card, or nothing (fewer than 2 entries).
function dayCardFor(date, days, entries) {
  const done = entries.filter((e) => e.date === date && e.status === "done").length;
  const summary = days.find((d) => d.date === date);
  if (summary) return renderDay(summary, date, done);
  return done >= 2 ? renderDay(null, date, done) : null;
}

async function fetchDays(start, end) {
  try { return await api(`/api/days?start=${start}&end=${end}`); } catch { return []; }
}

function renderSummary(entry, { onChange } = {}) {
  const s = entry.summary;
  const card = el("article", { class: "card entry", "data-entry-id": entry.id, "data-status": entry.status });
  card._opts = { onChange };
  const meta = PENDING[entry.status] ? "processing" : entry.provider ? `${entry.provider} · ${entry.model}` : "not summarised";
  card.append(el("div", { class: "entry-head" },
    el("span", { class: "entry-date" }, prettyDate(entry.date)),
    el("span", { class: "entry-meta" }, meta)));

  if (PENDING[entry.status]) {
    const cancel = entry.status === "summarising" ? el("button", {
      class: "btn small",
      onclick: async (e) => {
        await busy(e.currentTarget, "Cancelling…", async () => {
          try { await api(`/api/entries/${entry.id}/cancel`, { method: "POST" }); watchPending(); }
          catch (err) { toast(err.message); }
        });
      },
    }, "Cancel") : null;
    const bar = entry.status === "transcribing" ? progressBar(entry.progress) : null;
    card.append(el("div", { class: "processing" }, el("span", { class: "spinner", "aria-hidden": "true" }),
      el("div", { class: "processing-body" }, el("b", {}, PENDING[entry.status], " ", etaLabel(entry.eta_seconds)), bar,
        el("div", { class: "muted small" }, "Saved safely. You can leave this page; it carries on in the background.")),
      cancel));
    watchPending();
  }
  if (entry.status === "recording") {
    if (entry.id === rec.id) {
      card.append(el("div", { class: "processing" }, el("span", { class: "spinner", "aria-hidden": "true" }),
        el("b", {}, "Recording… each piece is saved as you talk")));
    } else {
      card.append(el("div", { class: "warn" },
        el("div", {}, "This recording didn't finish uploading (the page closed or lost connection). Everything that reached the server is safe. It's processed automatically after 10 minutes, or now:"),
        el("button", {
          class: "btn small",
          onclick: async (e) => {
            await busy(e.currentTarget, "Processing…", async () => {
              try {
                const updated = await api(`/api/recordings/${entry.id}/finish`, { method: "POST", body: { chunks: 0, partial: true } });
                card.replaceWith(renderSummary(updated, { onChange }));
              } catch (err) { toast(err.message); }
            });
          },
        }, "Process what was saved")));
    }
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

  if (s) card.append(...summaryParts(s));

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

// "about 40s left", from the server's estimate (learned from how long past jobs took).
function etaText(seconds) {
  if (seconds == null) return "";
  if (seconds === 0) return "taking longer than usual…";
  if (seconds <= 3) return "almost done";
  if (seconds < 60) return `about ${Math.ceil(seconds / 5) * 5}s left`;
  const mins = Math.round(seconds / 60);
  return `about ${mins} min left`;
}

function etaLabel(seconds) {
  return el("span", { class: "muted small eta-label" }, etaText(seconds));
}

// Transcription progress: how far through the recording Whisper has got.
function progressBar(fraction) {
  const fill = el("div");
  const label = el("span", { class: "muted small progress-label" });
  const wrap = el("div", { class: "progress-row" }, el("div", { class: "progress" }, fill), label);
  setProgress(wrap, fraction);
  return wrap;
}

function setProgress(wrap, fraction) {
  const fill = wrap.querySelector(".progress > div");
  const label = wrap.querySelector(".progress-label");
  if (fraction == null) {
    fill.style.width = "0%";
    label.textContent = "Waiting to start…";
  } else {
    const pct = Math.round(fraction * 100);
    fill.style.width = `${Math.max(pct, 2)}%`;
    label.textContent = `${pct}%`;
  }
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
    let entryFinished = false;
    for (const card of cards) {
      if (card.dataset.day) {
        try {
          const day = await api(`/api/days/${card.dataset.day}`);
          if (day.status !== card.dataset.status) card.replaceWith(renderDay(day));
          else {
            const eta = card.querySelector(".eta-label");
            if (eta) eta.textContent = etaText(day.eta_seconds);
          }
        } catch {
          // Cancelled before it was ever written: back to the "create" card.
          const date = card.dataset.day;
          const entries = await api(`/api/entries?start=${date}&end=${date}`).catch(() => []);
          const fresh = dayCardFor(date, [], entries);
          fresh ? card.replaceWith(fresh) : card.remove();
        }
        continue;
      }
      try {
        const entry = await api(`/api/entries/${card.dataset.entryId}`);
        const bar = card.querySelector(".progress-row");
        if (entry.status === card.dataset.status && bar) setProgress(bar, entry.progress);
        const eta = card.querySelector(".eta-label");
        if (entry.status === card.dataset.status && eta) eta.textContent = etaText(entry.eta_seconds);
        if (entry.status !== card.dataset.status) {
          card.replaceWith(renderSummary(entry, card._opts));
          entryFinished = true;
          if (entry.status === "done") toast("Highlights ready");
          if (entry.status === "failed") toast("An entry couldn't be processed");
        }
      } catch { /* try again next round */ }
    }
    // A finished entry may start (or update) the day's combined summary.
    if (entryFinished && !$("#view-log").hidden) await refreshDayCard();
    if (document.querySelector('.entry[data-status="transcribing"], .entry[data-status="summarising"]')) watchPending();
  }, 2000);
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
      addToLatest(renderSummary(entry, { onChange: loadLatest }));
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
    const [entries, days] = await Promise.all([api(`/api/entries?start=${day}&end=${day}`), fetchDays(day, day)]);
    const dayCard = dayCardFor(day, days, entries);
    $("#latest").replaceChildren(...(dayCard ? [dayCard] : []), ...entries.map((e) => renderSummary(e, { onChange: loadLatest })));
  } catch { /* shown elsewhere */ }
}

// New entries go just below the end-of-day card, which always stays pinned at the top.
function addToLatest(card) {
  const day = $("#latest .day-summary");
  if (day) day.after(card);
  else $("#latest").prepend(card);
}

// Shows, updates or removes the selected day's end-of-day card on the Log view.
async function refreshDayCard() {
  const day = $("#entry-date").value || today();
  try {
    const [entries, days] = await Promise.all([api(`/api/entries?start=${day}&end=${day}`), fetchDays(day, day)]);
    const card = dayCardFor(day, days, entries);
    const existing = $("#latest .day-summary");
    if (card) existing ? existing.replaceWith(card) : $("#latest").prepend(card);
    else if (existing) existing.remove();
  } catch { /* try again on the next change */ }
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
    const dates = entries.map((e) => e.date);
    const days = entries.length ? await fetchDays(dates[dates.length - 1], dates[0]) : [];
    // Each day with several entries starts with its end-of-day card.
    const cards = [];
    let lastDate = null;
    for (const e of entries) {
      if (e.date !== lastDate) {
        const dayCard = dayCardFor(e.date, days, entries);
        if (dayCard) cards.push(dayCard);
      }
      lastDate = e.date;
      cards.push(renderSummary(e, { onChange: loadHistory }));
    }
    list.replaceChildren(...(cards.length ? cards
      : [el("p", { class: "empty" }, "Nothing logged yet. Head to Log and talk about your day.")]));
  } catch (err) { toast(err.message); }
}
$("#hist-apply").addEventListener("click", loadHistory);

// ---------- reports ----------

function setRange(kind) {
  const now = new Date();
  let start = new Date(now);
  let end = new Date(now);
  if (kind === "yesterday") { start.setDate(now.getDate() - 1); end = new Date(start); }
  if (kind === "week") start.setDate(now.getDate() - 6);
  if (kind === "month") start.setDate(now.getDate() - 29);
  if (kind === "quarter") start = new Date(now.getFullYear(), Math.floor(now.getMonth() / 3) * 3, 1);
  if (kind === "half") start.setMonth(now.getMonth() - 6);
  if (kind === "year") start = new Date(now.getFullYear(), 0, 1);
  $("#rep-start").value = isoDate(start);
  $("#rep-end").value = isoDate(end);
  document.querySelectorAll("#range-presets .chip").forEach((c) => c.classList.toggle("active", c.dataset.range === kind));
}
document.querySelectorAll("#range-presets .chip").forEach((c) => c.addEventListener("click", () => setRange(c.dataset.range)));

// The notes box doubles as the prompt for a custom report; remember each separately.
const CUSTOM_PROMPT_KEY = "reviewer-custom-prompt";
let lastStyle = null;
function onStyleChange() {
  const style = $("#rep-style").value;
  const notes = $("#rep-notes");
  const store = (k, v) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } };
  const load = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
  if (lastStyle === "custom") store(CUSTOM_PROMPT_KEY, notes.value);
  if (style === "custom") {
    $("#rep-notes-label").textContent = "Your prompt";
    notes.placeholder = "e.g. Turn this into a short Slack update for my team: three bullets, plain language, no headings.";
    notes.rows = 5;
    notes.value = load(CUSTOM_PROMPT_KEY);
  } else {
    $("#rep-notes-label").replaceChildren("Anything to emphasise? ", el("span", { class: "muted" }, "(optional)"));
    notes.placeholder = "e.g. focus on leadership, I'm going for promotion";
    notes.rows = 2;
    if (lastStyle === "custom") notes.value = "";
  }
  // Daily notes are about one day: default to today unless a single day is already picked.
  if (style === "daily" && $("#rep-start").value !== $("#rep-end").value) setRange("today");
  lastStyle = style;
}
$("#rep-style").addEventListener("change", onStyleChange);
$("#rep-notes").addEventListener("input", (e) => {
  if ($("#rep-style").value === "custom") { try { localStorage.setItem(CUSTOM_PROMPT_KEY, e.target.value); } catch { /* ignore */ } }
});

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
// `parts` picks which sections to show: Settings splits them across tabs, setup shows them all.
function settingsForm(cfg, { setup = false, onSaved, parts = ["profile", "ai", "speech"] } = {}) {
  const has = (part) => parts.includes(part);
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
    speechModel: el("select", {}, Object.entries(state.speechModels || {}).map(([v, l]) =>
      el("option", { value: v, selected: v === cfg.speech.model }, `${v}: ${l}`))),
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
    has("profile") ? el("section", { class: "card" },
      el("h2", {}, "About you"),
      el("p", { class: "muted small" }, "Used to tailor your summaries and reports."),
      field("Your name", f.name),
      field("Role", f.role),
      field("Team / company", f.company),
      field("Goals and what matters in your role", f.goals, "Optional. Reports will emphasise these."),
      field("Background for the AI", f.context,
        "Optional. Projects, people, team names and acronyms you mention often. Included with every summary and report, so it can make sense of what you say and spell names right.")) : null,
    setup ? el("section", { class: "card" },
      el("h2", {}, "Choose a PIN"),
      el("p", { class: "muted small" }, "You'll type this to unlock the app. Use 6+ digits."),
      el("div", { class: "grid2" }, field("PIN", f.pin), field("Confirm PIN", f.pin2))) : null,
    has("ai") ? el("section", { class: "card" },
      el("h2", {}, "AI model"),
      el("p", { class: "muted small" }, "Turns what you say into highlights and writes your reports."),
      providers,
      el("h3", { class: "subhead" }, "Local model (Ollama)"), ollamaBox,
      el("h3", { class: "subhead" }, "Claude (optional)"), claudeBox) : null,
    has("speech") ? el("section", { class: "card" },
      el("h2", {}, "Speech"),
      el("p", { class: "muted small" }, "Recordings are transcribed on this machine and deleted once transcribed."),
      field("Language you speak in", f.language),
      field("Speech model", f.speechModel,
        "Bigger models are more accurate but slower. Recordings are processed in the background, so a slower model only means waiting a bit longer for highlights. A new model downloads the first time it's used.")) : null,
    el("div", { class: "actions" }, save),
    error);

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.textContent = "";
    const provider = form.querySelector("input[type=radio]:checked")?.value || "ollama";
    const body = {};
    if (has("profile")) {
      body.profile = {
        name: f.name.value.trim(), role: f.role.value.trim(), company: f.company.value.trim(),
        goals: f.goals.value.trim(), context: f.context.value.trim(),
      };
    }
    if (has("ai")) {
      body.ai = {
        provider,
        ollama_url: f.ollamaUrl.value.trim(),
        ollama_model: f.ollamaModel.value.trim(),
        anthropic_api_key: f.claudeKey.value.trim() || null,
        anthropic_model: f.claudeModel.value.trim(),
      };
    }
    if (has("speech")) body.speech = { language: f.language.value, model: f.speechModel.value || "small" };
    if (has("ai") && provider === "claude" && !body.ai.anthropic_api_key && !ai.anthropic_key_set) {
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

  if (!has("ai")) return form;
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

const SETTINGS_TABS = { profile: ["profile"], ai: ["ai", "speech"], security: [] };
let settingsTab = "profile";

async function loadSettings(tab = settingsTab) {
  settingsTab = SETTINGS_TABS[tab] ? tab : "profile";
  document.querySelectorAll("#settings-tabs button").forEach((b) => {
    const active = b.dataset.tab === settingsTab;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", String(active));
  });
  $("#settings-security").hidden = settingsTab !== "security";
  $("#settings-form").hidden = settingsTab === "security";
  if (settingsTab === "security") {
    $("#auto-lock").value = String(idle.minutes);
    return;
  }
  try {
    const cfg = await api("/api/settings");
    $("#settings-form").replaceChildren(settingsForm(cfg, {
      parts: SETTINGS_TABS[settingsTab],
      onSaved: async () => { toast("Settings saved"); await refreshSession(); },
    }));
  } catch (err) { toast(err.message); }
}
document.querySelectorAll("#settings-tabs button").forEach((b) => b.addEventListener("click", () => loadSettings(b.dataset.tab)));

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

$("#auto-lock").addEventListener("change", async (e) => {
  const minutes = Number(e.currentTarget.value);
  try {
    await api("/api/settings", { method: "PUT", body: { security: { auto_lock_minutes: minutes } } });
    idle.minutes = minutes;
    markActive(true);
    toast(minutes ? `Locks after ${minutes} minute${minutes === 1 ? "" : "s"} of inactivity` : "Auto-lock turned off");
  } catch (err) {
    e.currentTarget.value = String(idle.minutes);
    toast(err.message);
  }
});

$("#show-key").addEventListener("click", async (e) => {
  $("#key-error").textContent = "";
  await busy(e.currentTarget, "Checking…", async () => {
    try {
      const { key } = await api("/api/settings/key", { method: "POST", body: { pin: $("#key-pin").value } });
      $("#key-pin").value = "";
      $("#key-value").value = key;
      $("#key-box").hidden = false;
    } catch (err) { $("#key-error").textContent = err.message; }
  });
});

$("#copy-key").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#key-value").value);
    toast("Key copied");
  } catch {
    $("#key-value").select();
    toast("Select the key and copy it");
  }
});

$("#import-backup").addEventListener("click", async (e) => {
  $("#import-error").textContent = "";
  $("#import-result").textContent = "";
  const file = $("#import-file").files[0];
  if (!file) { $("#import-error").textContent = "Choose a backup file first."; return; }
  const form = new FormData();
  form.append("file", file);
  form.append("key", $("#import-key").value);
  await busy(e.currentTarget, "Loading…", async () => {
    try {
      const r = await api("/api/import/backup", { method: "POST", form });
      const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
      let msg = `Added ${count(r.entries, "entry", "entries")}, ${count(r.days, "end-of-day summary", "end-of-day summaries")} `
        + `and ${count(r.reports, "report", "reports")}.`;
      if (!r.entries && !r.days && !r.reports) msg = "Nothing new: everything in this backup is already here.";
      if (r.profile) msg += " Your \"About you\" details came across too.";
      if (r.skipped) msg += ` Left out ${count(r.skipped, "recording", "recordings")} that hadn't been transcribed.`;
      $("#import-result").textContent = msg;
      $("#import-file").value = "";
      $("#import-key").value = "";
      toast("Backup loaded");
    } catch (err) { $("#import-error").textContent = err.message; }
  });
});

// ---------- start ----------

$("#entry-date").value = today();
try { $("#entry-text").value = localStorage.getItem(DRAFT_KEY) || ""; } catch { /* ignore */ }
setRange("month");
boot().catch((err) => { document.body.prepend(el("p", { class: "error" }, err.message)); });
