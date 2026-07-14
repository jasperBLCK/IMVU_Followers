const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = { method: body ? "POST" : "GET" };
  if (body) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  return r.json();
}

// boot sequence typewriter
const BOOT = [
  "[ booting imvu_net kernel v2.0 ]",
  "tls handshake .......... OK",
  "api.imvu.com ........... ONLINE",
  "select access level _",
];
(function bootSeq() {
  const el = $("boot");
  let i = 0;
  (function next() {
    if (i >= BOOT.length) return;
    const cls = BOOT[i].includes("OK") || BOOT[i].includes("ONLINE") ? "ok" : "";
    el.innerHTML += `<span class="${cls}">${BOOT[i]}</span>\n`;
    i++;
    setTimeout(next, 350);
  })();
})();

function switchTab(which) {
  const acc = which === "account";
  $("tabAccount").classList.toggle("active", acc);
  $("tabGuest").classList.toggle("active", !acc);
  $("paneAccount").classList.toggle("hidden", !acc);
  $("paneGuest").classList.toggle("hidden", acc);
  $("pane2fa").classList.add("hidden");
  $("err").textContent = "";
}

function show2fa(show) {
  $("pane2fa").classList.toggle("hidden", !show);
  $("paneAccount").classList.toggle("hidden", show);
  if (show) {
    $("code2fa").value = "";
    $("code2fa").focus();
  }
}

function cancel2fa() {
  setErr("");
  show2fa(false);
  const btn = $("btnLogin");
  btn.disabled = false;
  btn.textContent = "[ войти в систему ]";
}

function setErr(msg) {
  $("err").textContent = msg || "";
}

async function loginAccount() {
  setErr("");
  const username = $("username").value.trim();
  const password = $("password").value;
  if (!username || !password) return setErr("введите логин и пароль");
  const btn = $("btnLogin");
  btn.disabled = true;
  btn.textContent = "[ авторизация... ]";
  const j = await api("/api/auth/account", {
    username,
    password,
    remember: $("remember").checked,
  });
  if (j.ok) {
    sessionStorage.setItem("just_logged_in", JSON.stringify(j.profile || {}));
    location.href = "/";
  } else if (j.need_2fa) {
    setErr("");
    if (j.email) $("email2fa").textContent = j.email;
    show2fa(true);
  } else {
    setErr("ACCESS DENIED: " + (j.error || "ошибка входа"));
    btn.disabled = false;
    btn.textContent = "[ войти в систему ]";
  }
}

async function submit2fa() {
  setErr("");
  const code = $("code2fa").value.trim();
  if (!code) return setErr("введите код из письма");
  const btn = $("btn2fa");
  btn.disabled = true;
  btn.textContent = "[ проверка... ]";
  const j = await api("/api/auth/2fa", { code });
  btn.disabled = false;
  btn.textContent = "[ подтвердить код ]";
  if (j.ok) {
    sessionStorage.setItem("just_logged_in", JSON.stringify(j.profile || {}));
    location.href = "/";
  } else if (j.restart) {
    setErr(j.error || "сессия истекла — войдите заново");
    cancel2fa();
  } else {
    setErr("ACCESS DENIED: " + (j.error || "неверный код"));
  }
}

async function resend2fa() {
  setErr("");
  const btn = $("btnResend");
  btn.disabled = true;
  btn.textContent = "[ отправка... ]";
  const j = await api("/api/auth/2fa/resend", {});
  btn.disabled = false;
  btn.textContent = "[ отправить код повторно ]";
  if (j.ok && j.profile) {
    sessionStorage.setItem("just_logged_in", JSON.stringify(j.profile || {}));
    location.href = "/";
  } else if (j.ok) {
    if (j.email) $("email2fa").textContent = j.email;
    setErr("код отправлен повторно — проверь почту (и спам)");
  } else if (j.restart) {
    setErr(j.error || "сессия истекла — войдите заново");
    cancel2fa();
  } else {
    setErr("ACCESS DENIED: " + (j.error || "не удалось отправить код"));
  }
}

async function loginGuest() {
  setErr("");
  const btn = $("btnGuest");
  btn.disabled = true;
  btn.textContent = "[ вход... ]";
  const j = await api("/api/auth/guest", {});
  if (j.ok) {
    sessionStorage.setItem("guest_login", "1");
    location.href = "/";
  } else {
    setErr(j.error || "ошибка");
    btn.disabled = false;
    btn.textContent = "[ войти как гость ]";
  }
}

// prefill saved username
(async function () {
  const s = await api("/api/saved-login");
  if (s.username) {
    $("username").value = s.username;
    if (s.has_password) $("password").placeholder = "•••••••• (сохранён)";
  }
})();

$("password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loginAccount();
});

$("code2fa").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submit2fa();
});
