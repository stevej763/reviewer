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

async function boot() {
  const s = await api("/api/session");
  state.providers = s.providers;
  state.defaultProvider = s.default_provider;
  state.styles = s.report_styles;
  if (!s.authenticated) return showLogin();
  $("#login").hidden = true;
  $("#app").hidden = false;
  fillProviders($("#entry-provider"));
  fillProviders($("#rep-provider"));
  $("#rep-style").replaceChildren(...Object.entries(state.styles).map(([k, v]) => el("option", { value: k }, v)));
  if (!state.providers.length) {
    $("#log-error").textContent = "No AI model is configured yet. Entries will be saved without a summary.";
  }
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

const TITLES = { log: "Today", history: "History", reports: "Reports" };

function showView(name) {
  if (!TITLES[name]) name = "log";
  for (const v of Object.keys(TITLES)) $(`#view-${v}`).hidden = v !== name;
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("#view-title").textContent = TITLES[name];
  history.replaceState(null, "", `#${name}`);
  if (name === "history") loadHistory();
  if (name === "reports") loadReports();
}
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
  const blob = new Blob(rec.chunks, { type });
  rec.recorder = null;
  setRecordUI("busy", "Transcribing…");
  const form = new FormData();
  form.append("audio", blob, type.includes("mp4") ? "recording.mp4" : "recording.webm");
  try {
    const { text } = await api("/api/transcribe", { method: "POST", form });
    const box = $("#entry-text");
    box.value = [box.value.trim(), text].filter(Boolean).join("\n\n");
    try { localStorage.setItem(DRAFT_KEY, box.value); } catch { /* ignore */ }
    setRecordUI("idle", text ? "Got it. Record more, or save" : "Didn't catch anything. Try again?");
  } catch (err) {
    $("#log-error").textContent = err.message;
    setRecordUI("idle", "Tap to talk about your day");
  }
}

$("#record").addEventListener("click", () => {
  if (rec.recorder && rec.recorder.state === "recording") stopRecording();
  else startRecording();
});

// ---------- entries ----------

function renderSummary(entry, { onChange } = {}) {
  const s = entry.summary;
  const card = el("article", { class: "card entry" });
  const meta = entry.provider ? `${entry.provider} · ${entry.model}` : "not summarised";
  card.append(el("div", { class: "entry-head" },
    el("span", { class: "entry-date" }, prettyDate(entry.date)),
    el("span", { class: "entry-meta" }, meta)));

  if (entry.summary_error) card.append(el("div", { class: "warn" }, `Saved, but summarising failed: ${entry.summary_error}`));

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

  card.append(el("details", {}, el("summary", {}, "What you said"), el("pre", {}, entry.text)));

  const actions = el("div", { class: "entry-actions" });
  if (state.providers.length) {
    for (const p of state.providers) {
      actions.append(el("button", {
        class: "btn",
        onclick: async (e) => {
          await busy(e.currentTarget, "Summarising…", async () => {
            try {
              const updated = await api(`/api/entries/${entry.id}/summarize`, { method: "POST", body: { provider: p.name } });
              card.replaceWith(renderSummary(updated, { onChange }));
              toast("Re-summarised");
            } catch (err) { toast(err.message); }
          });
        },
      }, state.providers.length > 1 ? `Redo with ${p.name === "claude" ? "Claude" : "Ollama"}` : "Redo summary"));
    }
  }
  actions.append(el("button", {
    class: "btn",
    onclick: () => editEntry(entry, card, onChange),
  }, "Edit"));
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
              toast("Saved");
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
  await busy(e.currentTarget, "Summarising…", async () => {
    try {
      const entry = await api("/api/entries", {
        method: "POST",
        body: { date: $("#entry-date").value || today(), text, provider: $("#entry-provider").value || null },
      });
      $("#entry-text").value = "";
      try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
      $("#latest").prepend(renderSummary(entry));
      setRecordUI("idle", "Tap to talk about your day");
      toast(entry.summary_error ? "Saved without summary" : "Saved");
    } catch (err) {
      $("#log-error").textContent = err.message;
    }
  });
});

// Keep unsent text if the page reloads or the phone kills the tab.
$("#entry-text").addEventListener("input", (e) => {
  try { localStorage.setItem(DRAFT_KEY, e.target.value); } catch { /* storage unavailable */ }
});

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

// ---------- start ----------

$("#entry-date").value = today();
try { $("#entry-text").value = localStorage.getItem(DRAFT_KEY) || ""; } catch { /* ignore */ }
setRange("month");
boot().catch((err) => { document.body.prepend(el("p", { class: "error" }, err.message)); });
