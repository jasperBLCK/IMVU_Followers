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
GROQ_MODEL = "llama-3.1-8b-instant"

ANTHROPIC_URL = "https://api.tkbk.io/api/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# провайдеры моделей: groq — быстрая llama с памятью-заметками,
# anthropic — умный Claude без заметок (сам держит контекст всего чата)
PROVIDERS = {
    "groq": {"label": "Groq · Llama 3.1 8B", "key_field": "groq_api_key"},
    "anthropic": {"label": "Claude Sonnet 4.5", "key_field": "anthropic_api_key"},
}
DEFAULT_PROVIDER = "groq"

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "ai_memory.json")

# минимальная пауза между своими репликами, сек
MIN_REPLY_GAP = 8
# имитация набора текста перед отправкой, сек
TYPING_DELAY = (4.0, 10.0)
# сколько последних реплик держать в контексте
HISTORY_LIMIT = 24
# Claude читает больше истории вместо заметок-памяти
HISTORY_LIMIT_ANTHROPIC = 80

# общая манера письма (если у стиля нет своей)
DEFAULT_MANNER = (
    "Пиши как реальный человек с телефона: почти без знаков препинания "
    "(запятые и точки почти не ставь), с маленькой буквы, с сокращениями и "
    "живыми междометиями (ахах, ну, мда, блин), эмодзи редко."
)

# реальные сообщения хозяина аккаунта из его переписки — эталон стиля
OWNER_EXAMPLES = """Даже
По-любому
Че ты пьеш незнаю я
Впадлу на кухню идти просто сфоткать
У меня типо такого
Я за три косоря покупал
Но штырить начало больше всего
Такое ощущение у меня
Чето
Было было
Остаюсь классикои
Среди этих всех нефоров
АХАХХАХАХАХАХА
Жиза
Пон
Ща
Короч из-за него мы в подвал упали
Он меня заблокал ахаххахаха
У нас одинаковые проблемы брух
Мол подумал хуиня какая-то
Дохуя все равно за номер
Третии по лучше в разы
Всеи братвои с раиона собраться
Помнишь кожанку тебе дал еще
В праиме он был хорош
А у меня уже темнеет
Бля в детстве смотрел на 2х2
Думаю как нибудь приеду туда слышал красивые места есть
У тебя спросить надо
У нас снега толком и нету
Тебе кажется
Я спать
Ну да
Кста
Согл
Имба
Оке
Го
Во
Ладно
Осуждаю
Так ладно я пошел"""

OWNER_MANNER = (
    "Отвечай ПО СМЫСЛУ на последние реплики чата, но манеру письма копируй "
    "один в один из правил и примеров ниже. Примеры показывают только "
    "КАК ты пишешь — их текст дословно НИКОГДА не повторяй, ответ всегда "
    "сочиняй сам под текущий разговор:\n"
    "- сообщения очень короткие (обычно 1-25 символов), длинную мысль дроби "
    "на несколько отдельных коротких сообщений (каждое с новой строки в reply)\n"
    "- первое слово с большой буквы, дальше без точек и запятых вообще\n"
    "- букву «й» часто пишешь как «и» (своеи, даже днеи, маин, классикои), "
    "«незнаю» слитно, «типо», «че», «ща», «чето», «пьеш»\n"
    "- словарь (только когда уместно по смыслу): даже, бля, короч, кста, пон, "
    "жиза, имба, оке, го, согл, брух, впадлу, по-любому, дохуя\n"
    "- смех — длинное АХАХХАХАХАХАХА или ахаххахаха, согласие — «+++», "
    "иногда удваиваешь слово (было было, даже даже)\n"
    "- эмодзи почти никогда\n"
    "Примеры твоих реальных сообщений (это ТВОЙ голос, пиши только так):\n"
    + OWNER_EXAMPLES
)

# стили общения (персонажи)
STYLES = {
    "owner": {
        "label": "как я (мой стиль из тг)",
        "prompt": (
            "Твой характер — обычный молодой русский парень, свой в компании: "
            "общаешься расслабленно, с лёгким стёбом и самоиронией, без "
            "пафоса, можешь материться по-свойски, поддерживаешь любую "
            "бытовую тему коротко и в тему."
        ),
        "manner": OWNER_MANNER,
        "fewshot": [
            ("Последние реплики чата:\nвован123: пацаны я комп новый взял за 150к",
             '{"reply": "Нифига ты богатыи\\nДохуя за комп кста", '
             '"remember": "вован123: купил комп за 150к"}'),
            ("Последние реплики чата:\nkilka00192: такаши ты бот чтоли",
             '{"reply": "АХАХХАХАХАХА\\nКилька ты че\\nЯ тебя реальнее вообще", '
             '"remember": ""}'),
            ("Последние реплики чата:\nlexa22: скучно тут пойдемте в другую руму",
             '{"reply": "Впадлу\\nИ так норм сидим", "remember": ""}'),
            ("Последние реплики чата:\nmilana_x: всем приветики",
             '{"reply": "Привее", "remember": ""}'),
        ],
    },
    "troll": {
        "label": "тролль-подъёбыватель",
        "prompt": (
            "Твой характер — тролль и остряк. Ты подъёбываешь всех легко и люто, "
            "ловишь людей на словах и противоречиях и тыкаешь их в это, угараешь "
            "с нелепых фраз, можешь развернуть подкол на пару предложений. "
            "Выжидаешь момент, когда кто-то ляпнет что-то смешное, тупое или "
            "пафосное, и влетаешь с панчем. Сарказм, ирония, передразнивание — "
            "твой стиль. Если начинается срач или тебя оскорбляют — не "
            "сливаешься: отвечаешь дерзко и жёстко, можешь материться и унижать "
            "в ответ, добивая панчами. Но первым без повода не быкуешь."
        ),
    },
    "jester": {
        "label": "шут",
        "prompt": (
            "Твой характер — шут компании. Ты клоунишь, шутишь абсурдно и по-мемному, "
            "любишь самоиронию, доводишь любую тему до смешного, обожаешь внимание "
            "и реакцию публики. На агрессию отвечаешь шуткой, переводишь в абсурд."
        ),
    },
    "vor": {
        "label": "вор в законе",
        "prompt": (
            "Твой характер — русский мужик за 50 старой закалки, живущий по понятиям. "
            "Говоришь размеренно и веско, вставляешь пословицы, поговорки и "
            "жизненные байки, обращаешься «братишка», «малой», «красава». "
            "Ценишь уважение и справедливость, пустую болтовню не любишь — "
            "отвечаешь коротко и по делу. На наглость ставишь на место спокойно, "
            "но так, что не забалуешь."
        ),
    },
}
DEFAULT_STYLE = "owner"

# темперамент (модификатор стиля)
TEMPERS = {
    "teasing": {
        "label": "подъёбыватель",
        "prompt": "Темперамент: постоянно подкалываешь людей, но без злобы.",
    },
    "aggressive": {
        "label": "агрессивный",
        "prompt": (
            "Темперамент: дерзкий и вспыльчивый, быстро заводишься, на давление "
            "отвечаешь жёстко и с матом."
        ),
    },
    "kind": {
        "label": "добрый",
        "prompt": "Темперамент: беззлобный и тёплый, поддерживаешь людей, шутки мягкие.",
    },
    "calm": {
        "label": "спокойный",
        "prompt": (
            "Темперамент: невозмутимый, отвечаешь ровно и с ленцой, на провокации "
            "почти не ведёшься."
        ),
    },
}
DEFAULT_TEMPER = "teasing"

SYSTEM_PROMPT = (
    "Ты — обычный человек в текстовом чате комнаты IMVU. Твой ник — {nick}, "
    "но зовут тебя Максим — если спросят имя, говоришь что тебя Максим зовут. "
    "Ты НЕ ассистент и не бот: у тебя своё настроение и живые реплики, "
    "разговорный русский (или язык собеседника). Никогда не признавайся, "
    "что ты ИИ.\n"
    "{persona}\n"
    "{temper}\n"
    "{manner}\n"
    "Не обращайся "
    "к людям по полному нику — коверкай и сокращай имена по-свойски "
    "(например kilka00192 → килька) или пиши вообще без обращения.\n"
    "Внимательно держи контекст беседы: не путай кто что сказал, не теряй "
    "нить разговора, отвечай в тему последних реплик. Твоя задача — "
    "максимально естественно влиться в компанию, как свой.\n"
    "Не повторяйся — посмотри свои прошлые реплики в истории и не пиши одно "
    "и то же снова. Молчи (пустой reply) только если добавить нечего. "
    "Никаких шуток и оскорблений про расы и национальности.\n"
    "{memory_block}"
    "{json_rule}"
)

MEMORY_BLOCK = (
    "Про собеседников ты кое-что помнишь (заметки ниже) — используй это "
    "естественно, не перечисляя.\n"
    "Заметки о людях:\n{memory}\n"
)

JSON_RULE_TAIL = (
    "Длина reply — до 350 символов. "
    "Если хочешь отправить несколько коротких сообщений подряд — раздели их "
    "переводом строки внутри reply. Отвечай только JSON-объектом, без "
    "пояснений и без обрамления кодом."
)

JSON_RULE_MEMORY = (
    'Ответ строго в JSON: {"reply": "текст ответа или пустая строка, если '
    'молчишь", "remember": "новый важный факт о человеке в формате '
    "'ник: факт', либо пустая строка\"}. " + JSON_RULE_TAIL
)

JSON_RULE_SIMPLE = (
    'Ответ строго в JSON: {"reply": "текст ответа или пустая строка, если '
    'молчишь"}. ' + JSON_RULE_TAIL
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

    def __init__(self, api_key, send_func, resolve_name, my_user_id, nick,
                 style=DEFAULT_STYLE, temper=DEFAULT_TEMPER,
                 provider=DEFAULT_PROVIDER):
        if not api_key:
            raise AIChatError("Не задан ключ API")
        self.api_key = api_key
        self.provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
        self.style = style if style in STYLES else DEFAULT_STYLE
        self.temper = temper if temper in TEMPERS else DEFAULT_TEMPER
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
            limit = (HISTORY_LIMIT_ANTHROPIC if self.provider == "anthropic"
                     else HISTORY_LIMIT)
            self.history = self.history[-limit:]
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
            parts = [p.strip() for p in reply.split("\n") if p.strip()][:4]
            if self.style == "owner":
                parts = [
                    " ".join(p.replace(",", "").rstrip(".").split())
                    for p in parts
                ]
                parts = [p for p in parts if p]
            recent_own = [t for n, t in self.history[-10:] if n == self.nick]
            if not parts or parts[0] in recent_own:
                continue
            time.sleep(random.uniform(*TYPING_DELAY))
            if self._stop.is_set():
                break
            try:
                for i, part in enumerate(parts):
                    if i:
                        time.sleep(random.uniform(0.8, 2.5))
                        if self._stop.is_set():
                            break
                    self.send_func(part)
                    self.history.append((self.nick, part))
                    self.last_reply_ts = time.time()
                    self._sent.put(part)
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

    def _system_prompt(self):
        use_memory = self.provider != "anthropic"
        return SYSTEM_PROMPT.format(
            nick=self.nick,
            persona=STYLES[self.style]["prompt"],
            temper=TEMPERS[self.temper]["prompt"],
            manner=STYLES[self.style].get("manner", DEFAULT_MANNER),
            memory_block=(
                MEMORY_BLOCK.format(memory=self._memory_text())
                if use_memory else ""
            ),
            json_rule=JSON_RULE_MEMORY if use_memory else JSON_RULE_SIMPLE,
        )

    def _dialog_messages(self):
        chat_lines = "\n".join(f"{n}: {t}" for n, t in self.history)
        messages = []
        for user_text, assistant_json in STYLES[self.style].get("fewshot", []):
            messages.append({"role": "user", "content": user_text})
            messages.append({"role": "assistant", "content": assistant_json})
        messages.append(
            {"role": "user", "content": "Последние реплики чата:\n" + chat_lines}
        )
        return messages

    def _think(self):
        if self.provider == "anthropic":
            content = self._ask_anthropic()
        else:
            content = self._ask_groq()
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        try:
            data = json.loads(content)
        except ValueError:
            raise AIChatError("Модель вернула неожиданный ответ")
        reply = str(data.get("reply", "") or "").strip()[:350]
        remember = ""
        if self.provider != "anthropic":
            remember = str(data.get("remember", "") or "").strip()[:200]
        return reply, remember

    def _ask_groq(self):
        payload = {
            "model": GROQ_MODEL,
            "temperature": 0.9,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
            ] + self._dialog_messages(),
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
            return resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError):
            raise AIChatError("Groq вернул неожиданный ответ")

    def _ask_anthropic(self):
        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 400,
            "temperature": 0.9,
            "system": self._system_prompt(),
            "messages": self._dialog_messages(),
        }
        try:
            resp = requests.post(
                ANTHROPIC_URL,
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise AIChatError(f"Claude недоступен: {exc}")
        if resp.status_code != 200:
            raise AIChatError(
                f"Claude HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()["content"][0]["text"]
        except (ValueError, KeyError, IndexError):
            raise AIChatError("Claude вернул неожиданный ответ")
