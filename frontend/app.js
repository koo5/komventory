// komventory PWA — single-page app for note capture + browse.
//
// Three input paths feed the same log:
//   1. typed text       → POST /api/notes/text
//   2. recorded audio   → VAD-chunked Float32 → WAV → POST /api/notes/audio
//   3. (later) photo    → POST /api/notes/audio for the audio surface, etc.
//
// Log refresh is driven by SSE /api/log/stream: server emits `log-changed`
// whenever log.md's mtime changes (covers PWA writes, watcher writes, manual
// edits in the fork) and we refetch /api/log/recent on each ping.
//
// Q&A side-channel: after an audio note is transcribed, if "odpovídat" toggle
// is on we POST /api/ask. The answer renders as an ephemeral bubble (NOT in
// log.md — assistant answers don't get committed). TTS, if on, reads back the
// transcript and the answer.

const $ = (sel) => document.querySelector(sel);
const logEl = $("#log");
const statusEl = $("#status");
const textForm = $("#text-form");
const textInput = $("#text-input");
const recBtn = $("#rec-toggle");
const optTts = $("#opt-tts");
const optAsk = $("#opt-ask");
const meterEl = $("#meter");
const dotEl = $("#speaking-dot");

let micVad = null;          // MicVAD instance, created per record session (torn down on stop)
let micStream = null;       // owned MediaStream, teed to VAD + analyser
let audioCtx = null;        // AudioContext for the analyser
let analyser = null;        // AnalyserNode tapped off micStream
let meterRaf = 0;           // requestAnimationFrame handle for the scope
let recOn = false;

// Ephemeral LLM answers keyed by the triggering entry's "timestamp|source".
// Re-attached under their anchor entry on every fetchLog so SSE-driven
// refetches don't unpair them. If the anchor scrolls out of the recent-50
// window, the answer disappears with it — they're not in log.md anyway.
const pendingAnswers = new Map();

// ----------------------------------------------------------------- status --
function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

// -------------------------------------------------------------------- log --
function entryKey(e) {
  return `${e.timestamp}|${e.source}`;
}

function classifyEntry(e) {
  if (e.source?.startsWith("text@")) return "text";
  if (e.source?.startsWith("whisper")) return "whisper";
  if (e.source?.startsWith("image@")) return "image";
  if (e.source?.startsWith("note@")) return "note";
  return "other";
}

function renderEntry(e) {
  const div = document.createElement("div");
  div.className = `entry ${classifyEntry(e)}`;
  div.dataset.testid = "entry";
  div.dataset.timestamp = e.timestamp;
  div.dataset.key = entryKey(e);

  const meta = document.createElement("div");
  meta.className = "meta";
  const ts = document.createElement("span");
  ts.textContent = new Date(e.timestamp).toLocaleString("cs-CZ", {
    hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit",
  });
  meta.appendChild(ts);
  if (e.source) {
    const src = document.createElement("span");
    src.textContent = e.source;
    meta.appendChild(src);
  }
  if (e.where) {
    const w = document.createElement("span");
    w.textContent = `📍 ${e.where}`;
    meta.appendChild(w);
  }
  div.appendChild(meta);

  const body = document.createElement("div");
  body.className = "body";
  body.textContent = e.body || "";
  div.appendChild(body);

  if (e.attachments?.length) {
    const a = document.createElement("div");
    a.className = "attach";
    a.textContent = `📎 ${e.attachments.length} přílo${e.attachments.length === 1 ? "ha" : "hy"}`;
    div.appendChild(a);
  }
  return div;
}

function renderAnswerBubble(question, answer, isStub) {
  // Ephemeral — not in log.md. Sits below the entry that triggered it.
  const div = document.createElement("div");
  div.className = "entry answer" + (isStub ? " stub" : "");
  div.dataset.testid = "answer";
  const meta = document.createElement("div");
  meta.className = "meta";
  const tag = document.createElement("span");
  tag.textContent = isStub ? "🤖 (stub odpověď)" : "🤖 odpověď";
  meta.appendChild(tag);
  div.appendChild(meta);
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = answer;
  div.appendChild(body);
  return div;
}

function findEntryNode(key) {
  for (const node of logEl.querySelectorAll(".entry[data-key]")) {
    if (node.dataset.key === key) return node;
  }
  return null;
}

function attachAnswerUnder(key, data) {
  const anchor = findEntryNode(key);
  if (!anchor) return;
  const node = renderAnswerBubble(data.question, data.answer, data.isStub);
  node.dataset.answerFor = key;
  anchor.after(node);
}

async function fetchLog() {
  const r = await fetch("/api/log/recent?n=50");
  if (!r.ok) { setStatus("log fetch failed", "error"); return; }
  const entries = await r.json();
  logEl.innerHTML = "";
  for (const e of entries) logEl.appendChild(renderEntry(e));
  // Replay any pending answers in place, right under their anchor entry.
  for (const [key, data] of pendingAnswers) attachAnswerUnder(key, data);
  logEl.scrollTop = logEl.scrollHeight;
}

function subscribeSSE() {
  let es;
  function connect() {
    es = new EventSource("/api/log/stream");
    es.addEventListener("log-changed", () => fetchLog());
    es.onerror = () => {
      // EventSource auto-reconnects, but if the connection is closed by the
      // server we fall through to here. Browser will retry on its own; just
      // surface a brief status so the user knows feed updates may be paused.
      setStatus("spojení přerušeno, čekám…", "error");
    };
    es.onopen = () => setStatus(recOn ? "poslouchám…" : "připraveno");
  }
  connect();
}

// ----------------------------------------------------------------- text --
textForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = textInput.value.trim();
  if (!body) return;
  textInput.value = "";
  setStatus("ukládám…", "busy");
  const r = await fetch("/api/notes/text", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify({body}),
  });
  if (!r.ok) { setStatus("text save failed", "error"); return; }
  const entry = await r.json();
  setStatus(recOn ? "poslouchám…" : "připraveno");
  if (optAsk.checked) askMaybe(body, entryKey(entry));
});

// ---------------------------------------------------- audio capture (VAD) --
function floatToWav(float32, sampleRate) {
  const numSamples = float32.length;
  const buf = new ArrayBuffer(44 + numSamples * 2);
  const v = new DataView(buf);
  const ascii = (s, off) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  ascii("RIFF", 0);
  v.setUint32(4, 36 + numSamples * 2, true);
  ascii("WAVE", 8);
  ascii("fmt ", 12);
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  ascii("data", 36);
  v.setUint32(40, numSamples * 2, true);
  let off = 44;
  for (let i = 0; i < numSamples; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([buf], {type: "audio/wav"});
}

// Peak-normalize a Float32 buffer so its loudest sample sits at ~0.9. Many
// mic inputs (built-in laptop mics, headsets at default gain) deliver speech
// at -30 dB peak or quieter; Whisper's internal VAD then discards the whole
// clip as silence. Pre-amping before WAV encoding sidesteps this without
// touching server-side code. Gain is capped to avoid blowing up pure noise.
function normalizeFloat32(buf, targetPeak = 0.9, maxGain = 50) {
  let peak = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = buf[i] < 0 ? -buf[i] : buf[i];
    if (v > peak) peak = v;
  }
  const peakDb = peak > 0 ? 20 * Math.log10(peak) : -Infinity;
  if (peak < 1e-6) {
    console.log(`[audio] silent buffer (peak=${peakDb.toFixed(1)} dB), sending as-is`);
    return buf;
  }
  const gain = Math.min(targetPeak / peak, maxGain);
  if (gain <= 1.01) {
    console.log(`[audio] already loud (peak=${peakDb.toFixed(1)} dB), no normalization`);
    return buf;
  }
  console.log(`[audio] normalizing peak=${peakDb.toFixed(1)} dB → ×${gain.toFixed(2)} gain`);
  const out = new Float32Array(buf.length);
  for (let i = 0; i < buf.length; i++) out[i] = buf[i] * gain;
  return out;
}

async function uploadAudio(float32) {
  const normalized = normalizeFloat32(float32);
  const wav = floatToWav(normalized, 16000);
  const form = new FormData();
  form.append("file", wav, "rec.wav");
  setStatus("přepisuji…", "busy");
  let r;
  try {
    r = await fetch("/api/notes/audio", {method: "POST", body: form});
  } catch (e) {
    setStatus("upload selhal", "error");
    return;
  }
  if (r.status === 204) {
    setStatus(recOn ? "poslouchám…" : "připraveno");
    return;
  }
  if (!r.ok) {
    setStatus(`audio: HTTP ${r.status}`, "error");
    return;
  }
  const entry = await r.json();
  setStatus(recOn ? "poslouchám…" : "připraveno");
  if (entry.body) {
    if (optTts.checked) speakBack(entry.body);
    if (optAsk.checked) askMaybe(entry.body, entryKey(entry));
  }
}

// Scope: live oscilloscope drawn from analyser's time-domain data. Cheap and
// honestly shows "the mic is hearing you" — silent room → flat line, voice →
// wiggle. Not a level meter; pair with the speaking-dot (VAD-driven) for the
// "you crossed the speech threshold" signal.
function startMeter() {
  if (!analyser) return;
  const ctx = meterEl.getContext("2d");
  const W = meterEl.width, H = meterEl.height;
  const data = new Uint8Array(analyser.fftSize);
  function frame() {
    if (!recOn || !analyser) return;
    analyser.getByteTimeDomainData(data);
    ctx.fillStyle = "rgba(29,31,33,0.55)";  // motion-blur trail
    ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "#ffd86b";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    const step = data.length / W;
    for (let x = 0; x < W; x++) {
      const v = data[Math.floor(x * step)] / 128.0;  // 0..2, 1=silence baseline
      const y = (v * H) / 2;
      if (x === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.stroke();
    meterRaf = requestAnimationFrame(frame);
  }
  cancelAnimationFrame(meterRaf);
  meterRaf = requestAnimationFrame(frame);
}

function stopMeter() {
  cancelAnimationFrame(meterRaf);
  meterRaf = 0;
  const ctx = meterEl.getContext("2d");
  ctx.clearRect(0, 0, meterEl.width, meterEl.height);
}

async function startRecording() {
  if (!window.vad?.MicVAD) {
    setStatus("VAD knihovna se nenačetla", "error");
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) {
    setStatus(`mikrofon: ${e.message || e}`, "error");
    return;
  }
  // Tap an analyser off the stream BEFORE passing it to VAD, so the scope
  // shows raw input regardless of what the VAD library does internally.
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 1024;
  audioCtx.createMediaStreamSource(micStream).connect(analyser);
  // Deliberately NOT connecting analyser → destination: that would echo the
  // mic into the speakers and feed back through the next recording.

  try {
    micVad = await window.vad.MicVAD.new({
      stream: micStream,
      // Silero VAD ring-buffers `preSpeechPadFrames` audio frames BEFORE the
      // speech-onset trigger and prepends them to onSpeechEnd's audio. Default
      // 1 frame is tight — bumping to 8 gives ~250ms leadup so hard consonants
      // (k-, p-, t-) at word starts don't get clipped. Same idea for the tail
      // via redemptionFrames (silence tolerated mid-utterance before cutoff).
      preSpeechPadFrames: 8,
      redemptionFrames: 16,
      minSpeechFrames: 3,
      onSpeechStart: () => { dotEl.classList.add("on"); setStatus("mluvíš…", "busy"); },
      onSpeechEnd: (audio) => { dotEl.classList.remove("on"); uploadAudio(audio); },
      onVADMisfire: () => { dotEl.classList.remove("on"); setStatus(recOn ? "poslouchám…" : "připraveno"); },
    });
    micVad.start();
  } catch (e) {
    console.error(e);
    setStatus(`VAD: ${e.message || e}`, "error");
    teardownAudio();
    return;
  }
  recOn = true;
  recBtn.classList.add("active");
  recBtn.textContent = "⏸ stop";
  setStatus("poslouchám…");
  startMeter();
}

function teardownAudio() {
  if (micVad) {
    try { micVad.pause(); } catch {}
    try { micVad.destroy?.(); } catch {}
    micVad = null;
  }
  if (micStream) {
    // Stopping tracks releases the mic — browser tab indicator goes away.
    micStream.getTracks().forEach((t) => { try { t.stop(); } catch {} });
    micStream = null;
  }
  if (audioCtx) {
    try { audioCtx.close(); } catch {}
    audioCtx = null;
  }
  analyser = null;
}

function stopRecording() {
  recOn = false;
  recBtn.classList.remove("active");
  recBtn.textContent = "🎙 záznam";
  dotEl.classList.remove("on");
  stopMeter();
  teardownAudio();
  setStatus("připraveno");
}

recBtn.addEventListener("click", async () => {
  if (recOn) stopRecording();
  else await startRecording();
});

// ------------------------------------------------------------------- ask --
async function askMaybe(text, anchorKey) {
  let r;
  try {
    r = await fetch("/api/ask", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({text}),
    });
  } catch (e) {
    return;
  }
  if (!r.ok) return;
  const result = await r.json();
  if (!result.is_question || !result.answer) return;
  const isStub = result.answer.startsWith("(stub:");
  const data = {question: text, answer: result.answer, isStub};
  pendingAnswers.set(anchorKey, data);
  attachAnswerUnder(anchorKey, data);
  logEl.scrollTop = logEl.scrollHeight;
  if (optTts.checked) speakBack(result.answer);
}

// ------------------------------------------------------------------- tts --
let ttsAudio = null;
async function speakBack(text) {
  if (!text) return;
  console.log("[tts] sending", JSON.stringify(text));
  let r;
  try {
    r = await fetch("/api/tts", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({text}),
    });
  } catch (e) {
    return;
  }
  if (!r.ok) return;
  const blob = await r.blob();
  // Stop any prior playback so new transcripts don't overlap with old ones.
  if (ttsAudio) { ttsAudio.pause(); ttsAudio.src = ""; }
  ttsAudio = new Audio(URL.createObjectURL(blob));
  ttsAudio.play().catch(() => { /* autoplay can fail; user must interact first */ });
}

// ------------------------------------------------------------------ init --
if ("serviceWorker" in navigator) {
  // Best-effort registration; failures are silent (no offline support is fine).
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

fetchLog();
subscribeSSE();
