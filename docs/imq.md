# IMVU IMQ — живой чат комнаты (WebSocket)

REST API (`api.imvu.com`) отвечает за профили, подписки, инвентарь и т.п.
Живой чат в 3D-комнате идёт **не через REST**, а через отдельный realtime-слой —
**IMQ** (IMVU Message Queue): постоянное WebSocket-соединение с JSON-«записями»
(records).

Этот документ описывает протокол в объёме, необходимом для **чтения и отправки
реплик комнаты**. Протокол реверс-инжинирился из фронтенд-бандла IMVU Next
(`imqjs/imq.min.js`, `withme/withme.min.js`) и проверялся вживую на реальном
аккаунте. Реализация — <code>imq_client.py</code>, REST-часть входа в чат —
методы `IMVUClient.get_room_chat / join_room_chat / leave_room_chat`.

Легенда: **[verified]** — подтверждено живым трафиком; **[from bundle]** —
выведено из фронтенд-кода; **[note]** — ограничение/замечание.

---

## 1. REST: от комнаты к очереди чата

Каждая комната (`/room/room-<owner>-<n>`) имеет relation **`chat`**, ведущий на
запись `/chat/chat-<owner>-<n>`. В её `data` есть поля очереди IMQ: **[verified]**

```jsonc
GET https://api.imvu.com/chat/chat-357036039-1
{
  "activity": "publicroom-357036039-1",
  "capacity": 9999,
  "imq_queue": "/chat/560603123",     // имя очереди комнаты в IMQ
  "imq_messages_mount": "messages",   // mount, куда шлются/откуда читаются реплики
  "last_updated": "..."
}
```

* `imq_queue` — **динамический**: у инстанса комнаты он меняется, не хардкодить.
  Всегда брать из свежего `GET /chat/...`.
* relations чата: `room`, `participants`, …

### Вход в чат как участник — обязателен для отправки

Чтобы шлюз IMQ принимал ваши сообщения в очередь комнаты, аккаунт должен быть
её **участником**. Иначе отправка отклоняется: `msg_g2c_result status=1
error_message="unknown_user"`. **[verified]**

```jsonc
POST https://api.imvu.com/chat/chat-357036039-1/participants
{ "id": "https://api.imvu.com/user/user-357036039" }
-> 201 { "status": "success" }
```

Выход: `DELETE .../participants/user-<id>`.

---

## 2. Транспорт IMQ

* URL: `wss://wss-imq.imvu.com/streaming/imvu_pre` **[verified]**
  (фронт получает его в рантайме; в проекте задан константой `IMQ_URL`).
* Протокол: WebSocket, текстовые кадры — по одной JSON-`record` на кадр.
* TLS: как и `api.imvu.com`, шлюз капризен к современным дефолтам — клиент
  использует `SECLEVEL=1`. Проверка сертификата по умолчанию отключена
  (`verify_tls=False`), это осознанно и совпадает с поведением REST-клиента. **[note]**
* Аутентификация: в connect-кадре передаётся `user_id` (это `legacy_cid`
  аккаунта) и `cookie` = base64(session_id), где session_id — id узла логина
  (кука `osCsid`). Отдельного sauce в IMQ нет. **[verified]**

---

## 3. Порядок кадров

```
-> msg_c2g_connect          connect + auth
<- msg_g2c_result {op_id:0, status:0}
-> msg_c2g_open_floodgates  «открыть шлюзы» — начать доставку событий
-> msg_c2g_subscribe        подписка на imq_queue комнаты
<- msg_g2c_joined_queue     присоединился к очереди
<- msg_g2c_create_mount     mounts очереди: control / messages / participants
<- msg_g2c_result {op_id:<sub>, status:0}
-> msg_c2g_send_message     отправка реплики
<- msg_g2c_result {op_id:<send>, status:0}
<- msg_g2c_send_message     входящие реплики (свои и чужие)
```

### connect **[verified]**

```jsonc
{
  "record": "msg_c2g_connect",
  "user_id": "357036039",
  "cookie": "<base64(session_id)>",
  "metadata": [
    { "record": "metadata", "key": "app",           "value": "<base64('imvu_next')>" },
    { "record": "metadata", "key": "platform_type", "value": "<base64('big')>" }
  ],
  "op_id": 0
}
```
Успех: `{"record":"msg_g2c_result","op_id":0,"status":0}`.

### subscribe **[verified]**

```jsonc
{
  "record": "msg_c2g_subscribe",
  "queues_with_results": [
    { "record": "subscription", "name": "/chat/560603123", "op_id": 1 }
  ]
}
```
В ответ приходят `msg_g2c_joined_queue`, `msg_g2c_create_mount` (для mounts
`control`, `messages`, `participants`) и `msg_g2c_result{status:0}`.

### send_message **[verified]** (from bundle: кодировщик в `imq.min.js`)

```jsonc
{
  "record": "msg_c2g_send_message",
  "queue": "/chat/560603123",
  "mount": "messages",
  "message": "<base64(текст)>",
  "op_id": 2
}
```
Успех: `msg_g2c_result{op_id:2, status:0}`. Ошибка: `status!=0` +
`error_message` (например `unknown_user`, если не вошли участником).

### входящая реплика **[verified: envelope]**

```jsonc
{
  "record": "msg_g2c_send_message",
  "user_id": "<base64(id отправителя)>",
  "queue":   "/chat/560603123",
  "mount":   "messages",
  "message": "<base64(текст)>",
  "sequence": 57155
}
```
Фильтр реплик комнаты: `record == msg_g2c_send_message`, `queue == imq_queue`,
`mount == imq_messages_mount`. `user_id` и `message` — base64.

**[note]** IMVU **не** возвращает отправителю его собственную реплику эхом —
свои сообщения UI показывает локально. Чужие реплики видны, когда в комнате
говорит другой аватар.

---

## 4. Лимиты и заметки

* Длина: обычный чат `chat_message_max_length = 1000`, live-комнаты (сцена/
  аудитория) `live_*_message_max_length = 250/1000` (из `GET /startup/startup-alloy`).
* Спец-действия/жесты идут той же командой с префиксом `*imvu:` в тексте
  (напр. `*imvu:showGift`) — здесь не реализовано, только обычный текст. **[from bundle]**
* «Войти в 3D-комнату» на самом сайте запускает нативный клиент
  (`applaunch`); полноценной 3D-сцены тут нет — только текстовый слой чата. **[from bundle]**
* Очередь привязана к сессии/инстансу — при реконнекте заново получить
  `imq_queue` из REST и переподписаться.

---

## 5. Как это в проекте

* `imvu_client.py`: `get_room_chat(room)` → `ChatInfo(imq_queue, imq_messages_mount, …)`;
  `join_room_chat(chat)` / `leave_room_chat(chat)`.
* `imq_client.py`:
  * `IMQClient` — низкоуровневый async-клиент (connect/subscribe/send/run/close);
  * `RoomChatSession` — синхронная обёртка на фоновом потоке: `start()` (resolve +
    join + connect + subscribe), `poll()` (забрать входящие), `send(text)`, `stop()`.
* `app.py`: роуты `POST /api/room/join`, `GET /api/room/messages`,
  `POST /api/room/send`, `POST /api/room/leave`; в UI — карточка «чат комнаты · live».
