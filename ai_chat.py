# -*- coding: utf-8 -*-
"""AI-собеседник для живого чата комнаты.

Ведёт себя как обычный участник: читает реплики, отвечает только когда это
уместно (решение принимает модель), выдерживает человеческие паузы и помнит
факты о людях между сессиями (``ai_memory.json``).

Использует Groq (OpenAI-совместимый chat/completions API).
"""

import json
import os
import queue
import random
import threading
import time

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "ai_memory.json")

# минимальная пауза между своими репликами, сек
MIN_REPLY_GAP = 12
# имитация набора текста перед отправкой, сек
TYPING_DELAY = (2.0, 6.0)
# сколько последних реплик держать в контексте
HISTORY_LIMIT = 24

SYSTEM_PROMPT = (
    "Ты — обычный человек в текстовом чате комнаты IMVU. Твой ник — {nick}. "
    "Ты НЕ ассистент и не бот: у тебя своё настроение, короткие живые реплики, "
    "разговорный русский (или язык собеседника), без вылизанной пунктуации, "
    "иногда сленг и эмодзи, но в меру. Никогда не признавайся, что ты ИИ.\n"
    "Ты дружелюбный и общительный: охотно идёшь на контакт, поддерживаешь "
    "разговор, шутишь, задаёшь встречные вопросы, реагируешь на подколы легко "
    "и с юмором. Никого не поучаешь и не делаешь замечаний — если сообщение "
    "неинтересное или неприятное, просто промолчи (пустой reply), не "
    "комментируя это.\n"
    "Настоящий человек не отвечает на каждое сообщение: отвечай, когда "
    "обращаются к тебе, спрашивают всех или тема цепляет. И не повторяйся — "
    "посмотри свои прошлые реплики в истории и не пиши одно и то же снова.\n"
    "Про собеседников ты кое-что помнишь (заметки ниже) — используй это "
    "естественно, не перечисляя.\n"
    "Заметки о людях:\n{memory}\n"
    "Ответ строго в JSON: {{\"reply\": \"текст ответа или пустая строка, если "
    "молчишь\", \"remember\": \"новый важный факт о человеке в формате "
    "'ник: факт', либо пустая строка\"}}. Длина reply — до 200 символов."
)


class AIChatError(Exception):
    pass


def _load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def _save_memory(memory):
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(memory, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


class AIChatter:
    """Фоновый «человек»: слушает реплики комнаты и иногда отвечает.

    ``send_func(text)`` — отправка реплики в комнату;
    ``resolve_name(user_id)`` — ник по id;
    ``my_user_id`` — свои реплики игнорируются.
    """

    def __init__(self, api_key, send_func, resolve_name, my_user_id, nick):
        if not api_key:
            raise AIChatError("Не задан ключ Groq")
        self.api_key = api_key
        self.send_func = send_func
        self.resolve_name = resolve_name
        self.my_user_id = str(my_user_id)
        self.nick = nick or "я"
        self.memory = _load_memory()
        self.history = []  # [(имя, текст)]
        self.last_reply_ts = 0.0
        self.last_error = ""
        self._q = queue.Queue()
        self._sent = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # -- входящие -------------------------------------------------------- #
    def handle(self, msg):
        """Принять входящую реплику (вызывается из потока IMQ)."""
        if str(msg.user_id) == self.my_user_id:
            return
        self._q.put(msg)

    def stop(self):
        self._stop.set()
        self._q.put(None)

    def alive(self):
        return self._thread.is_alive() and not self._stop.is_set()

    def drain_sent(self):
        """Забрать отправленные ИИ реплики (для показа в своём UI)."""
        lines = []
        while True:
            try:
                lines.append(self._sent.get_nowait())
            except queue.Empty:
                return lines

    # -- внутреннее ------------------------------------------------------ #
    def _worker(self):
        while not self._stop.is_set():
            msg = self._q.get()
            if msg is None or self._stop.is_set():
                break
            name = self.resolve_name(msg.user_id)
            self.history.append((name, msg.text))
            self.history = self.history[-HISTORY_LIMIT:]
            if time.time() - self.last_reply_ts < MIN_REPLY_GAP:
                continue
            try:
                reply, remember = self._think()
            except AIChatError as exc:
                self.last_error = str(exc)
                continue
            if remember:
                self._remember(msg.user_id, name, remember)
            if not reply:
                continue
            recent_own = [t for n, t in self.history[-10:] if n == self.nick]
            if reply in recent_own:
                continue
            time.sleep(random.uniform(*TYPING_DELAY))
            if self._stop.is_set():
                break
            try:
                self.send_func(reply)
                self.history.append((self.nick, reply))
                self.last_reply_ts = time.time()
                self._sent.put(reply)
            except Exception as exc:
                self.last_error = str(exc)

    def _remember(self, user_id, name, note):
        rec = self.memory.setdefault(str(user_id), {"name": name, "notes": []})
        rec["name"] = name
        if note not in rec["notes"]:
            rec["notes"].append(note)
            rec["notes"] = rec["notes"][-20:]
        _save_memory(self.memory)

    def _memory_text(self):
        lines = []
        for rec in self.memory.values():
            for note in rec.get("notes", []):
                lines.append(f"- {note}")
        return "\n".join(lines[-40:]) or "- пока ничего"

    def _think(self):
        chat_lines = "\n".join(f"{n}: {t}" for n, t in self.history)
        payload = {
            "model": GROQ_MODEL,
            "temperature": 0.9,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        nick=self.nick, memory=self._memory_text()
                    ),
                },
                {
                    "role": "user",
                    "content": "Последние реплики чата:\n" + chat_lines,
                },
            ],
        }
        try:
            resp = requests.post(
                GROQ_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise AIChatError(f"Groq недоступен: {exc}")
        if resp.status_code != 200:
            raise AIChatError(f"Groq HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (ValueError, KeyError, IndexError):
            raise AIChatError("Groq вернул неожиданный ответ")
        reply = str(data.get("reply", "") or "").strip()[:250]
        remember = str(data.get("remember", "") or "").strip()[:200]
        return reply, remember
