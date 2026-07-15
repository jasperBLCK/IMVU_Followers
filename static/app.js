const $ = (id) => document.getElementById(id);
const PAGES = ["pageMain", "pageFollow", "pageRoom"];

function showPage(id) {
  PAGES.forEach((p) => $(p).classList.toggle("hidden", p !== id));
  document.querySelectorAll("#tabs .tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.page === id);
  });
}
const JOB_FIELDS = ["target_id", "max_follows", "follow_delay", "unfollow_delay"];
let ROLE = null;

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET") };
  if (body) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.href = "/login";
    return {};
  }
  return r.json();
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function fmt(n) {
  return (Number(n) || 0).toLocaleString("ru-RU");
}

function toast(msg, sub, isErr) {
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.innerHTML = esc(msg) + (sub ? "<small>" + esc(sub) + "</small>" : "");
  $("toastHost").appendChild(t);
  setTimeout(() => {
    t.style.opacity = "0";
    setTimeout(() => t.remove(), 400);
  }, 4200);
}

function profileTags(p) {
  const tags = [];
  tags.push(`<span class="tag ${p.online ? "on" : ""}">${p.online ? "online" : "offline"}</span>`);
  if (p.is_vip) tags.push('<span class="tag vip">VIP</span>');
  if (p.is_ap) tags.push('<span class="tag ap">AP</span>');
  if (p.is_creator) tags.push('<span class="tag">creator</span>');
  if (p.country) tags.push(`<span class="tag">${esc(p.country)}</span>`);
  if (p.gender) tags.push(`<span class="tag">${esc(p.gender === "m" ? "male" : p.gender === "f" ? "female" : p.gender)}</span>`);
  if (p.age) tags.push(`<span class="tag">${esc(p.age)}y</span>`);
  return tags.join("");
}

function fmtDate(s) {
  if (!s) return "—";
  return String(s).slice(0, 10);
}

// ---------------- init ----------------
async function init() {
  const me = await api("/api/me");
  if (!me.ok) {
    location.href = "/login";
    return;
  }
  ROLE = me.role;
  $("roleBadge").textContent = ROLE === "user" ? "АККАУНТ" : "ГОСТЬ";
  $("roleBadge").className = "badge" + (ROLE === "guest" ? " guest" : "");

  if (ROLE === "user") {
    $("profileCard").classList.remove("hidden");
    $("exceptionsCard").classList.remove("hidden");
    $("userPanel").classList.remove("hidden");
    document.querySelectorAll("#tabs .tab").forEach((t) => t.classList.remove("hidden"));
    if (me.profile) showMe(me.profile);
    $("whoami").textContent = me.username ? "@" + me.username : "";
    loadSettings();
    loadExceptions();
    loadRecentRooms();
    scheduleStatus();
    // login notification
    const stash = sessionStorage.getItem("just_logged_in");
    if (stash) {
      sessionStorage.removeItem("just_logged_in");
      const p = JSON.parse(stash);
      toast("ДОСТУП РАЗРЕШЁН", "вошёл как " + (p.name || me.username) + " · " + fmt(p.followers) + " подписчиков");
    }
  } else {
    $("userPanel").classList.add("hidden");
    if (sessionStorage.getItem("guest_login")) {
      sessionStorage.removeItem("guest_login");
      toast("ГОСТЕВОЙ РЕЖИМ", "только чтение публичной статистики");
    }
  }
}

function showMe(p) {
  $("meName").textContent = p.name || p.display_name || "—";
  const h = $("meHandle");
  h.textContent = "@" + (p.avatar_name || p.user_id);
  if (p.profile_url) h.href = p.profile_url;
  $("meFollowers").textContent = fmt(p.followers);
  $("meFollowing").textContent = fmt(p.following);
  $("meCreated").textContent = fmtDate(p.created);
  $("meTags").innerHTML = profileTags(p);
  if (p.image) $("meAvatar").src = p.image;
}

// ---------------- lookup (guest + user) ----------------
async function lookup() {
  const target = $("lookupInput").value.trim();
  if (!target) return;
  const box = $("lookupResult");
  box.innerHTML = '<div class="muted">сканирую...</div>';
  const j = await api("/api/lookup", { target });
  if (!j.ok) {
    box.innerHTML = '<div class="muted">⚠ ' + esc(j.error) + "</div>";
    return;
  }
  const p = j.profile;
  let followBtn = "";
  if (ROLE === "user" && j.is_following !== null) {
    followBtn = j.is_following
      ? `<button class="btn danger small" onclick="quickUnfollow('${j.user_id}')">отписаться</button>`
      : `<button class="btn small" onclick="quickFollow('${j.user_id}')">подписаться</button>`;
  }
  LAST_PREVIEW = j.preview || [];
  const previewBlock = LAST_PREVIEW.length
    ? '<h2 style="margin-top:16px">подписчики цели</h2>' +
      '<div class="inline" style="margin-bottom:10px">' +
      '<input id="prevFilter" placeholder="фильтр по имени" oninput="renderPreview()">' +
      '<button class="btn ghost small" id="prevSort" onclick="togglePrevSort()">сорт: топ ▾</button></div>' +
      '<div class="people" id="previewGrid"></div>'
    : "";
  box.innerHTML =
    '<div class="profile-row" style="margin-top:10px">' +
    '<img class="avatar" src="' + esc(p.image) + '" onerror="this.style.visibility=\'hidden\'">' +
    '<div><div class="pname">' + esc(p.name || p.display_name) + "</div>" +
    '<a class="phandle" href="' + esc(p.profile_url) + '" target="_blank">@' + esc(p.avatar_name || p.user_id) + "</a>" +
    '<div class="pmeta">' + profileTags(p) + "</div></div></div>" +
    '<div class="stats-row"><div class="stat"><b>' + fmt(p.followers) + "</b><span>подписчиков</span></div>" +
    '<div class="stat"><b>' + fmt(p.following) + "</b><span>подписок</span></div>" +
    '<div class="stat"><b>' + fmtDate(p.created) + "</b><span>создан</span></div></div>" +
    (followBtn ? '<div style="margin-top:12px">' + followBtn + "</div>" : "") +
    previewBlock;
  renderPreview();
}

let LAST_PREVIEW = [];
let PREV_SORT = true; // sort by followers desc
function togglePrevSort() {
  PREV_SORT = !PREV_SORT;
  const b = $("prevSort");
  if (b) b.textContent = PREV_SORT ? "сорт: топ ▾" : "сорт: как есть";
  renderPreview();
}
function renderPreview() {
  const grid = $("previewGrid");
  if (!grid) return;
  const q = ($("prevFilter") && $("prevFilter").value.trim().toLowerCase()) || "";
  let list = LAST_PREVIEW.filter((x) => !q || (x.name || "").toLowerCase().includes(q));
  if (PREV_SORT) list = list.slice().sort((a, b) => (b.followers || 0) - (a.followers || 0));
  grid.innerHTML =
    list
      .map(
        (x) =>
          '<div class="person"><img src="' + esc(x.image) +
          '" onerror="this.style.visibility=\'hidden\'"><div><div class="pn">' +
          esc(x.name) + '</div><div class="pf">' + fmt(x.followers) + " подп.</div></div></div>"
      )
      .join("") || '<div class="muted">ничего не найдено</div>';
}

async function quickFollow(uid) {
  const j = await api("/api/follow-one", { target: uid });
  toast(j.ok ? "подписался на user-" + uid : "не удалось подписаться", j.error, !j.ok);
  if (j.ok) lookup();
}
async function quickUnfollow(uid) {
  const j = await api("/api/unfollow-one", { target: uid });
  toast(j.ok ? "отписался от user-" + uid : "не удалось отписаться", j.error, !j.ok);
  if (j.ok) lookup();
}

// ---------------- settings ----------------
async function loadSettings() {
  const s = await api("/api/settings");
  JOB_FIELDS.forEach((f) => {
    if ($(f) && s[f] !== undefined) $(f).value = s[f];
  });
}
function collectJob() {
  const o = {};
  JOB_FIELDS.forEach((f) => (o[f] = $(f).value));
  o.only_non_followers = $("only_non_followers").checked;
  return o;
}

// ---------------- exceptions ----------------
async function loadExceptions() {
  const j = await api("/api/exceptions");
  renderExceptions(j.items || []);
}
function renderExceptions(items) {
  const el = $("excList");
  if (!items.length) {
    el.innerHTML = '<div class="muted">список пуст — этих людей анфоллоу не тронет</div>';
    return;
  }
  el.innerHTML = items
    .map(
      (e) =>
        '<div class="exc"><img src="' + esc(e.image) +
        '" onerror="this.style.visibility=\'hidden\'"><span>' + esc(e.name) +
        '</span><span class="muted">#' + esc(e.id) + '</span>' +
        '<span class="x" onclick="delException(\'' + esc(e.id) + '\')">✕</span></div>'
    )
    .join("");
}
async function addException() {
  const target = $("excInput").value.trim();
  if (!target) return;
  const j = await api("/api/exceptions", { target });
  if (j.ok) {
    $("excInput").value = "";
    renderExceptions(j.items);
    toast("добавлено в исключения");
  } else {
    toast("ошибка", j.error, true);
  }
}
async function delException(uid) {
  const j = await api("/api/exceptions/" + encodeURIComponent(uid), null, "DELETE");
  if (j.ok) renderExceptions(j.items);
}

// ---------------- top-5 subscriptions ----------------
async function loadTop() {
  const btn = $("btnTop");
  btn.disabled = true;
  btn.textContent = "сканирую подписки... (это займёт время)";
  const j = await api("/api/top-subscriptions?n=5&scan=2000");
  btn.disabled = false;
  btn.textContent = "★ обновить топ";
  if (!j.ok) return toast("ошибка", j.error, true);
  const list = $("topList");
  list.classList.remove("hidden");
  $("topHint").textContent = "Проверено подписок: " + fmt(j.scanned);
  if (!j.items.length) {
    list.innerHTML = '<li class="muted">пусто</li>';
    return;
  }
  list.innerHTML = j.items
    .map(
      (x) =>
        '<li class="top-item"><img src="' + esc(x.image) +
        '" onerror="this.style.visibility=\'hidden\'">' +
        '<div class="ti-main"><div class="pn">' + esc(x.name) + "</div>" +
        '<div class="pf">#' + esc(x.user_id) + "</div></div>" +
        '<div class="ti-count"><b>' + fmt(x.followers) + "</b><span>подп.</span></div></li>"
    )
    .join("");
}

// ---------------- friends picker ----------------
let FRIENDS = [];
const PICKED = new Set();
async function openFriends() {
  $("friendsModal").classList.remove("hidden");
  $("friendList").innerHTML = '<div class="muted">загрузка...</div>';
  PICKED.clear();
  updatePickCount();
  const j = await api("/api/friends");
  if (!j.ok) {
    $("friendList").innerHTML = '<div class="muted">⚠ ' + esc(j.error) + "</div>";
    return;
  }
  FRIENDS = j.items || [];
  $("friendsCount").textContent = "всего друзей: " + fmt(FRIENDS.length);
  renderFriends();
}
function closeFriends() {
  $("friendsModal").classList.add("hidden");
}
function friendsFiltered() {
  const q = ($("friendsFilter").value || "").trim().toLowerCase();
  const onlineOnly = $("friendsOnline").checked;
  return FRIENDS.filter(
    (f) =>
      (!onlineOnly || f.online) &&
      (!q || (f.name || "").toLowerCase().includes(q) || String(f.user_id).includes(q))
  );
}
function renderFriends() {
  const el = $("friendList");
  const list = friendsFiltered();
  if (!list.length) {
    el.innerHTML = '<div class="muted">никого не найдено</div>';
    return;
  }
  el.innerHTML = list
    .map((f) => {
      const checked = PICKED.has(f.user_id) ? "checked" : "";
      const prot = f.protected
        ? '<span class="tag on">уже защищён</span>'
        : "";
      const dis = f.protected ? "disabled" : "";
      return (
        '<label class="friend' + (f.protected ? " is-prot" : "") + '">' +
        '<input type="checkbox" ' + checked + " " + dis +
        ' onchange="pick(\'' + esc(f.user_id) + '\', this.checked)">' +
        '<img src="' + esc(f.image) + '" onerror="this.style.visibility=\'hidden\'">' +
        '<div class="fr-main"><div class="pn">' + esc(f.name) + "</div>" +
        '<div class="pf">#' + esc(f.user_id) + (f.online ? " · online" : "") + "</div></div>" +
        prot +
        "</label>"
      );
    })
    .join("");
}
function pick(uid, on) {
  if (on) PICKED.add(uid);
  else PICKED.delete(uid);
  updatePickCount();
}
function toggleAllFriends() {
  const list = friendsFiltered().filter((f) => !f.protected);
  const allPicked = list.length && list.every((f) => PICKED.has(f.user_id));
  list.forEach((f) => (allPicked ? PICKED.delete(f.user_id) : PICKED.add(f.user_id)));
  updatePickCount();
  renderFriends();
}
function updatePickCount() {
  $("pickCount").textContent = PICKED.size;
  $("btnAddFriends").disabled = PICKED.size === 0;
}
async function addPicked() {
  const items = FRIENDS.filter((f) => PICKED.has(f.user_id)).map((f) => ({
    id: f.user_id,
    name: f.name,
    image: f.image,
  }));
  if (!items.length) return;
  const j = await api("/api/exceptions/bulk", { items });
  if (!j.ok) return toast("ошибка", j.error, true);
  renderExceptions(j.items);
  toast("добавлено в исключения: " + fmt(j.added));
  closeFriends();
}

// ---------------- analytics ----------------
async function analyze() {
  const btn = $("btnAna");
  btn.disabled = true;
  btn.textContent = "сканирую связи... (это займёт время)";
  const j = await api("/api/analytics");
  btn.disabled = false;
  btn.textContent = "просканировать связи";
  if (!j.ok) return toast("ошибка", j.error, true);
  const s = j.stats;
  $("anaGrid").classList.remove("hidden");
  $("aMutual").textContent = fmt(s.mutual);
  $("aFans").textContent = fmt(s.fans);
  $("aNon").textContent = fmt(s.non_followers);
  $("anaHint").classList.remove("hidden");
  $("anaHint").textContent =
    "подписок: " + fmt(s.following) + " · подписчиков: " + fmt(s.followers) +
    " · фанаты не получают ответной подписки, невзаимные можно отписать";
}

// ---------------- jobs ----------------
async function startJob(kind) {
  const j = await api("/api/" + kind, collectJob());
  if (!j.ok) toast("ошибка", j.error, true);
  else {
    toast(kind === "follow" ? "задача подписок запущена" : "задача отписок запущена");
    scheduleStatus();
  }
}
async function stopJob() {
  await api("/api/stop", {});
  toast("остановка...");
}

function fmtEta(sec) {
  if (!sec || sec <= 0) return "—";
  const m = Math.floor(sec / 60), s = sec % 60;
  return m > 0 ? m + "м " + s + "с" : s + "с";
}
function renderLog(logs) {
  const el = $("log");
  el.innerHTML = logs
    .map((l) => '<div class="line ' + (l.l || "info") + '"><span class="ts">' + l.t + '</span><span class="msg">' + esc(l.m) + "</span></div>")
    .join("");
  el.scrollTop = el.scrollHeight;
}
let STATUS_TIMER = null;
function scheduleStatus() {
  if (STATUS_TIMER) clearTimeout(STATUS_TIMER);
  poll().then((running) => {
    // poll fast while a job runs, idle slowly otherwise
    STATUS_TIMER = setTimeout(scheduleStatus, running ? 1000 : 5000);
  });
}
async function poll() {
  let s;
  try {
    s = await api("/api/status");
  } catch (e) {
    return false;
  }
  if (!s || !s.stats) return false;
  const st = s.stats;
  $("mDone").textContent = st.done || 0;
  $("mErrors").textContent = st.errors || 0;
  $("mSkipped").textContent = st.skipped || 0;
  $("mRate").textContent = st.rate || 0;
  $("mEta").textContent = s.running ? fmtEta(st.eta) : "—";
  const total = st.total || 0;
  const pct = total ? Math.min(100, ((st.done || 0) / total) * 100) : s.running ? 100 : 0;
  $("barFill").style.width = pct + "%";
  $("bFollow").disabled = s.running;
  $("bUnfollow").disabled = s.running;
  renderLog(s.logs || []);
  return !!s.running;
}

// ---------------- live room chat (IMQ) ----------------
let CHAT_TIMER = null;
let LAST_ROOM = "";
let AI_ON = false;

function renderRecent(list) {
  const box = $("recentRooms");
  if (!box) return;
  box.innerHTML = (list || [])
    .map((r) => '<span class="room-chip" onclick="joinRecent(\'' + esc(r.room) + '\')">' + esc(r.name || r.room) + "</span>")
    .join("");
}

function fillSelect(sel, options, value) {
  sel.innerHTML = Object.entries(options || {})
    .map(([k, label]) => '<option value="' + esc(k) + '">' + esc(label) + "</option>")
    .join("");
  if (value) sel.value = value;
}

async function loadRecentRooms() {
  const r = await api("/api/room/recent");
  if (!r.ok) return;
  renderRecent(r.recent);
  fillSelect($("aiStyle"), r.styles, r.ai_style);
  fillSelect($("aiTemper"), r.tempers, r.ai_temper);
}

async function aiStyleChanged() {
  if (AI_ON) return; // применится при следующем включении
  await api("/api/room/ai", {
    enabled: false,
    style: $("aiStyle").value,
    temper: $("aiTemper").value,
  });
}

function joinRecent(room) {
  $("roomInput").value = room;
  roomJoin();
}

async function roomJoin() {
  const room = $("roomInput").value.trim();
  if (!room) { toast("Укажите комнату", "", true); return; }
  $("btnRoomJoin").disabled = true;
  $("roomHint").textContent = "Подключаюсь к IMQ…";
  const r = await api("/api/room/join", { room });
  $("btnRoomJoin").disabled = false;
  if (!r.ok) { $("roomHint").textContent = r.error || "Не удалось войти"; toast("Чат", r.error, true); return; }
  LAST_ROOM = room;
  setAIState(false);
  renderRecent(r.recent);
  $("kickedBanner").classList.add("hidden");
  $("chatRoomName").textContent = (r.name || r.room_id) + "  ·  " + (r.occupancy || 0) + "/" + (r.capacity || 0);
  $("roomHint").textContent = "Ты в комнате. Реплики появляются ниже.";
  $("chatLog").innerHTML = "";
  $("chatBox").classList.remove("hidden");
  if (CHAT_TIMER) clearInterval(CHAT_TIMER);
  CHAT_TIMER = setInterval(roomPoll, 2500);
  roomPoll();
}

function roomReconnect() {
  if (!LAST_ROOM) return;
  $("roomInput").value = LAST_ROOM;
  roomJoin();
}

function onKicked() {
  if (CHAT_TIMER) { clearInterval(CHAT_TIMER); CHAT_TIMER = null; }
  setAIState(false);
  $("kickedBanner").classList.remove("hidden");
  $("roomHint").textContent = "Соединение с комнатой потеряно.";
  toast("Чат", "тебя выкинуло из комнаты", true);
}

async function roomPoll() {
  const r = await api("/api/room/messages");
  if (!r.ok) return;
  for (const m of r.messages) appendChat(m);
  if (r.connected === false) { onKicked(); return; }
  if (AI_ON && r.ai === false) {
    setAIState(false);
    if (r.ai_error) toast("ИИ", r.ai_error, true);
  }
}

function setAIState(on) {
  AI_ON = on;
  $("btnAI").textContent = on ? "ИИ: вкл" : "ИИ: выкл";
  $("btnAI").classList.toggle("cyan", on);
  $("aiState").textContent = on ? "🤖 ИИ в чате" : "";
}

async function aiToggle() {
  const key = $("groqKey").value.trim();
  const r = await api("/api/room/ai", {
    enabled: !AI_ON,
    key,
    style: $("aiStyle").value,
    temper: $("aiTemper").value,
  });
  if (!r.ok) { toast("ИИ", r.error, true); return; }
  $("groqKey").value = "";
  setAIState(!!r.ai);
  toast(r.ai ? "ИИ включён — отвечает выборочно, как человек" : "ИИ выключен");
}

let LAST_CHAT_LINE = { key: "", ts: 0 };

function appendChat(m) {
  // guard against duplicate echoes of the same line (e.g. own/AI messages)
  const key = (m.name || m.user_id) + "\u0000" + m.text;
  const now = Date.now();
  if (key === LAST_CHAT_LINE.key && now - LAST_CHAT_LINE.ts < 6000) return;
  LAST_CHAT_LINE = { key, ts: now };
  const log = $("chatLog");
  const line = document.createElement("div");
  line.className = "chat-line" + (m.self ? " self" : "");
  line.innerHTML = '<span class="chat-who">' + esc(m.name || m.user_id) + '</span>' +
    '<span class="chat-text">' + esc(m.text) + '</span>';
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

async function roomSend() {
  const inp = $("chatInput");
  const text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  const r = await api("/api/room/send", { text });
  if (!r.ok) { toast("Чат", r.error, true); return; }
  appendChat({ name: "я", text, self: true });
}

async function roomLeave() {
  if (CHAT_TIMER) { clearInterval(CHAT_TIMER); CHAT_TIMER = null; }
  setAIState(false);
  await api("/api/room/leave", {});
  $("chatBox").classList.add("hidden");
  $("roomHint").textContent = "Ты вышел из комнаты.";
}

async function logout() {
  if (CHAT_TIMER) { clearInterval(CHAT_TIMER); CHAT_TIMER = null; }
  await api("/api/room/leave", {}).catch(() => {});
  await api("/api/auth/logout", {});
  location.href = "/login";
}

$("lookupInput").addEventListener("keydown", (e) => { if (e.key === "Enter") lookup(); });
init();
