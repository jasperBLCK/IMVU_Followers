"""IMVU IMQ client — live room chat over WebSocket.

IMVU's realtime layer ("IMQ", the IMVU Message Queue) is a WebSocket protocol
carrying small JSON *records*. This module implements the slice needed to read
and write **live room chat**:

* connect / authenticate,
* subscribe to a room's chat queue,
* receive chat lines (and other queue events),
* send chat lines.

The wire protocol was reverse-engineered from the IMVU Next frontend bundle
(``imqjs/imq.min.js``) and verified live against a real account. See
``docs/imq.md`` for the full protocol notes.

Flow (per the frontend ``ImqManager``)::

    ws = wss://wss-imq.imvu.com/streaming/imvu_pre
    -> msg_c2g_connect        {user_id, cookie: b64(session_id), metadata}
    <- msg_g2c_result         {op_id, status: 0}
    -> msg_c2g_open_floodgates
    -> msg_c2g_subscribe      {queues_with_results: [{record: subscription,
                                                       name: <queue>, op_id}]}
    <- msg_g2c_joined_queue / msg_g2c_create_mount / msg_g2c_result
    -> msg_c2g_send_message   {queue, mount, message: b64(text), op_id}
    <- msg_g2c_send_message   {user_id: b64, queue, mount, message: b64, ...}

``user_id`` is the account's ``legacy_cid`` and the connect ``cookie`` is the
login session id (the ``osCsid`` cookie / login-node id). To send into a room
chat the account must first be a chat *participant* (see
``IMVUClient.join_room_chat``); otherwise sends fail with ``unknown_user``.
"""

import asyncio
import base64
import json
import queue
import ssl
import threading
from dataclasses import dataclass

import websockets

IMQ_URL = "wss://wss-imq.imvu.com/streaming/imvu_pre"

# The frontend pings every 45s; without it the gateway drops the connection.
PING_INTERVAL = 45


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _unb64(text):
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return text or ""


def _chat_id(imq_queue):
    """Numeric chat id used inside the JSON payload (``/chat/123`` -> ``123``)."""
    return str(imq_queue).rstrip("/").rsplit("/", 1)[-1]


def _parse_incoming(raw_text):
    """Extract ``(user_id, text)`` from a room-chat payload.

    Room lines are JSON like ``{"chatId","message","to","userId"}``. Returns
    ``None`` for whispers (``to`` != 0), engine commands (``*imvu:``,
    ``*putOnOutfit``, ``*msg ...``) and empty lines — only human chat is kept.
    """
    user_id = ""
    text = raw_text
    try:
        obj = json.loads(raw_text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        if str(obj.get("to", 0)) not in ("0", "None"):
            return None
        text = obj.get("message", "")
        user_id = str(obj.get("userId", "") or "")
    text = (text or "").strip()
    if not text or text.startswith("*"):
        return None
    return user_id, text


def _build_outgoing(imq_queue, user_id, text):
    """Wrap a chat line in the JSON envelope the room expects."""
    return json.dumps(
        {
            "chatId": _chat_id(imq_queue),
            "message": text,
            "to": 0,
            "userId": str(user_id),
        }
    )


@dataclass
class ChatMessage:
    """A single decoded chat line received from IMQ."""

    user_id: str
    text: str
    queue: str
    mount: str
    sequence: int = 0


class IMQError(Exception):
    """Raised when the IMQ connection or a command fails."""


class IMQClient:
    """Low-level async IMQ WebSocket client.

    Most callers should use :class:`RoomChatSession`, which drives this on a
    background thread and exposes a plain synchronous API.
    """

    def __init__(
        self,
        user_id,
        session_id,
        *,
        url=IMQ_URL,
        app="imvu_next",
        platform="big",
        verify_tls=False,
    ):
        if not user_id or not session_id:
            raise IMQError("IMQ требует user_id и session_id (выполните вход)")
        self.user_id = str(user_id)
        self.session_id = str(session_id)
        self.url = url
        self.app = app
        self.platform = platform
        self.verify_tls = verify_tls
        self._ws = None
        self._op = 0
        self._on_message = None

    def _next_op(self):
        self._op += 1
        return self._op

    def _ssl_context(self):
        ctx = ssl.create_default_context()
        # api.imvu.com needs a relaxed cipher level; mirror that here so the
        # whole app behaves consistently. Documented in docs/imq.md.
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def connect(self, open_timeout=15):
        """Open the socket and perform the connect handshake."""
        self._ws = await websockets.connect(
            self.url, ssl=self._ssl_context(), open_timeout=open_timeout
        )
        await self._send(
            {
                "record": "msg_c2g_connect",
                "user_id": self.user_id,
                "cookie": _b64(self.session_id),
                "metadata": [
                    {"record": "metadata", "key": "app", "value": _b64(self.app)},
                    {
                        "record": "metadata",
                        "key": "platform_type",
                        "value": _b64(self.platform),
                    },
                ],
                "op_id": 0,
            }
        )
        reply = json.loads(await asyncio.wait_for(self._ws.recv(), open_timeout))
        if reply.get("record") != "msg_g2c_result" or reply.get("status") != 0:
            raise IMQError(f"IMQ connect отклонён: {reply}")
        await self._send({"record": "msg_c2g_open_floodgates"})

    async def subscribe(self, queue_name):
        """Subscribe to a queue (e.g. a room's ``imq_queue``)."""
        await self._send(
            {
                "record": "msg_c2g_subscribe",
                "queues_with_results": [
                    {
                        "record": "subscription",
                        "name": queue_name,
                        "op_id": self._next_op(),
                    }
                ],
            }
        )

    async def unsubscribe(self, queue_name):
        await self._send(
            {
                "record": "msg_c2g_unsubscribe",
                "queues_with_results": [
                    {
                        "record": "subscription",
                        "name": queue_name,
                        "op_id": self._next_op(),
                    }
                ],
            }
        )

    async def send_message(self, queue_name, mount, text):
        """Send a chat line into ``queue_name`` at ``mount``."""
        await self._send(
            {
                "record": "msg_c2g_send_message",
                "queue": queue_name,
                "mount": mount,
                "message": _b64(text),
                "op_id": self._next_op(),
            }
        )

    async def ping(self):
        await self._send({"record": "msg_c2g_ping"})

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await self.ping()
            except (IMQError, OSError, websockets.WebSocketException):
                return

    async def run(self, on_message):
        """Read frames until the socket closes, calling ``on_message`` per line.

        ``on_message`` receives a :class:`ChatMessage` for every incoming
        ``msg_g2c_send_message`` record. A keepalive ping runs alongside so the
        gateway doesn't drop the connection.
        """
        ping_task = asyncio.ensure_future(self._ping_loop())
        self._on_message = on_message
        try:
            async for raw in self._ws:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if rec.get("record") == "msg_g2c_send_message":
                    on_message(
                        ChatMessage(
                            user_id=_unb64(rec.get("user_id", "")),
                            text=_unb64(rec.get("message", "")),
                            queue=rec.get("queue", ""),
                            mount=rec.get("mount", ""),
                            sequence=rec.get("sequence", 0) or 0,
                        )
                    )
        finally:
            ping_task.cancel()

    async def close(self):
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _send(self, record):
        if self._ws is None:
            raise IMQError("IMQ не подключён")
        await self._ws.send(json.dumps(record))


class RoomChatSession:
    """Synchronous, thread-backed live chat for a single room.

    Resolves the room chat over REST, joins it as a participant, then keeps an
    IMQ connection open on a background thread. Incoming lines are buffered so a
    (polling) web UI can drain them with :meth:`poll`.
    """

    def __init__(self, imvu_client, room, *, max_buffer=500):
        self.client = imvu_client
        self.room = room
        self.chat = None
        self._inbox = queue.Queue(maxsize=max_buffer)
        self._loop = None
        self._imq = None
        self._thread = None
        self._ready = threading.Event()
        self._error = None
        self._stopping = False
        # optional tap: called with every parsed ChatMessage (from the IMQ thread)
        self.on_line = None

    # -- lifecycle ----------------------------------------------------- #
    def start(self, timeout=20):
        """Resolve + join the room chat and open the IMQ connection."""
        self.chat = self.client.get_room_chat(self.room)
        self.client.join_room_chat(self.chat)
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise IMQError("Не удалось подключиться к IMQ вовремя")
        if self._error:
            raise self._error
        return self.chat

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:  # surfaced to start() / caller
            self._error = exc if isinstance(exc, IMQError) else IMQError(str(exc))
            self._ready.set()
        finally:
            self._loop.close()

    async def _main(self):
        self._imq = IMQClient(self.client.my_user_id, self.client.session_id)
        await self._imq.connect()
        await self._imq.subscribe(self.chat.imq_queue)
        self._ready.set()
        await self._imq.run(self._buffer_message)

    def _buffer_message(self, msg):
        # only surface actual room-chat lines from this room's queue/mount
        if msg.queue != self.chat.imq_queue or msg.mount != self.chat.imq_messages_mount:
            return
        parsed = _parse_incoming(msg.text)
        if parsed is None:
            return
        msg.user_id = parsed[0] or msg.user_id
        msg.text = parsed[1]
        if self.on_line is not None:
            try:
                self.on_line(msg)
            except Exception:
                pass
        try:
            self._inbox.put_nowait(msg)
        except queue.Full:
            try:
                self._inbox.get_nowait()
                self._inbox.put_nowait(msg)
            except queue.Empty:
                pass

    # -- public sync API ----------------------------------------------- #
    def is_alive(self):
        """True while the IMQ connection is still up."""
        return bool(
            self._thread
            and self._thread.is_alive()
            and self._error is None
            and not self._stopping
        )

    def poll(self):
        """Return and clear all buffered incoming :class:`ChatMessage`."""
        out = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    def send(self, text, timeout=10):
        """Send a chat line into the room."""
        text = (text or "").strip()
        if not text:
            return False
        if not self._imq or not self._loop:
            raise IMQError("Сессия чата не запущена")
        payload = _build_outgoing(
            self.chat.imq_queue, self.client.my_user_id, text
        )
        fut = asyncio.run_coroutine_threadsafe(
            self._imq.send_message(
                self.chat.imq_queue, self.chat.imq_messages_mount, payload
            ),
            self._loop,
        )
        fut.result(timeout)
        return True

    def stop(self):
        """Close IMQ and leave the room chat participants."""
        if self._stopping:
            return
        self._stopping = True
        if self._imq and self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._imq.close(), self._loop
                ).result(5)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        if self.chat:
            try:
                self.client.leave_room_chat(self.chat)
            except Exception:
                pass
