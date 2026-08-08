/* IMVU_NET live — слушатель эфира: все слышат одно и то же место плейлиста */
"use strict";

const TOKEN = location.pathname.split("/").filter(Boolean).pop();
const RESYNC_MS = 5000;
const MAX_DRIFT = 2.5; // сек — допустимое рассогласование с сервером

const audio = new Audio();
audio.preload = "auto";

let state = null;      // последний ответ /now
let listening = false;
let curIndex = -1;
let syncTimer = null;

function toast(msg, err) {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function fmtTime(s) {
  s = Math.floor(s || 0);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

async function fetchNow() {
  const r = await fetch("/api/live/" + encodeURIComponent(TOKEN) + "/now");
  if (!r.ok) throw new Error("нет эфира");
  return r.json();
}

/* серверный now.offset — смещение ВНУТРИ текущего трека */
function currentPosition() {
  if (!state || !state.on || !state.now) return null;
  const elapsedSinceFetch = Date.now() / 1000 - state.clientAt;
  let offset = state.now.offset + elapsedSinceFetch;
  let index = state.now.index;
  const n = state.tracks.length;
  let guard = 0;
  while (offset >= state.tracks[index].duration && guard < n * 3 + 3) {
    offset -= state.tracks[index].duration;
    index = (index + 1) % n;
    guard++;
  }
  return { index, offset };
}

function renderTracks() {
  const host = document.getElementById("trackList");
  host.innerHTML = "";
  if (!state) return;
  state.tracks.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "track" + (i === curIndex && listening ? " playing" : "");
    li.style.cursor = "default";
    const name = document.createElement("span");
    name.className = "tr-name";
    name.textContent = t.name;
    const dur = document.createElement("span");
    dur.className = "muted";
    dur.textContent = fmtTime(t.duration);
    li.append(name, dur);
    host.appendChild(li);
  });
}

function setNP(title, sub) {
  document.getElementById("npTitle").textContent = title;
  document.getElementById("npSub").textContent = sub;
}

function updateBadge() {
  const b = document.getElementById("liveBadge");
  const on = state && state.on;
  b.textContent = on ? "on air" : "offline";
  b.classList.toggle("guest", !on);
  document.getElementById("rpVis").classList.toggle("on", listening && !audio.paused);
}

function tuneInRequest() {
  // заявка из интернета — слушаем прямой mp3-поток эфира
  curIndex = -2;
  audio.src = "/live/" + encodeURIComponent(TOKEN) + "/stream?t=" + Date.now();
  audio.play().then(() => {
    setNP(state.request.title, "по заявке · прямой эфир");
    renderTracks();
    updateBadge();
  }).catch(() => {
    toast("нажми play ещё раз — браузер требует клик", true);
    listening = false;
    updateUI();
  });
}

function tuneIn() {
  if (state && state.request) return tuneInRequest();
  const pos = currentPosition();
  if (!pos) return;
  const t = state.tracks[pos.index];
  curIndex = pos.index;
  audio.src = "/api/live/" + encodeURIComponent(TOKEN) + "/audio/" + t.id;
  audio.currentTime = 0;
  audio.play().then(() => {
    try { audio.currentTime = pos.offset; } catch (e) {}
    setNP(t.name, "прямой эфир · трек " + (pos.index + 1) + " / " + state.tracks.length);
    renderTracks();
    updateBadge();
  }).catch(() => {
    toast("нажми play ещё раз — браузер требует клик", true);
    listening = false;
    updateUI();
  });
}

async function resync(force) {
  try {
    const st = await fetchNow();
    st.clientAt = Date.now() / 1000;
    const wasOn = state && state.on;
    state = st;
    updateBadge();
    renderTracks();
    if (!st.on) {
      if (listening) { audio.pause(); }
      setNP("— эфир выключен —", "загляни позже");
      return;
    }
    if (!listening) {
      if (st.request) {
        setNP(st.request.title, "по заявке · нажми play чтобы слушать");
        return;
      }
      const pos = currentPosition();
      if (pos) setNP(st.tracks[pos.index].name, "сейчас в эфире · нажми play чтобы слушать");
      return;
    }
    if (st.request) {
      if (curIndex !== -2 || audio.paused || force) tuneInRequest();
      return;
    }
    const pos = currentPosition();
    if (!pos) return;
    const needTrackSwitch = pos.index !== curIndex;
    const drift = Math.abs(audio.currentTime - pos.offset);
    if (force || needTrackSwitch || drift > MAX_DRIFT || audio.paused || !wasOn) {
      tuneIn();
    }
  } catch (e) {
    setNP("— эфир не найден —", "проверь ссылку");
    updateBadge();
  }
}

function toggleListen() {
  listening = !listening;
  if (listening) {
    resync(true);
  } else {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    curIndex = -1;
    setNP("— пауза —", "нажми play чтобы вернуться в эфир");
    renderTracks();
  }
  updateUI();
}

function updateUI() {
  document.getElementById("btnListen").textContent = listening ? "Отключиться" : "Слушать эфир";
  updateBadge();
}

function setVolume(v) {
  audio.volume = v / 100;
  localStorage.setItem("live_volume", v);
}

audio.addEventListener("ended", () => { if (listening) resync(true); });
audio.addEventListener("error", () => { if (listening && audio.src) resync(true); });
audio.addEventListener("play", updateBadge);
audio.addEventListener("pause", updateBadge);

setInterval(() => {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString("ru-RU");
}, 1000);

(function init() {
  const v = parseInt(localStorage.getItem("live_volume") || "80", 10);
  document.getElementById("vol").value = v;
  audio.volume = v / 100;
  resync(false);
  syncTimer = setInterval(() => resync(false), RESYNC_MS);
})();
