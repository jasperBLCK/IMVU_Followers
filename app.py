"""Веб-панель IMVU — авторизация, пробив аккаунтов, подписки/отписки, радио.

FastAPI-приложение с двумя уровнями доступа:

* **Гость** — только чтение: публичная статистика любого аватара по нику.
* **Аккаунт** — вход с данными IMVU: дашборд, аналитика, исключения,
  задачи подписок/отписок, чат комнат и собственная радиостанция «эфир».

Данные хранятся в SQLite (``data.db``), загруженные треки — в
``uploads/live/``. Запуск: ``python app.py`` → http://localhost:5000.
"""

import os
import secrets
import shutil
import threading
import time
from collections import deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import radio
import storage
from ai_chat import (
    DEFAULT_PROVIDER,
    DEFAULT_STYLE,
    DEFAULT_TEMPER,
    PROVIDERS,
    STYLES,
    TEMPERS,
    AIChatError,
    AIChatter,
)
from imq_client import IMQError, RoomChatSession
from imvu_client import IMVUClient, IMVUError, TwoFactorRequired

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
LIVE_DIR = radio.LIVE_DIR
ALLOWED_AUDIO_EXT = {".mp3"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 МБ на один запрос загрузки

DEFAULT_SETTINGS = {
    "username": "",
    "target_id": "",
    "max_follows": 2000,
    "follow_delay": 0.8,
    "unfollow_delay": 0.3,
    "exceptions": [],  # [{"id": "123", "name": "Alice"}]
    "groq_api_key": "",
    "anthropic_api_key": "",
    "ai_provider": DEFAULT_PROVIDER,
    "ai_style": DEFAULT_STYLE,
    "ai_temper": DEFAULT_TEMPER,
    "recent_rooms": [],  # [{"room": "room-1-2", "name": "..."}]
}

JOB_KEYS = ("target_id", "max_follows", "follow_delay", "unfollow_delay")

storage.init_db()
app = FastAPI(title="IMVU_NET", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def load_settings():
    return storage.load_settings(DEFAULT_SETTINGS)


def save_settings(settings):
    storage.save_settings(settings, DEFAULT_SETTINGS)


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        self.message = message
        self.status = status
        self.extra = extra


@app.exception_handler(ApiError)
async def _api_error_handler(request, exc):
    body = {"ok": False, "error": exc.message}
    body.update(exc.extra)
    return JSONResponse(body, status_code=exc.status)


async def _json(request):
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _set_session_cookie(resp, sid):
    resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp


# --------------------------------------------------------------------------- #
# Серверные сессии (живой IMVU-клиент не попадает в cookie)
# --------------------------------------------------------------------------- #
_sessions = {}
_sessions_lock = threading.Lock()

# Входы, ожидающие код с почты: pending_id -> контекст входа
_pending_2fa = {}


def _new_session(role, client, username=""):
    sid = secrets.token_hex(16)
    with _sessions_lock:
        _sessions[sid] = {"role": role, "client": client, "username": username}
    return sid


def current(request):
    sid = request.cookies.get("sid")
    if not sid:
        return None
    with _sessions_lock:
        return _sessions.get(sid)


def require(request, *roles):
    ctx = current(request)
    if not ctx or ctx["role"] not in roles:
        raise ApiError("Требуется вход", 401)
    return ctx


# --------------------------------------------------------------------------- #
# Фоновый исполнитель задач
# --------------------------------------------------------------------------- #
class TaskRunner:
    """Выполняет одну задачу подписок/отписок в фоновом потоке."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.logs = deque(maxlen=500)
        self.running = False
        self.kind = None
        self.stats = {}
        self.started_at = None

    def log(self, message, level="info"):
        self.logs.append(
            {"t": time.strftime("%H:%M:%S"), "m": str(message), "l": level}
        )

    def is_running(self):
        return self.running

    def stop(self):
        self._stop.set()

    def stopping(self):
        return self._stop.is_set()

    def bump(self, field, amount=1):
        self.stats[field] = self.stats.get(field, 0) + amount

    def start(self, kind, target, total=0):
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self.logs.clear()
            self.running = True
            self.kind = kind
            self.started_at = time.time()
            self.stats = {"done": 0, "errors": 0, "skipped": 0, "total": total}
            self._thread = threading.Thread(
                target=self._wrap, args=(target,), daemon=True
            )
            self._thread.start()
            return True

    def snapshot(self):
        stats = dict(self.stats)
        elapsed = (time.time() - self.started_at) if self.started_at else 0
        done = stats.get("done", 0)
        rate = (done / elapsed * 60) if elapsed > 1 else 0
        total = stats.get("total", 0)
        eta = ((total - done) / (rate / 60)) if rate and total else 0
        stats.update(elapsed=round(elapsed), rate=round(rate, 1), eta=round(eta))
        return {
            "running": self.running,
            "kind": self.kind,
            "stats": stats,
            "logs": list(self.logs),
        }

    def _wrap(self, target):
        try:
            target()
        except IMVUError as exc:
            self.log(f"Ошибка: {exc}", "error")
        except Exception as exc:  # pragma: no cover - defensive
            self.log(f"Непредвиденная ошибка: {exc}", "error")
        finally:
            self.running = False
            self.kind = None
            self.log("Задача завершена.", "done")


runner = TaskRunner()


def _exception_ids(settings):
    return {str(e.get("id")) for e in settings.get("exceptions", []) if e.get("id")}


def run_follow(client, settings):
    runner.log(f"Вошёл. User ID: {client.my_user_id}", "ok")
    target_id = client.resolve_username(settings["target_id"])
    max_follows = int(settings["max_follows"])
    delay = float(settings["follow_delay"])

    done_ids = {client.my_user_id}
    total = 0
    runner.log(f"Цель: user-{target_id}. Собираю подписчиков...")

    for card in client.iter_subscribers(target_id):
        if total >= max_follows or runner.stopping():
            break
        if card.user_id in done_ids:
            continue
        done_ids.add(card.user_id)

        code = client.follow(card.user_id)
        if code in (200, 201):
            total += 1
            runner.bump("done")
            runner.log(f"[{total}/{max_follows}] подписался на {card.name}", "ok")
        elif code in (400, 429):
            runner.log("rate limit — пауза 10 сек...", "warn")
            time.sleep(10)
            if client.follow(card.user_id) in (200, 201):
                total += 1
                runner.bump("done")
                runner.log(f"[{total}/{max_follows}] подписался на {card.name}", "ok")
            else:
                runner.bump("skipped")
                runner.log(f"пропуск {card.name} (rate limit)", "warn")
        elif code == 0:
            runner.bump("errors")
            runner.log(f"обрыв соединения, пропуск {card.name}", "error")
        else:
            runner.bump("skipped")
            runner.log(f"[{code}] пропуск {card.name}", "warn")
        time.sleep(delay)

    if runner.stopping():
        runner.log("Остановлено пользователем.", "warn")
    runner.log(f"Итого подписок: {total}", "done")


def run_unfollow(client, settings):
    runner.log(f"Вошёл. User ID: {client.my_user_id}", "ok")
    delay = float(settings["unfollow_delay"])
    only_non_followers = bool(settings.get("only_non_followers"))
    protected = _exception_ids(settings)
    if protected:
        runner.log(f"Защищено от отписки: {len(protected)}", "info")
    total = 0

    if only_non_followers:
        runner.log("Считаю невзаимных подписок...")
        targets = client.get_non_followers(exclude=protected)
        runner.stats["total"] = len(targets)
        runner.log(f"Невзаимных (без исключений): {len(targets)}")
        for uid in targets:
            if runner.stopping():
                break
            if client.unfollow(uid):
                total += 1
                runner.bump("done")
                runner.log(f"[{total}] отписался от user-{uid}", "ok")
            else:
                runner.bump("skipped")
                runner.log(f"пропуск user-{uid}", "warn")
            time.sleep(delay)
    else:
        while not runner.stopping():
            cards = [c for c in client.iter_subscriptions() if c.user_id not in protected]
            if not cards:
                break
            runner.stats["total"] = max(runner.stats.get("total", 0), len(cards))
            for card in cards:
                if runner.stopping():
                    break
                if card.user_id in protected:
                    continue
                if client.unfollow(card.user_id):
                    total += 1
                    runner.bump("done")
                    runner.log(f"[{total}] отписался от {card.name}", "ok")
                else:
                    runner.bump("skipped")
                    runner.log(f"пропуск {card.name}", "warn")
                time.sleep(delay)

    if runner.stopping():
        runner.log("Остановлено пользователем.", "warn")
    runner.log(f"Итого отписок: {total}", "done")


# --------------------------------------------------------------------------- #
# Страницы
# --------------------------------------------------------------------------- #
@app.get("/")
def index(request: Request):
    if not current(request):
        return RedirectResponse("/login")
    return FileResponse(os.path.join(STATIC_DIR, "app.html"))


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/radio")
def radio_page():
    # radio.html использует относительные пути — та же страница работает на GitHub Pages
    return RedirectResponse("/static/radio.html")


@app.get("/live/{token}")
def live_page(token: str):
    return FileResponse(os.path.join(STATIC_DIR, "live.html"))


# --------------------------------------------------------------------------- #
# Эфир: владелец грузит треки, слушатели ловят их по уникальной ссылке
# --------------------------------------------------------------------------- #
_live_lock = threading.Lock()


def _live_public(meta):
    tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
    return {
        "on": meta["on"],
        "started_at": meta["started_at"],
        "server_time": time.time(),
        "tracks": [{"id": t["id"], "name": t["name"], "duration": t["duration"]} for t in tracks],
        "now": radio.live_now(meta),
    }


@app.get("/api/live")
def live_state(request: Request):
    require(request, "user")
    meta = storage.load_live()
    out = _live_public(meta)
    out.update({"ok": True, "token": meta["token"]})
    return out


@app.post("/api/live/upload")
async def live_upload(request: Request):
    require(request, "user")
    length = int(request.headers.get("content-length") or 0)
    if length > MAX_UPLOAD_BYTES:
        raise ApiError("Слишком большая загрузка — максимум 512 МБ за раз", 413)
    form = await request.form()
    files = form.getlist("files")
    if not files:
        raise ApiError("Нет файлов")
    os.makedirs(LIVE_DIR, exist_ok=True)
    added = 0
    rejected = []
    with _live_lock:
        for f in files:
            name = os.path.basename(getattr(f, "filename", "") or "")
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                rejected.append(name + " — нужен mp3")
                continue
            if not radio.is_real_mp3(f.file):
                rejected.append(name + " — внутри не mp3 (переименованный файл?), сконвертируй в настоящий mp3")
                continue
            tid = secrets.token_hex(8)
            path = os.path.join(LIVE_DIR, tid + ext)
            with open(path, "wb") as out:
                shutil.copyfileobj(f.file, out)
            duration = radio.mp3_duration(path)
            if not duration:
                os.remove(path)
                rejected.append(name + " — не определилась длительность")
                continue
            storage.add_track(tid, os.path.splitext(name)[0], tid + ext, round(duration, 3))
            added += 1
    if not added:
        raise ApiError("; ".join(rejected) or "Нужны mp3-файлы")
    return {"ok": True, "added": added, "rejected": rejected}


@app.delete("/api/live/track/{tid}")
def live_delete_track(tid: str, request: Request):
    require(request, "user")
    with _live_lock:
        file = storage.delete_track(tid)
    if file is None:
        raise ApiError("Трек не найден", 404)
    try:
        os.remove(os.path.join(LIVE_DIR, file))
    except OSError:
        pass
    return {"ok": True}


@app.post("/api/live/toggle")
async def live_toggle(request: Request):
    require(request, "user")
    on = bool((await _json(request)).get("on"))
    with _live_lock:
        meta = storage.load_live()
        if on and not meta["tracks"]:
            raise ApiError("Сначала загрузи треки")
        storage.save_live_state(on=on, started_at=time.time() if on else None)
    return {"ok": True, "on": on}


@app.post("/api/live/regen")
def live_regen(request: Request):
    require(request, "user")
    token = secrets.token_urlsafe(8)
    with _live_lock:
        storage.save_live_state(token=token)
    return {"ok": True, "token": token}


def _check_live_token(token):
    meta = storage.load_live()
    if not secrets.compare_digest(token, meta["token"]):
        raise ApiError("Эфир не найден", 404)
    return meta


@app.get("/api/live/{token}/now")
def live_now_public(token: str):
    meta = _check_live_token(token)
    out = _live_public(meta)
    out["ok"] = True
    return out


@app.get("/api/live/{token}/audio/{tid}")
def live_audio(token: str, tid: str):
    meta = _check_live_token(token)
    track = next((t for t in meta["tracks"] if t["id"] == tid), None)
    if not track:
        raise ApiError("Трек не найден", 404)
    return FileResponse(os.path.join(LIVE_DIR, track["file"]), media_type="audio/mpeg")


@app.get("/live/{token}/stream")
def live_stream(token: str):
    _check_live_token(token)
    return StreamingResponse(
        radio.stream_generator(),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Accept-Ranges": "none",
            "icy-name": "IMVU_NET live",
            "Access-Control-Allow-Origin": "*",
        },
    )


# --------------------------------------------------------------------------- #
# Авторизация
# --------------------------------------------------------------------------- #
def _login_ok(client, username, profile):
    """Успешный вход: серверная сессия + запоминаем логин (без пароля)."""
    sid = _new_session("user", client, username)
    settings = load_settings()
    settings["username"] = username
    save_settings(settings)
    resp = JSONResponse({"ok": True, "role": "user", "profile": profile})
    resp.delete_cookie("p2fa")
    return _set_session_cookie(resp, sid)


@app.post("/api/auth/account")
async def auth_account(request: Request):
    body = await _json(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        raise ApiError("Укажите логин и пароль")
    try:
        client = IMVUClient(username, password)
        client.login()
        profile = client.get_full_profile()
    except TwoFactorRequired as exc:
        pending_id = secrets.token_hex(16)
        _pending_2fa[pending_id] = {"client": client, "username": username}
        resp = JSONResponse(
            {"ok": False, "need_2fa": True, "error": str(exc), "email": exc.email}
        )
        resp.set_cookie("p2fa", pending_id, httponly=True, samesite="lax", max_age=900)
        return resp
    except IMVUError as exc:
        raise ApiError(str(exc))
    return _login_ok(client, username, profile)


@app.post("/api/auth/2fa/resend")
async def auth_2fa_resend(request: Request):
    """Попросить IMVU выслать свежий код (повторная попытка входа шлёт письмо)."""
    pending_id = request.cookies.get("p2fa")
    pending = _pending_2fa.get(pending_id)
    if not pending:
        raise ApiError("Сессия входа истекла — войдите заново", restart=True)
    client = pending["client"]
    try:
        client.login()
    except TwoFactorRequired as exc:
        return {"ok": True, "resent": True, "email": exc.email}
    except IMVUError as exc:
        raise ApiError(str(exc))
    # Кода не потребовалось — вход уже прошёл.
    profile = client.get_full_profile()
    _pending_2fa.pop(pending_id, None)
    return _login_ok(client, pending["username"], profile)


@app.post("/api/auth/2fa")
async def auth_2fa(request: Request):
    body = await _json(request)
    code = (str(body.get("code") or "")).strip()
    if not code:
        raise ApiError("Введите код из письма")
    pending_id = request.cookies.get("p2fa")
    pending = _pending_2fa.get(pending_id)
    if not pending:
        raise ApiError("Сессия входа истекла — войдите заново", restart=True)
    client = pending["client"]
    try:
        client.login(code=code)
        profile = client.get_full_profile()
    except TwoFactorRequired:
        raise ApiError("Неверный код — попробуйте ещё раз", need_2fa=True)
    except IMVUError as exc:
        raise ApiError(str(exc))
    _pending_2fa.pop(pending_id, None)
    return _login_ok(client, pending["username"], profile)


@app.post("/api/auth/guest")
def auth_guest():
    sid = _new_session("guest", IMVUClient.anonymous())
    resp = JSONResponse({"ok": True, "role": "guest"})
    return _set_session_cookie(resp, sid)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    sid = request.cookies.get("sid")
    if sid:
        with _sessions_lock:
            _sessions.pop(sid, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sid")
    return resp


@app.get("/api/me")
def api_me(request: Request):
    ctx = current(request)
    if not ctx:
        return {"ok": False, "role": None}
    out = {"ok": True, "role": ctx["role"], "username": ctx.get("username", "")}
    if ctx["role"] == "user":
        try:
            out["profile"] = ctx["client"].get_full_profile()
        except IMVUError:
            pass
    return out


@app.get("/api/saved-login")
def api_saved_login():
    s = load_settings()
    return {"username": s.get("username", ""), "has_password": False}


# --------------------------------------------------------------------------- #
# Пробив аккаунта (гость + аккаунт)
# --------------------------------------------------------------------------- #
@app.post("/api/lookup")
async def api_lookup(request: Request):
    ctx = require(request, "guest", "user")
    target = (await _json(request)).get("target", "")
    if not target:
        raise ApiError("Укажите ник или ID")
    client = ctx["client"]
    try:
        uid = client.resolve_username(target)
        profile = client.get_full_profile(uid)
        page = client._list_page("subscribers", uid, limit=12)
        following = (
            client.is_following(uid)
            if ctx["role"] == "user" and uid != client.my_user_id
            else None
        )
        return {
            "ok": True,
            "user_id": uid,
            "profile": profile,
            "is_following": following,
            "preview": [c.as_dict() for c in page.cards],
        }
    except IMVUError as exc:
        raise ApiError(str(exc))


# --------------------------------------------------------------------------- #
# Настройки и исключения (только аккаунт)
# --------------------------------------------------------------------------- #
@app.get("/api/settings")
def get_settings(request: Request):
    require(request, "user")
    s = load_settings()
    return {k: s.get(k, DEFAULT_SETTINGS[k]) for k in JOB_KEYS}


@app.post("/api/settings")
async def post_settings(request: Request):
    require(request, "user")
    incoming = await _json(request)
    settings = load_settings()
    for key in JOB_KEYS:
        if key in incoming and incoming[key] != "":
            settings[key] = incoming[key]
    save_settings(settings)
    return {"ok": True}


@app.get("/api/exceptions")
def get_exceptions(request: Request):
    require(request, "user")
    return {"ok": True, "items": load_settings().get("exceptions", [])}


@app.post("/api/exceptions")
async def add_exception(request: Request):
    ctx = require(request, "user")
    target = (await _json(request)).get("target", "")
    if not target:
        raise ApiError("Укажите ник или ID")
    try:
        uid = ctx["client"].resolve_username(target)
        card = ctx["client"].get_profile_summary(uid)
    except IMVUError as exc:
        raise ApiError(str(exc))
    settings = load_settings()
    items = settings.get("exceptions", [])
    if not any(str(e.get("id")) == uid for e in items):
        items.append({"id": uid, "name": card.name, "image": card.image})
        settings["exceptions"] = items
        save_settings(settings)
    return {"ok": True, "items": items}


@app.post("/api/exceptions/bulk")
async def add_exceptions_bulk(request: Request):
    """Добавить сразу несколько пользователей в белый список отписок.

    Принимает ``{"items": [{"id", "name", "image"}, ...]}`` — используется
    модалкой «выбрать из друзей», чтобы не делать по запросу на человека.
    """
    require(request, "user")
    incoming = (await _json(request)).get("items", [])
    settings = load_settings()
    items = settings.get("exceptions", [])
    have = {str(e.get("id")) for e in items}
    added = 0
    for entry in incoming:
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("id") or "").strip()
        if not uid or uid in have:
            continue
        items.append({
            "id": uid,
            "name": entry.get("name", "") or f"user-{uid}",
            "image": entry.get("image", ""),
        })
        have.add(uid)
        added += 1
    settings["exceptions"] = items
    save_settings(settings)
    return {"ok": True, "added": added, "items": items}


@app.delete("/api/exceptions/{uid}")
def del_exception(uid: str, request: Request):
    require(request, "user")
    settings = load_settings()
    settings["exceptions"] = [
        e for e in settings.get("exceptions", []) if str(e.get("id")) != str(uid)
    ]
    save_settings(settings)
    return {"ok": True, "items": settings["exceptions"]}


# --------------------------------------------------------------------------- #
# Друзья и топ подписок (только аккаунт)
# --------------------------------------------------------------------------- #
@app.get("/api/friends")
def api_friends(request: Request):
    """Друзья аккаунта IMVU (для выбора исключений)."""
    ctx = require(request, "user")
    try:
        friends = ctx["client"].get_friends()
    except IMVUError as exc:
        raise ApiError(str(exc))
    protected = {str(e.get("id")) for e in load_settings().get("exceptions", [])}
    for f in friends:
        f["protected"] = f["user_id"] in protected
    return {"ok": True, "items": friends, "total": len(friends)}


@app.get("/api/top-subscriptions")
def api_top_subscriptions(request: Request):
    """Топ моих подписок по числу их подписчиков."""
    ctx = require(request, "user")
    try:
        n = max(1, min(int(request.query_params.get("n", 5)), 20))
    except (TypeError, ValueError):
        n = 5
    try:
        max_scan = max(50, min(int(request.query_params.get("scan", 2000)), 6000))
    except (TypeError, ValueError):
        max_scan = 2000
    try:
        top, scanned = ctx["client"].top_subscriptions(n=n, max_scan=max_scan)
    except IMVUError as exc:
        raise ApiError(str(exc))
    return {"ok": True, "items": top, "scanned": scanned}


# --------------------------------------------------------------------------- #
# Аналитика (только аккаунт)
# --------------------------------------------------------------------------- #
@app.get("/api/analytics")
def api_analytics(request: Request):
    ctx = require(request, "user")
    try:
        return {"ok": True, "stats": ctx["client"].get_relationship_stats()}
    except IMVUError as exc:
        raise ApiError(str(exc))


# --------------------------------------------------------------------------- #
# Задачи (только аккаунт)
# --------------------------------------------------------------------------- #
def _job_settings(incoming):
    settings = load_settings()
    for key in JOB_KEYS:
        if key in incoming and incoming[key] not in ("", None):
            settings[key] = incoming[key]
    settings["only_non_followers"] = bool(incoming.get("only_non_followers"))
    save_settings(settings)
    return settings


@app.post("/api/follow")
async def api_follow(request: Request):
    ctx = require(request, "user")
    settings = _job_settings(await _json(request))
    if not settings["target_id"]:
        raise ApiError("Укажите цель (ник или ID)")
    client = ctx["client"]
    total = int(settings.get("max_follows") or 0)
    started = runner.start("follow", lambda: run_follow(client, settings), total=total)
    if not started:
        raise ApiError("Задача уже выполняется", 409)
    return {"ok": True}


@app.post("/api/unfollow")
async def api_unfollow(request: Request):
    ctx = require(request, "user")
    settings = _job_settings(await _json(request))
    client = ctx["client"]
    started = runner.start("unfollow", lambda: run_unfollow(client, settings))
    if not started:
        raise ApiError("Задача уже выполняется", 409)
    return {"ok": True}


@app.post("/api/follow-one")
async def api_follow_one(request: Request):
    ctx = require(request, "user")
    target = (await _json(request)).get("target", "")
    if not target:
        raise ApiError("Не указана цель")
    try:
        uid = ctx["client"].resolve_username(target)
        code = ctx["client"].follow(uid)
    except IMVUError as exc:
        raise ApiError(str(exc))
    ok = code in (200, 201)
    return {"ok": ok, "code": code, "user_id": uid,
            "error": None if ok else f"HTTP {code}"}


@app.post("/api/unfollow-one")
async def api_unfollow_one(request: Request):
    ctx = require(request, "user")
    target = (await _json(request)).get("target", "")
    if not target:
        raise ApiError("Не указана цель")
    try:
        uid = ctx["client"].resolve_username(target)
        ok = ctx["client"].unfollow(uid)
    except IMVUError as exc:
        raise ApiError(str(exc))
    return {"ok": ok, "user_id": uid,
            "error": None if ok else "Не удалось отписаться"}


@app.post("/api/stop")
def api_stop(request: Request):
    require(request, "user")
    runner.stop()
    return {"ok": True}


@app.get("/api/status")
def api_status(request: Request):
    require(request, "user")
    return runner.snapshot()


# --------------------------------------------------------------------------- #
# Живой чат комнаты (IMQ)
# --------------------------------------------------------------------------- #
def _chat_name(ctx, cid):
    """Имя по числовому id, кэшируется в рамках веб-сессии."""
    cache = ctx.setdefault("_name_cache", {})
    cid = str(cid)
    if cid not in cache:
        name = cid
        try:
            card = ctx["client"].get_profile_summary(cid)
            name = card.avatar_name or card.name or cid
        except IMVUError:
            pass
        cache[cid] = name
    return cache[cid]


def _remember_room(room_id, name):
    settings = load_settings()
    recent = [r for r in settings.get("recent_rooms", []) if r.get("room") != room_id]
    recent.insert(0, {"room": room_id, "name": name})
    settings["recent_rooms"] = recent[:10]
    save_settings(settings)
    return settings["recent_rooms"]


def _stop_ai(ctx):
    ai = ctx.pop("ai", None)
    if ai:
        ai.stop()


@app.post("/api/room/join")
async def api_room_join(request: Request):
    ctx = require(request, "user")
    room = (await _json(request)).get("room", "").strip()
    if not room:
        raise ApiError("Укажите комнату (room-<id> или ссылку)")
    _stop_ai(ctx)
    old = ctx.pop("chat", None)
    if old:
        old.stop()
    chat = RoomChatSession(ctx["client"], room)
    try:
        info = chat.start()
    except (IMQError, IMVUError) as exc:
        raise ApiError(str(exc))
    ctx["chat"] = chat
    recent = _remember_room(info.room_id, info.name)
    return {
        "ok": True,
        "room_id": info.room_id,
        "name": info.name,
        "occupancy": info.occupancy,
        "capacity": info.capacity,
        "recent": recent,
    }


@app.get("/api/room/recent")
def api_room_recent(request: Request):
    require(request, "user")
    settings = load_settings()
    return {
        "ok": True,
        "recent": settings.get("recent_rooms", []),
        "has_groq_key": bool(settings.get("groq_api_key")),
        "has_anthropic_key": bool(settings.get("anthropic_api_key")),
        "ai_provider": settings.get("ai_provider", DEFAULT_PROVIDER),
        "ai_style": settings.get("ai_style", DEFAULT_STYLE),
        "ai_temper": settings.get("ai_temper", DEFAULT_TEMPER),
        "providers": {k: v["label"] for k, v in PROVIDERS.items()},
        "styles": {k: v["label"] for k, v in STYLES.items()},
        "tempers": {k: v["label"] for k, v in TEMPERS.items()},
    }


@app.get("/api/room/messages")
def api_room_messages(request: Request):
    ctx = require(request, "user")
    chat = ctx.get("chat")
    if not chat:
        raise ApiError("Вы не в комнате")
    # свои реплики UI рисует локально, поэтому их эхо здесь отбрасывается
    msgs = [
        {
            "user_id": m.user_id,
            "name": _chat_name(ctx, m.user_id),
            "text": m.text,
            "sequence": m.sequence,
            "self": False,
        }
        for m in chat.poll()
        if str(m.user_id) != str(ctx["client"].my_user_id)
    ]
    ai = ctx.get("ai")
    if ai:
        # ответы ИИ уходят с нашего аккаунта, их IMQ-эхо отфильтровано выше —
        # показываем их явно
        for text in ai.drain_sent():
            msgs.append(
                {
                    "user_id": str(ctx["client"].my_user_id),
                    "name": "я · ИИ",
                    "text": text,
                    "sequence": 0,
                    "self": True,
                }
            )
    return {
        "ok": True,
        "messages": msgs,
        "connected": chat.is_alive(),
        "ai": bool(ai and ai.alive()),
        "ai_error": ai.last_error if ai else "",
    }


@app.post("/api/room/send")
async def api_room_send(request: Request):
    ctx = require(request, "user")
    chat = ctx.get("chat")
    if not chat:
        raise ApiError("Вы не в комнате")
    text = (await _json(request)).get("text", "").strip()
    if not text:
        raise ApiError("Пустое сообщение")
    try:
        chat.send(text)
    except IMQError as exc:
        raise ApiError(str(exc))
    return {"ok": True}


@app.post("/api/room/leave")
def api_room_leave(request: Request):
    ctx = require(request, "user")
    _stop_ai(ctx)
    chat = ctx.pop("chat", None)
    if chat:
        chat.stop()
    return {"ok": True}


@app.post("/api/room/ai")
async def api_room_ai(request: Request):
    ctx = require(request, "user")
    body = await _json(request)
    enabled = bool(body.get("enabled"))
    key = (body.get("key") or "").strip()
    provider = (body.get("provider") or "").strip()
    style = (body.get("style") or "").strip()
    temper = (body.get("temper") or "").strip()
    settings = load_settings()
    changed = False
    if provider in PROVIDERS:
        settings["ai_provider"] = provider
        changed = True
    active = settings.get("ai_provider", DEFAULT_PROVIDER)
    if active not in PROVIDERS:
        active = DEFAULT_PROVIDER
    if key:
        settings[PROVIDERS[active]["key_field"]] = key
        changed = True
    if style in STYLES:
        settings["ai_style"] = style
        changed = True
    if temper in TEMPERS:
        settings["ai_temper"] = temper
        changed = True
    if changed:
        save_settings(settings)
    if not enabled:
        _stop_ai(ctx)
        return {"ok": True, "ai": False}
    chat = ctx.get("chat")
    if not chat or not chat.is_alive():
        raise ApiError("Сначала войдите в комнату")
    api_key = settings.get(PROVIDERS[active]["key_field"], "")
    if not api_key:
        raise ApiError(f"Добавьте ключ для {PROVIDERS[active]['label']}")
    _stop_ai(ctx)
    try:
        ai = AIChatter(
            api_key,
            chat.send,
            lambda cid: _chat_name(ctx, cid),
            ctx["client"].my_user_id,
            ctx.get("username", ""),
            style=settings.get("ai_style", DEFAULT_STYLE),
            temper=settings.get("ai_temper", DEFAULT_TEMPER),
            provider=active,
        )
    except AIChatError as exc:
        raise ApiError(str(exc))
    chat.on_line = ai.handle
    ctx["ai"] = ai
    return {"ok": True, "ai": True}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
