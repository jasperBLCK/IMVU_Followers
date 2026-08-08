"""SQLite-хранилище настроек и эфира.

Единственный файл базы — ``data.db`` рядом с приложением. При первом запуске
данные автоматически переносятся из старых ``settings.json`` и
``uploads/live/live.json``.
"""

import json
import os
import secrets
import sqlite3
import threading

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "data.db")

_OLD_SETTINGS = os.path.join(BASE_DIR, "settings.json")
_OLD_LIVE = os.path.join(BASE_DIR, "uploads", "live", "live.json")

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    token      TEXT NOT NULL,
    on_air     INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tracks (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    file     TEXT NOT NULL,
    duration REAL NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS request (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    duration   REAL NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0
);
"""


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with _lock, _connect() as con:
        con.executescript(_SCHEMA)
        if con.execute("SELECT 1 FROM request WHERE id = 1").fetchone() is None:
            con.execute("INSERT INTO request (id) VALUES (1)")
        if con.execute("SELECT 1 FROM live WHERE id = 1").fetchone() is None:
            con.execute(
                "INSERT INTO live (id, token, on_air, started_at) VALUES (1, ?, 0, 0)",
                (secrets.token_urlsafe(8),),
            )
        _migrate_json(con)


def _migrate_json(con):
    """Разовый перенос из старых json-файлов (если база ещё пустая)."""
    if os.path.exists(_OLD_SETTINGS) and not con.execute(
        "SELECT 1 FROM settings LIMIT 1"
    ).fetchone():
        try:
            with open(_OLD_SETTINGS, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data.pop("password", None)  # пароль в открытом виде больше не храним
        for k, v in data.items():
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        try:
            os.replace(_OLD_SETTINGS, _OLD_SETTINGS + ".bak")
        except OSError:
            pass
    if os.path.exists(_OLD_LIVE) and not con.execute(
        "SELECT 1 FROM tracks LIMIT 1"
    ).fetchone():
        try:
            with open(_OLD_LIVE, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {}
        if meta.get("token"):
            con.execute(
                "UPDATE live SET token = ?, on_air = ?, started_at = ? WHERE id = 1",
                (meta["token"], 1 if meta.get("on") else 0, meta.get("started_at", 0.0)),
            )
        for pos, t in enumerate(meta.get("tracks", [])):
            con.execute(
                "INSERT OR REPLACE INTO tracks (id, name, file, duration, position)"
                " VALUES (?, ?, ?, ?, ?)",
                (t["id"], t["name"], t["file"], t.get("duration", 0.0), pos),
            )
        try:
            os.replace(_OLD_LIVE, _OLD_LIVE + ".bak")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Настройки (ключ-значение, значения хранятся как JSON)
# --------------------------------------------------------------------------- #
def load_settings(defaults):
    settings = dict(defaults)
    with _lock, _connect() as con:
        for row in con.execute("SELECT key, value FROM settings"):
            if row["key"] in defaults:
                try:
                    settings[row["key"]] = json.loads(row["value"])
                except ValueError:
                    pass
    return settings


def save_settings(settings, defaults):
    with _lock, _connect() as con:
        for key in defaults:
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(settings.get(key, defaults[key]), ensure_ascii=False)),
            )


# --------------------------------------------------------------------------- #
# Эфир
# --------------------------------------------------------------------------- #
def load_live():
    with _lock, _connect() as con:
        row = con.execute("SELECT token, on_air, started_at FROM live WHERE id = 1").fetchone()
        tracks = [
            dict(r)
            for r in con.execute(
                "SELECT id, name, file, duration FROM tracks ORDER BY position"
            )
        ]
    return {
        "token": row["token"],
        "on": bool(row["on_air"]),
        "started_at": row["started_at"],
        "tracks": tracks,
    }


def save_live_state(on=None, started_at=None, token=None):
    sets, args = [], []
    if on is not None:
        sets.append("on_air = ?")
        args.append(1 if on else 0)
    if started_at is not None:
        sets.append("started_at = ?")
        args.append(started_at)
    if token is not None:
        sets.append("token = ?")
        args.append(token)
    if not sets:
        return
    with _lock, _connect() as con:
        con.execute("UPDATE live SET " + ", ".join(sets) + " WHERE id = 1", args)


def load_request():
    with _lock, _connect() as con:
        row = con.execute(
            "SELECT title, url, duration, started_at FROM request WHERE id = 1"
        ).fetchone()
    return dict(row)


def save_request(title, url, duration, started_at):
    with _lock, _connect() as con:
        con.execute(
            "UPDATE request SET title = ?, url = ?, duration = ?, started_at = ?"
            " WHERE id = 1",
            (title, url, duration, started_at),
        )


def clear_request():
    save_request("", "", 0, 0)


def add_track(tid, name, file, duration):
    with _lock, _connect() as con:
        pos = con.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM tracks").fetchone()[0]
        con.execute(
            "INSERT INTO tracks (id, name, file, duration, position) VALUES (?, ?, ?, ?, ?)",
            (tid, name, file, duration, pos),
        )


def delete_track(tid):
    with _lock, _connect() as con:
        row = con.execute("SELECT file FROM tracks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            return None
        con.execute("DELETE FROM tracks WHERE id = ?", (tid,))
        return row["file"]
