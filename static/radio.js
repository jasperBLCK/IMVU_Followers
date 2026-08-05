/* IMVU_NET radio — станции-стримы + свои треки (IndexedDB), работает без бэкенда */
"use strict";

const DEFAULT_STATIONS = [
  { name: "Radio Record — YO?!", url: "https://rediorecord.hostinradio.ru/yo96.aacp", builtin: true },
];

const LS_STATIONS = "radio_custom_stations";
const LS_VOLUME = "radio_volume";

const audio = new Audio();
audio.preload = "none";

let tracks = [];          // [{id, name, blob}]
let queue = [];           // порядок воспроизведения (индексы tracks)
let queuePos = -1;
let shuffle = false;
let mode = null;          // "station" | "playlist"
let currentStation = null;
let currentUrl = null;    // objectURL текущего трека

/* ---------- toast ---------- */
function toast(msg, err) {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

/* ---------- IndexedDB ---------- */
function openDB() {
  return new Promise((res, rej) => {
    const req = indexedDB.open("imvu_net_radio", 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore("tracks", { keyPath: "id", autoIncrement: true });
    };
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

async function dbAll() {
  const db = await openDB();
  return new Promise((res, rej) => {
    const req = db.transaction("tracks").objectStore("tracks").getAll();
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

async function dbAdd(rec) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction("tracks", "readwrite");
    const req = tx.objectStore("tracks").add(rec);
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error);
  });
}

async function dbDel(id) {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction("tracks", "readwrite");
    tx.objectStore("tracks").delete(id);
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}

async function dbClear() {
  const db = await openDB();
  return new Promise((res, rej) => {
    const tx = db.transaction("tracks", "readwrite");
    tx.objectStore("tracks").clear();
    tx.oncomplete = () => res();
    tx.onerror = () => rej(tx.error);
  });
}

/* ---------- stations ---------- */
function customStations() {
  try { return JSON.parse(localStorage.getItem(LS_STATIONS) || "[]"); }
  catch { return []; }
}

function allStations() {
  return DEFAULT_STATIONS.concat(customStations());
}

function renderStations() {
  const host = document.getElementById("stationList");
  host.innerHTML = "";
  allStations().forEach((st) => {
    const el = document.createElement("div");
    el.className = "station" + (mode === "station" && currentStation === st.url ? " playing" : "");
    const dot = document.createElement("span");
    dot.className = "st-dot";
    const name = document.createElement("span");
    name.className = "st-name";
    name.textContent = st.name;
    const url = document.createElement("span");
    url.className = "st-url";
    url.textContent = st.url;
    el.append(dot, name, url);
    el.onclick = () => playStation(st);
    if (!st.builtin) {
      const x = document.createElement("span");
      x.className = "x";
      x.textContent = "✕";
      x.onclick = (e) => { e.stopPropagation(); removeStation(st.url); };
      el.appendChild(x);
    }
    host.appendChild(el);
  });
}

function addStation() {
  const name = document.getElementById("stName").value.trim();
  const url = document.getElementById("stUrl").value.trim();
  if (!name || !/^https?:\/\//.test(url)) return toast("нужны название и ссылка http(s)", true);
  const list = customStations();
  if (list.some((s) => s.url === url)) return toast("такая станция уже есть", true);
  list.push({ name, url });
  localStorage.setItem(LS_STATIONS, JSON.stringify(list));
  document.getElementById("stName").value = "";
  document.getElementById("stUrl").value = "";
  renderStations();
  toast("станция добавлена");
}

function removeStation(url) {
  localStorage.setItem(LS_STATIONS, JSON.stringify(customStations().filter((s) => s.url !== url)));
  renderStations();
}

function playStation(st) {
  mode = "station";
  currentStation = st.url;
  freeTrackUrl();
  audio.src = st.url;
  audio.play().then(() => {
    setNowPlaying(st.name, "прямой эфир · стрим");
    updateUI();
  }).catch(() => toast("не удалось подключиться к стриму", true));
  updateUI();
}

/* ---------- tracks ---------- */
async function refreshTracks() {
  tracks = await dbAll();
  renderTracks();
}

function renderTracks() {
  const host = document.getElementById("trackList");
  host.innerHTML = "";
  tracks.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "track" + (mode === "playlist" && queue[queuePos] === i ? " playing" : "");
    const name = document.createElement("span");
    name.className = "tr-name";
    name.textContent = t.name;
    const x = document.createElement("span");
    x.className = "x";
    x.textContent = "✕";
    x.onclick = (e) => { e.stopPropagation(); removeTrack(t.id); };
    li.append(name, x);
    li.onclick = () => playTrackAt(i);
    host.appendChild(li);
  });
  document.getElementById("trackCount").textContent = tracks.length + " треков";
}

async function handleFiles(files) {
  let added = 0;
  for (const f of files) {
    if (!f.type.startsWith("audio/") && !/\.(mp3|ogg|wav|m4a|aac|flac)$/i.test(f.name)) continue;
    await dbAdd({ name: f.name.replace(/\.[^.]+$/, ""), blob: f });
    added++;
  }
  document.getElementById("fileInput").value = "";
  await refreshTracks();
  toast(added ? "добавлено треков: " + added : "аудиофайлы не найдены", !added);
}

async function removeTrack(id) {
  await dbDel(id);
  await refreshTracks();
  buildQueue();
}

async function clearTracks() {
  if (!tracks.length) return;
  await dbClear();
  await refreshTracks();
  if (mode === "playlist") stopPlayback();
}

/* ---------- playlist playback ---------- */
function buildQueue() {
  queue = tracks.map((_, i) => i);
  if (shuffle) {
    for (let i = queue.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [queue[i], queue[j]] = [queue[j], queue[i]];
    }
  }
}

function playPlaylist() {
  if (!tracks.length) return toast("сначала загрузи треки", true);
  buildQueue();
  queuePos = 0;
  startTrack();
}

function playTrackAt(i) {
  if (!tracks.length) return;
  buildQueue();
  queuePos = queue.indexOf(i);
  startTrack();
}

function freeTrackUrl() {
  if (currentUrl) { URL.revokeObjectURL(currentUrl); currentUrl = null; }
}

function startTrack() {
  const t = tracks[queue[queuePos]];
  if (!t) return;
  mode = "playlist";
  currentStation = null;
  freeTrackUrl();
  currentUrl = URL.createObjectURL(t.blob);
  audio.src = currentUrl;
  audio.play().catch(() => toast("не удалось воспроизвести " + t.name, true));
  setNowPlaying(t.name, "моё радио · трек " + (queuePos + 1) + " / " + queue.length);
  updateUI();
}

function playNext() {
  if (mode !== "playlist" || !queue.length) return;
  queuePos = (queuePos + 1) % queue.length;
  if (queuePos === 0 && shuffle) buildQueue();
  startTrack();
}

function playPrev() {
  if (mode !== "playlist" || !queue.length) return;
  queuePos = (queuePos - 1 + queue.length) % queue.length;
  startTrack();
}

function stopPlayback() {
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  freeTrackUrl();
  mode = null;
  currentStation = null;
  setNowPlaying("— тишина в эфире —", "выбери станцию или запусти свой плейлист");
  updateUI();
}

/* ---------- controls ---------- */
function togglePlay() {
  if (!audio.src) {
    if (tracks.length) return playPlaylist();
    const st = allStations()[0];
    if (st) return playStation(st);
    return toast("нечего играть — добавь треки или станцию", true);
  }
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
}

function toggleShuffle() {
  shuffle = !shuffle;
  if (mode === "playlist") {
    const cur = queue[queuePos];
    buildQueue();
    queuePos = Math.max(0, queue.indexOf(cur));
  }
  document.getElementById("btnShuffle").textContent = "🔀 " + (shuffle ? "вкл" : "выкл");
  document.getElementById("btnShuffle").classList.toggle("cyan", shuffle);
  toast("режим радио-перемешивания: " + (shuffle ? "вкл" : "выкл"));
}

function setVolume(v) {
  audio.volume = v / 100;
  localStorage.setItem(LS_VOLUME, v);
}

function seekTo(v) {
  if (mode === "playlist" && isFinite(audio.duration)) {
    audio.currentTime = (v / 1000) * audio.duration;
  }
}

function fmtTime(s) {
  if (!isFinite(s)) return "∞";
  s = Math.floor(s);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/* ---------- UI ---------- */
function setNowPlaying(title, sub) {
  document.getElementById("npTitle").textContent = title;
  document.getElementById("npSub").textContent = sub;
}

function updateUI() {
  document.getElementById("btnPlay").textContent = audio.paused ? "▶ play" : "❚❚ pause";
  document.getElementById("rpVis").classList.toggle("on", !audio.paused);
  const isPl = mode === "playlist";
  document.getElementById("btnPrev").disabled = !isPl;
  document.getElementById("btnNext").disabled = !isPl;
  document.getElementById("seek").disabled = !isPl;
  renderStations();
  renderTracks();
}

audio.addEventListener("play", updateUI);
audio.addEventListener("pause", updateUI);
audio.addEventListener("ended", () => {
  if (mode === "playlist") playNext();
});
audio.addEventListener("error", () => {
  if (audio.src) toast("ошибка воспроизведения", true);
});
audio.addEventListener("timeupdate", () => {
  document.getElementById("tCur").textContent = fmtTime(audio.currentTime);
  document.getElementById("tDur").textContent = mode === "station" ? "live" : fmtTime(audio.duration);
  const seek = document.getElementById("seek");
  if (mode === "playlist" && isFinite(audio.duration) && audio.duration > 0) {
    seek.value = Math.round((audio.currentTime / audio.duration) * 1000);
  } else {
    seek.value = 0;
  }
});

/* drag & drop */
const dz = document.getElementById("dropZone");
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => handleFiles(e.dataTransfer.files));

/* clock */
setInterval(() => {
  const d = new Date();
  document.getElementById("clock").innerHTML =
    "// " + d.toLocaleTimeString("ru-RU") + '<span class="blink">_</span>';
}, 1000);

/* init */
(function init() {
  const v = parseInt(localStorage.getItem(LS_VOLUME) || "80", 10);
  document.getElementById("vol").value = v;
  audio.volume = v / 100;
  /* на GitHub Pages бэкенда нет — прячем ссылку на консоль, если открыто не с Flask */
  if (location.pathname.endsWith("/index.html") || location.hostname.endsWith("github.io")) {
    document.getElementById("backLink").classList.add("hidden");
  }
  renderStations();
  refreshTracks().then(updateUI);
})();
