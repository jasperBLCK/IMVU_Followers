/* IMVU_NET радио — «мой эфир»: серверная радиостанция с уникальной ссылкой */
"use strict";

let liveState = null;

/* ---------- toast ---------- */
function toast(msg, err) {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function fmtTime(s) {
  s = Math.floor(s);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/* ---------- state ---------- */
async function liveRefresh() {
  const r = await fetch("/api/live");
  if (!r.ok) throw new Error("unauthorized");
  liveState = await r.json();
  renderLive();
}

function renderLive() {
  if (!liveState) return;
  document.getElementById("liveLink").value =
    location.origin + "/live/" + liveState.token;
  document.getElementById("liveStreamLink").value =
    location.origin + "/live/" + liveState.token + "/stream";
  const on = liveState.on;
  const btn = document.getElementById("btnLiveToggle");
  btn.textContent = on ? "Выключить эфир" : "Включить эфир";
  btn.classList.toggle("danger", on);
  document.getElementById("onAirBadge").textContent = on ? "on air" : "off air";
  document.getElementById("onAirBadge").classList.toggle("ok", on);
  document.getElementById("liveStatus").textContent =
    (on ? "в эфире" : "эфир выключен") + " · " + liveState.tracks.length + " треков";
  const host = document.getElementById("liveTrackList");
  host.innerHTML = "";
  liveState.tracks.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "track" + (on && liveState.now && liveState.now.index === i ? " playing" : "");
    li.style.cursor = "default";
    const name = document.createElement("span");
    name.className = "tr-name";
    name.textContent = t.name;
    const dur = document.createElement("span");
    dur.className = "muted";
    dur.textContent = fmtTime(t.duration);
    const x = document.createElement("span");
    x.className = "x";
    x.textContent = "✕";
    x.title = "убрать из эфира";
    x.onclick = async () => {
      const r = await fetch("/api/live/track/" + t.id, { method: "DELETE" });
      const j = await r.json();
      if (!j.ok) return toast(j.error || "не удалось удалить", true);
      liveRefresh();
    };
    li.append(name, dur, x);
    host.appendChild(li);
  });
}

/* ---------- upload с прогрессом ---------- */
function liveHandleFiles(files) {
  const fd = new FormData();
  let n = 0;
  for (const f of files) {
    if (!/\.mp3$/i.test(f.name)) { toast(f.name + " — нужен mp3", true); continue; }
    fd.append("files", f);
    n++;
  }
  document.getElementById("liveFileInput").value = "";
  if (!n) return;

  const wrap = document.getElementById("upWrap");
  const bar = document.getElementById("upBar");
  const txt = document.getElementById("upText");
  wrap.classList.remove("hidden");
  bar.style.width = "0%";
  txt.textContent = "0%";

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/live/upload");
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = Math.round((e.loaded / e.total) * 100);
    bar.style.width = p + "%";
    txt.textContent = p + "%";
  };
  xhr.onload = () => {
    wrap.classList.add("hidden");
    let j = null;
    try { j = JSON.parse(xhr.responseText); } catch (e) { /* не json */ }
    if (!j || !j.ok) {
      return toast((j && j.error) || "ошибка загрузки (" + xhr.status + ")", true);
    }
    (j.rejected || []).forEach((m) => toast(m, true));
    toast("в эфир добавлено: " + j.added);
    liveRefresh();
  };
  xhr.onerror = () => {
    wrap.classList.add("hidden");
    toast("загрузка оборвалась — проверь сеть", true);
  };
  xhr.send(fd);
}

/* ---------- controls ---------- */
async function liveToggle() {
  const r = await fetch("/api/live/toggle", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ on: !(liveState && liveState.on) }),
  });
  const j = await r.json();
  if (!j.ok) return toast(j.error || "ошибка", true);
  toast(j.on ? "эфир включён — плейлист пошёл по кругу" : "эфир выключен");
  liveRefresh();
}

function copyText(id, msg) {
  const inp = document.getElementById(id);
  inp.select();
  navigator.clipboard.writeText(inp.value).then(
    () => toast(msg),
    () => document.execCommand("copy"));
}

function copyLiveLink() { copyText("liveLink", "ссылка страницы эфира скопирована"); }
function copyStreamLink() { copyText("liveStreamLink", "mp3-ссылка для игры скопирована"); }

async function regenLiveLink() {
  if (!confirm("Создать новую ссылку? Старая перестанет работать.")) return;
  const r = await fetch("/api/live/regen", { method: "POST" });
  const j = await r.json();
  if (j.ok) { toast("новая ссылка эфира создана"); liveRefresh(); }
}

/* ---------- init ---------- */
async function init() {
  try {
    const me = await fetch("/api/me").then((r) => r.json());
    if (me.role !== "user") throw new Error("guest");
    await liveRefresh();
    document.getElementById("liveCard").classList.remove("hidden");
    const dz = document.getElementById("liveDropZone");
    ["dragenter", "dragover"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => liveHandleFiles(e.dataTransfer.files));
    setInterval(liveRefresh, 15000); // подсветка текущего трека
  } catch (e) {
    document.getElementById("loginHint").classList.remove("hidden");
  }
}

setInterval(() => {
  document.getElementById("clock").textContent =
    new Date().toLocaleTimeString("ru-RU");
}, 1000);

init();
