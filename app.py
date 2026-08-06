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

import base64
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
from imvu_client import IMVUClient, IMVUError, TwoFactorRequired
from imq_client import IMQError, RoomChatSession

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
    "groq_api_key": "",
    "anthropic_api_key": "",
    "ai_provider": DEFAULT_PROVIDER,
    "ai_style": DEFAULT_STYLE,
    "ai_temper": DEFAULT_TEMPER,
    "recent_rooms": [],  # [{"room": "room-1-2", "name": "..."}]
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

# Logins waiting for the email security code: pending_id -> login context
_pending_2fa = {}


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


@app.route("/radio")
def radio_page():
    # radio.html uses relative asset paths so the same page works on GitHub Pages
    return redirect("/static/radio.html")


@app.route("/live/<token>")
def live_page(token):
    return send_from_directory(STATIC_DIR, "live.html")


# --------------------------------------------------------------------------- #
# Live broadcast ("эфир"): владелец грузит треки, слушатели ловят их по
# уникальной ссылке синхронно, как настоящее радио.
# --------------------------------------------------------------------------- #
LIVE_DIR = os.path.join(os.path.dirname(__file__), "uploads", "live")
LIVE_META = os.path.join(LIVE_DIR, "live.json")
# для непрерывного mp3-эфира треки должны быть в mp3
ALLOWED_AUDIO_EXT = {".mp3"}
_live_lock = threading.Lock()


def _load_live():
    if os.path.exists(LIVE_META):
        try:
            with open(LIVE_META, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {"token": secrets.token_urlsafe(8), "on": False, "started_at": 0.0, "tracks": []}


def _save_live(meta):
    os.makedirs(LIVE_DIR, exist_ok=True)
    with open(LIVE_META, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, ensure_ascii=False)


def _live_now(meta):
    """Что играет прямо сейчас: индекс трека и смещение в секундах."""
    tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
    total = sum(t["duration"] for t in tracks)
    if not meta["on"] or not tracks or total <= 0:
        return None
    pos = (time.time() - meta["started_at"]) % total
    for i, t in enumerate(tracks):
        if pos < t["duration"]:
            return {"index": i, "offset": round(pos, 3)}
        pos -= t["duration"]
    return {"index": 0, "offset": 0.0}


def _live_public(meta):
    tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
    return {
        "on": meta["on"],
        "started_at": meta["started_at"],
        "server_time": time.time(),
        "tracks": [{"id": t["id"], "name": t["name"], "duration": t["duration"]} for t in tracks],
        "now": _live_now(meta),
    }


@app.route("/api/live", methods=["GET"])
@require_role("user")
def live_state(ctx):
    with _live_lock:
        meta = _load_live()
    out = _live_public(meta)
    out.update({"ok": True, "token": meta["token"]})
    return jsonify(out)


@app.route("/api/live/upload", methods=["POST"])
@require_role("user")
def live_upload(ctx):
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "Нет файлов"}), 400
    os.makedirs(LIVE_DIR, exist_ok=True)
    with _live_lock:
        meta = _load_live()
        added = 0
        rejected = []
        for f in files:
            name = os.path.basename(f.filename or "")
            ext = os.path.splitext(name)[1].lower()
            if ext not in ALLOWED_AUDIO_EXT:
                rejected.append(name + " — нужен mp3")
                continue
            if not _is_real_mp3(f.stream):
                rejected.append(name + " — внутри не mp3 (переименованный файл?), сконвертируй в настоящий mp3")
                continue
            tid = secrets.token_hex(8)
            path = os.path.join(LIVE_DIR, tid + ext)
            f.save(path)
            duration = _mp3_duration(path)
            if not duration:
                os.remove(path)
                rejected.append(name + " — не определилась длительность")
                continue
            meta["tracks"].append(
                {
                    "id": tid,
                    "name": os.path.splitext(name)[0],
                    "file": tid + ext,
                    "duration": round(duration, 3),
                }
            )
            added += 1
        _save_live(meta)
    if not added:
        return jsonify({"ok": False, "error": "; ".join(rejected) or "Нужны mp3-файлы"}), 400
    return jsonify({"ok": True, "added": added, "rejected": rejected})


@app.route("/api/live/track/<tid>", methods=["DELETE"])
@require_role("user")
def live_delete_track(ctx, tid):
    with _live_lock:
        meta = _load_live()
        track = next((t for t in meta["tracks"] if t["id"] == tid), None)
        if not track:
            return jsonify({"ok": False, "error": "Трек не найден"}), 404
        meta["tracks"] = [t for t in meta["tracks"] if t["id"] != tid]
        _save_live(meta)
    try:
        os.remove(os.path.join(LIVE_DIR, track["file"]))
    except OSError:
        pass
    return jsonify({"ok": True})


@app.route("/api/live/toggle", methods=["POST"])
@require_role("user")
def live_toggle(ctx):
    on = bool((request.get_json(silent=True) or {}).get("on"))
    with _live_lock:
        meta = _load_live()
        if on and not meta["tracks"]:
            return jsonify({"ok": False, "error": "Сначала загрузи треки"}), 400
        meta["on"] = on
        if on:
            meta["started_at"] = time.time()
        _save_live(meta)
    return jsonify({"ok": True, "on": on})


@app.route("/api/live/regen", methods=["POST"])
@require_role("user")
def live_regen(ctx):
    with _live_lock:
        meta = _load_live()
        meta["token"] = secrets.token_urlsafe(8)
        _save_live(meta)
    return jsonify({"ok": True, "token": meta["token"]})


def _check_live_token(token):
    with _live_lock:
        meta = _load_live()
    if not secrets.compare_digest(token, meta["token"]):
        return None
    return meta


@app.route("/api/live/<token>/now", methods=["GET"])
def live_now_public(token):
    meta = _check_live_token(token)
    if meta is None:
        return jsonify({"ok": False, "error": "Эфир не найден"}), 404
    out = _live_public(meta)
    out["ok"] = True
    return jsonify(out)


@app.route("/api/live/<token>/audio/<tid>", methods=["GET"])
def live_audio(token, tid):
    meta = _check_live_token(token)
    if meta is None:
        return jsonify({"ok": False, "error": "Эфир не найден"}), 404
    track = next((t for t in meta["tracks"] if t["id"] == tid), None)
    if not track:
        return jsonify({"ok": False, "error": "Трек не найден"}), 404
    return send_from_directory(LIVE_DIR, track["file"], conditional=True)


# ~0.13 c тишины (mp3, 128 kbps, 44.1 кГц стерео) — шлётся, когда эфир выключен,
# чтобы плеер не обрывал соединение и звук появлялся сразу после включения
_SILENT_MP3 = base64.b64decode(
    "//uQZAAP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVV//uSZECP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAEVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7kmRAj/AAAGkAAAAIAAANIAAA"
    "AQAAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/"
    "+5JkQI/wAABpAAAACAAADSAAAAEAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVV//uSZECP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAEVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVQ=="
)
_SILENT_SECS = 0.13


# битрейты (кбит/с) для MPEG1 / MPEG2(2.5) Layer III
_MP3_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_len(data, i):
    """Длина валидного mp3-фрейма (Layer III) с позиции i, иначе 0."""
    if i + 4 > len(data) or data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
        return 0
    version = (data[i + 1] >> 3) & 0x03   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer = (data[i + 1] >> 1) & 0x03     # 1 = Layer III
    if version == 1 or layer != 1:
        return 0
    br_idx = (data[i + 2] >> 4) & 0x0F
    sr_idx = (data[i + 2] >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return 0
    bitrate = (_MP3_BITRATES_V1 if version == 3 else _MP3_BITRATES_V2)[br_idx] * 1000
    rate = _MP3_RATES[version][sr_idx]
    padding = (data[i + 2] >> 1) & 0x01
    samples = 1152 if version == 3 else 576
    return samples * bitrate // (8 * rate) + padding


def _mp3_frame_align(data):
    """Сдвиг до начала настоящего mp3-фрейма: заголовок валиден и за фреймом
    следует ещё один валидный заголовок (защита от ложного sync в аудиоданных)."""
    for i in range(len(data) - 4):
        ln = _mp3_frame_len(data, i)
        if not ln:
            continue
        j = i + ln
        if j + 4 > len(data) or _mp3_frame_len(data, j):
            return data[i:]
    return data


def _is_real_mp3(fh):
    """Проверка содержимого: это настоящий mp3, а не переименованный m4a/webm.
    Пропускает ID3-тег и ищет два подряд валидных заголовка фрейма."""
    head = fh.read(10)
    skip = 0
    if head[:3] == b"ID3" and len(head) == 10:
        skip = (
            (head[6] & 0x7F) << 21
            | (head[7] & 0x7F) << 14
            | (head[8] & 0x7F) << 7
            | (head[9] & 0x7F)
        ) + 10
        fh.seek(skip)
        data = fh.read(65536)
    else:
        data = head + fh.read(65536)
    fh.seek(0)
    for i in range(len(data) - 4):
        ln = _mp3_frame_len(data, i)
        if ln and (i + ln + 4 > len(data) or _mp3_frame_len(data, i + ln)):
            return True
    return False


def _mp3_duration(path):
    """Длительность mp3 в секундах: проход по фреймам (работает и для VBR)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0.0
    i = 0
    if data[:3] == b"ID3" and len(data) > 10:
        i = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        ) + 10
    seconds = 0.0
    n = len(data)
    while i < n - 4:
        ln = _mp3_frame_len(data, i)
        if not ln:
            i += 1  # ресинк после мусора между фреймами
            continue
        version = (data[i + 1] >> 3) & 0x03
        sr_idx = (data[i + 2] >> 2) & 0x03
        rate = _MP3_RATES[version][sr_idx]
        seconds += (1152 if version == 3 else 576) / rate
        i += ln
    return seconds


def _live_stream_generator():
    """Бесконечный mp3-поток: отдаёт треки плейлиста с текущего места эфира,
    как Icecast-радио — все слушатели получают один и тот же момент."""
    chunk_size = 16384
    buffer_ahead = 10.0   # секунд аудио вперёд в устоявшемся режиме
    initial_burst = 20.0  # стартовый запас при подключении, чтобы плеер не лагал
    lead = initial_burst
    while True:
        with _live_lock:
            meta = _load_live()
        now = _live_now(meta)
        if now is None:
            # эфир выключен или пуст — держим слушателя тишиной
            yield _SILENT_MP3
            time.sleep(_SILENT_SECS)
            continue
        tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
        track = tracks[now["index"]]
        path = os.path.join(LIVE_DIR, track["file"])
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(1.0)
            continue
        bps = size / track["duration"]  # байт в секунду (усреднённый битрейт)
        start = int(now["offset"] * bps)
        t0 = time.time()
        # момент, когда этот трек закончится в эфире
        track_ends_at = t0 + max(track["duration"] - now["offset"], 0.0)
        sent = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(min(start, max(size - 1, 0)))
                first = True
                while True:
                    data = fh.read(chunk_size)
                    if not data:
                        break
                    if first:
                        data = _mp3_frame_align(data)
                        first = False
                    yield data
                    sent += len(data)
                    ahead = sent / bps - (time.time() - t0) - lead
                    if ahead > 0:
                        lead = buffer_ahead  # стартовый запас отдан
                        time.sleep(min(ahead, 1.0))
        except OSError:
            time.sleep(1.0)
            continue
        # файл отдан раньше конца трека (буфер) — ждём, пока эфир перейдёт
        # на следующий трек (небольшой запас против зацикливания на стыке)
        time.sleep(max(track_ends_at - time.time() + 0.05, 0.05))


@app.route("/live/<token>/stream", methods=["GET"])
def live_stream(token):
    meta = _check_live_token(token)
    if meta is None:
        return jsonify({"ok": False, "error": "Эфир не найден"}), 404
    resp = app.response_class(_live_stream_generator(), mimetype="audio/mpeg")
    resp.headers["Cache-Control"] = "no-cache, no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Accept-Ranges"] = "none"
    resp.headers["icy-name"] = "IMVU_NET live"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


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
    except TwoFactorRequired as exc:
        pending_id = secrets.token_hex(16)
        _pending_2fa[pending_id] = {
            "client": client,
            "username": username,
            "password": password,
            "remember": bool(body.get("remember")),
        }
        session["pending_2fa"] = pending_id
        return jsonify(
            {"ok": False, "need_2fa": True, "error": str(exc), "email": exc.email}
        )
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _new_session("user", client, username)
    if body.get("remember"):
        settings = load_settings()
        settings["username"] = username
        settings["password"] = password
        save_settings(settings)
    return jsonify({"ok": True, "role": "user", "profile": profile})


@app.route("/api/auth/2fa/resend", methods=["POST"])
def auth_2fa_resend():
    """Trigger IMVU to email a fresh security code (a login attempt sends one)."""
    pending_id = session.get("pending_2fa")
    pending = _pending_2fa.get(pending_id)
    if not pending:
        return jsonify(
            {"ok": False, "error": "Сессия входа истекла — войдите заново", "restart": True}
        ), 400
    client = pending["client"]
    try:
        client.login()
    except TwoFactorRequired as exc:
        return jsonify({"ok": True, "resent": True, "email": exc.email})
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    # No challenge this time — the login just succeeded.
    profile = client.get_full_profile()
    _pending_2fa.pop(pending_id, None)
    session.pop("pending_2fa", None)
    _new_session("user", client, pending["username"])
    if pending["remember"]:
        settings = load_settings()
        settings["username"] = pending["username"]
        settings["password"] = pending["password"]
        save_settings(settings)
    return jsonify({"ok": True, "role": "user", "profile": profile})


@app.route("/api/auth/2fa", methods=["POST"])
def auth_2fa():
    body = request.get_json(force=True) or {}
    code = (str(body.get("code") or "")).strip()
    if not code:
        return jsonify({"ok": False, "error": "Введите код из письма"}), 400
    pending_id = session.get("pending_2fa")
    pending = _pending_2fa.get(pending_id)
    if not pending:
        return jsonify(
            {"ok": False, "error": "Сессия входа истекла — войдите заново", "restart": True}
        ), 400
    client = pending["client"]
    try:
        client.login(code=code)
        profile = client.get_full_profile()
    except TwoFactorRequired:
        return jsonify({"ok": False, "need_2fa": True, "error": "Неверный код — попробуйте ещё раз"}), 400
    except IMVUError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    _pending_2fa.pop(pending_id, None)
    session.pop("pending_2fa", None)
    _new_session("user", client, pending["username"])
    if pending["remember"]:
        settings = load_settings()
        settings["username"] = pending["username"]
        settings["password"] = pending["password"]
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


# --------------------------------------------------------------------------- #
# Live room chat (IMQ)
# --------------------------------------------------------------------------- #
def _chat_name(ctx, cid):
    """Resolve a numeric user id to a display name, cached per web session."""
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


@app.route("/api/room/join", methods=["POST"])
@require_role("user")
def api_room_join(ctx):
    room = (request.get_json(force=True) or {}).get("room", "").strip()
    if not room:
        return jsonify({"ok": False, "error": "Укажите комнату (room-<id> или ссылку)"}), 400
    _stop_ai(ctx)
    old = ctx.pop("chat", None)
    if old:
        old.stop()
    chat = RoomChatSession(ctx["client"], room)
    try:
        info = chat.start()
    except (IMQError, IMVUError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    ctx["chat"] = chat
    recent = _remember_room(info.room_id, info.name)
    return jsonify(
        {
            "ok": True,
            "room_id": info.room_id,
            "name": info.name,
            "occupancy": info.occupancy,
            "capacity": info.capacity,
            "recent": recent,
        }
    )


@app.route("/api/room/recent", methods=["GET"])
@require_role("user")
def api_room_recent(ctx):
    settings = load_settings()
    return jsonify(
        {
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
    )


@app.route("/api/room/messages", methods=["GET"])
@require_role("user")
def api_room_messages(ctx):
    chat = ctx.get("chat")
    if not chat:
        return jsonify({"ok": False, "error": "Вы не в комнате"}), 400
    # own lines are rendered locally by the UI, so drop their echo here
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
        # AI replies go out under our own account, so their IMQ echo is
        # filtered above — surface them explicitly instead
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
    return jsonify(
        {
            "ok": True,
            "messages": msgs,
            "connected": chat.is_alive(),
            "ai": bool(ai and ai.alive()),
            "ai_error": ai.last_error if ai else "",
        }
    )


@app.route("/api/room/send", methods=["POST"])
@require_role("user")
def api_room_send(ctx):
    chat = ctx.get("chat")
    if not chat:
        return jsonify({"ok": False, "error": "Вы не в комнате"}), 400
    text = (request.get_json(force=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400
    try:
        chat.send(text)
    except IMQError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/room/leave", methods=["POST"])
@require_role("user")
def api_room_leave(ctx):
    _stop_ai(ctx)
    chat = ctx.pop("chat", None)
    if chat:
        chat.stop()
    return jsonify({"ok": True})


@app.route("/api/room/ai", methods=["POST"])
@require_role("user")
def api_room_ai(ctx):
    body = request.get_json(force=True) or {}
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
        return jsonify({"ok": True, "ai": False})
    chat = ctx.get("chat")
    if not chat or not chat.is_alive():
        return jsonify({"ok": False, "error": "Сначала войдите в комнату"}), 400
    api_key = settings.get(PROVIDERS[active]["key_field"], "")
    if not api_key:
        return jsonify(
            {"ok": False,
             "error": f"Добавьте ключ для {PROVIDERS[active]['label']}"}
        ), 400
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
        return jsonify({"ok": False, "error": str(exc)}), 400
    chat.on_line = ai.handle
    ctx["ai"] = ai
    return jsonify({"ok": True, "ai": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
