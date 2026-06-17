"""Web manager for IMVU — auth, guest lookup, follow/unfollow & analytics.

A Flask app with two access levels:

* **Guest** — read-only. Look up public stats (followers / following / online /
  country / VIP …) for any avatar by nickname. No automation.
* **Account** — log in with IMVU credentials to unlock the dashboard, target
  preview, relationship analytics, an unfollow whitelist ("exceptions") and the
  follow / unfollow jobs with a live log.

No database and no build step. The frontend lives in ``static/`` and is themed
like a terminal. Run with ``python app.py`` then open http://localhost:5000.
"""

import json
import os
import secrets
import threading
import time
from collections import deque
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
)

from imvu_client import IMVUClient, IMVUError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.secret_key = os.environ.get("IMVU_SECRET") or secrets.token_hex(32)

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "settings.json")

DEFAULT_SETTINGS = {
    "username": "",
    "password": "",
    "target_id": "",
    "max_follows": 2000,
    "follow_delay": 0.8,
    "unfollow_delay": 0.3,
    "exceptions": [],  # [{"id": "123", "name": "Alice"}]
}

JOB_KEYS = ("target_id", "max_follows", "follow_delay", "unfollow_delay")


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                settings.update(json.load(fh))
        except (OSError, ValueError):
            pass
    return settings


def save_settings(settings):
    to_save = {k: settings.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
    with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
        json.dump(to_save, fh, indent=2)


# --------------------------------------------------------------------------- #
# Server-side session store (keeps the live IMVU client out of the cookie)
# --------------------------------------------------------------------------- #
_sessions = {}
_sessions_lock = threading.Lock()


def _new_session(role, client, username=""):
    sid = secrets.token_hex(16)
    with _sessions_lock:
        _sessions[sid] = {"role": role, "client": client, "username": username}
    session["sid"] = sid
    return sid


def current():
    sid = session.get("sid")
    if not sid:
        return None
    with _sessions_lock:
        return _sessions.get(sid)


def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = current()
            if not ctx or ctx["role"] not in roles:
                return jsonify({"ok": False, "error": "Требуется вход"}), 401
            return fn(ctx, *args, **kwargs)

        return wrapper

    return deco


# --------------------------------------------------------------------------- #
# Background task runner
# --------------------------------------------------------------------------- #
class TaskRunner:
    """Runs a single follow/unfollow job at a time in a background thread."""

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
# Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    if not current():
        return redirect("/login")
    return send_from_directory(STATIC_DIR, "app.html")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.route("/api/auth/account", methods=["POST"])
def auth_account():
    body = request.get_json(force=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "error": "Укажите логин и пароль"}), 400
    try:
        client = IMVUClient(username, password)
        client.login()
        profile = client.get_full_profile()
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _new_session("user", client, username)
    if body.get("remember"):
        settings = load_settings()
        settings["username"] = username
        settings["password"] = password
        save_settings(settings)
    return jsonify({"ok": True, "role": "user", "profile": profile})


@app.route("/api/auth/guest", methods=["POST"])
def auth_guest():
    _new_session("guest", IMVUClient.anonymous())
    return jsonify({"ok": True, "role": "guest"})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    sid = session.pop("sid", None)
    if sid:
        with _sessions_lock:
            _sessions.pop(sid, None)
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def api_me():
    ctx = current()
    if not ctx:
        return jsonify({"ok": False, "role": None}), 200
    out = {"ok": True, "role": ctx["role"], "username": ctx.get("username", "")}
    if ctx["role"] == "user":
        try:
            out["profile"] = ctx["client"].get_full_profile()
        except IMVUError:
            pass
    return jsonify(out)


@app.route("/api/saved-login", methods=["GET"])
def api_saved_login():
    s = load_settings()
    return jsonify({"username": s.get("username", ""), "has_password": bool(s.get("password"))})


# --------------------------------------------------------------------------- #
# Lookup (guest + user)
# --------------------------------------------------------------------------- #
@app.route("/api/lookup", methods=["POST"])
@require_role("guest", "user")
def api_lookup(ctx):
    target = (request.get_json(force=True) or {}).get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "Укажите ник или ID"}), 400
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
        return jsonify(
            {
                "ok": True,
                "user_id": uid,
                "profile": profile,
                "is_following": following,
                "preview": [c.as_dict() for c in page.cards],
            }
        )
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# --------------------------------------------------------------------------- #
# Settings & exceptions (user only)
# --------------------------------------------------------------------------- #
@app.route("/api/settings", methods=["GET"])
@require_role("user")
def get_settings(ctx):
    s = load_settings()
    return jsonify({k: s.get(k, DEFAULT_SETTINGS[k]) for k in JOB_KEYS})


@app.route("/api/settings", methods=["POST"])
@require_role("user")
def post_settings(ctx):
    incoming = request.get_json(force=True) or {}
    settings = load_settings()
    for key in JOB_KEYS:
        if key in incoming and incoming[key] != "":
            settings[key] = incoming[key]
    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/exceptions", methods=["GET"])
@require_role("user")
def get_exceptions(ctx):
    return jsonify({"ok": True, "items": load_settings().get("exceptions", [])})


@app.route("/api/exceptions", methods=["POST"])
@require_role("user")
def add_exception(ctx):
    target = (request.get_json(force=True) or {}).get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "Укажите ник или ID"}), 400
    try:
        uid = ctx["client"].resolve_username(target)
        card = ctx["client"].get_profile_summary(uid)
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    settings = load_settings()
    items = settings.get("exceptions", [])
    if not any(str(e.get("id")) == uid for e in items):
        items.append({"id": uid, "name": card.name, "image": card.image})
        settings["exceptions"] = items
        save_settings(settings)
    return jsonify({"ok": True, "items": items})


@app.route("/api/exceptions/bulk", methods=["POST"])
@require_role("user")
def add_exceptions_bulk(ctx):
    """Add several users to the unfollow whitelist in one call.

    Accepts ``{"items": [{"id", "name", "image"}, ...]}`` — used by the
    "pick from friends" modal so no per-user resolve round-trips are needed.
    """
    incoming = (request.get_json(force=True) or {}).get("items", [])
    settings = load_settings()
    items = settings.get("exceptions", [])
    have = {str(e.get("id")) for e in items}
    added = 0
    for entry in incoming:
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
    return jsonify({"ok": True, "added": added, "items": items})


@app.route("/api/exceptions/<uid>", methods=["DELETE"])
@require_role("user")
def del_exception(ctx, uid):
    settings = load_settings()
    settings["exceptions"] = [
        e for e in settings.get("exceptions", []) if str(e.get("id")) != str(uid)
    ]
    save_settings(settings)
    return jsonify({"ok": True, "items": settings["exceptions"]})


# --------------------------------------------------------------------------- #
# Friends & top subscriptions (user only)
# --------------------------------------------------------------------------- #
@app.route("/api/friends", methods=["GET"])
@require_role("user")
def api_friends(ctx):
    """Return the logged-in account's IMVU friends (for the exceptions picker)."""
    try:
        friends = ctx["client"].get_friends()
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    protected = {str(e.get("id")) for e in load_settings().get("exceptions", [])}
    for f in friends:
        f["protected"] = f["user_id"] in protected
    return jsonify({"ok": True, "items": friends, "total": len(friends)})


@app.route("/api/top-subscriptions", methods=["GET"])
@require_role("user")
def api_top_subscriptions(ctx):
    """Top accounts I follow ranked by their follower count."""
    try:
        n = max(1, min(int(request.args.get("n", 5)), 20))
    except (TypeError, ValueError):
        n = 5
    try:
        max_scan = max(50, min(int(request.args.get("scan", 2000)), 6000))
    except (TypeError, ValueError):
        max_scan = 2000
    try:
        top, scanned = ctx["client"].top_subscriptions(n=n, max_scan=max_scan)
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "items": top, "scanned": scanned})


# --------------------------------------------------------------------------- #
# Analytics (user only)
# --------------------------------------------------------------------------- #
@app.route("/api/analytics", methods=["GET"])
@require_role("user")
def api_analytics(ctx):
    try:
        return jsonify({"ok": True, "stats": ctx["client"].get_relationship_stats()})
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# --------------------------------------------------------------------------- #
# Jobs (user only)
# --------------------------------------------------------------------------- #
def _job_settings(ctx):
    settings = load_settings()
    incoming = request.get_json(force=True) or {}
    for key in JOB_KEYS:
        if key in incoming and incoming[key] not in ("", None):
            settings[key] = incoming[key]
    settings["only_non_followers"] = bool(incoming.get("only_non_followers"))
    save_settings(settings)
    return settings


@app.route("/api/follow", methods=["POST"])
@require_role("user")
def api_follow(ctx):
    settings = _job_settings(ctx)
    if not settings["target_id"]:
        return jsonify({"ok": False, "error": "Укажите цель (ник или ID)"}), 400
    client = ctx["client"]
    total = int(settings.get("max_follows") or 0)
    started = runner.start("follow", lambda: run_follow(client, settings), total=total)
    if not started:
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/api/unfollow", methods=["POST"])
@require_role("user")
def api_unfollow(ctx):
    settings = _job_settings(ctx)
    client = ctx["client"]
    started = runner.start("unfollow", lambda: run_unfollow(client, settings))
    if not started:
        return jsonify({"ok": False, "error": "Задача уже выполняется"}), 409
    return jsonify({"ok": True})


@app.route("/api/follow-one", methods=["POST"])
@require_role("user")
def api_follow_one(ctx):
    target = (request.get_json(force=True) or {}).get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "Не указана цель"}), 400
    try:
        uid = ctx["client"].resolve_username(target)
        code = ctx["client"].follow(uid)
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    ok = code in (200, 201)
    return jsonify({"ok": ok, "code": code, "user_id": uid,
                    "error": None if ok else f"HTTP {code}"})


@app.route("/api/unfollow-one", methods=["POST"])
@require_role("user")
def api_unfollow_one(ctx):
    target = (request.get_json(force=True) or {}).get("target", "")
    if not target:
        return jsonify({"ok": False, "error": "Не указана цель"}), 400
    try:
        uid = ctx["client"].resolve_username(target)
        ok = ctx["client"].unfollow(uid)
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": ok, "user_id": uid,
                    "error": None if ok else "Не удалось отписаться"})


@app.route("/api/stop", methods=["POST"])
@require_role("user")
def api_stop(ctx):
    runner.stop()
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
@require_role("user")
def api_status(ctx):
    return jsonify(runner.snapshot())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
