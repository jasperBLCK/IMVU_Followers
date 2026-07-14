"""Reusable IMVU API client for follow / unfollow automation.

This module extracts the core logic that used to live inside the standalone
``imvu_follow.py`` and ``imvu_unfollow.py`` scripts so it can be reused by the
web manager (``app.py``) as well as from the command line.

Credentials are NOT hardcoded here — they are passed in by the caller (the web
UI form or environment variables), so nothing sensitive lives in the source.

The client is a thin, well-behaved wrapper around the public ``api.imvu.com``
endpoints. It focuses on three things the old scripts handled poorly:

* **Resilience** — network errors, timeouts and rate limits are retried with
  exponential backoff instead of a fixed ``sleep(10)``.
* **Rich data** — list endpoints are denormalized into friendly user cards
  (display name, avatar, follower count, follow date) instead of bare ids.
* **Discoverability** — extra helpers such as :meth:`resolve_username`,
  :meth:`get_profile_summary` and :meth:`is_following` make the API easy to
  drive from a UI.
"""

import re
import ssl
import time
from dataclasses import dataclass, field

import requests
import urllib3
from requests.adapters import HTTPAdapter

urllib3.disable_warnings()

BASE = "https://api.imvu.com"

_USER_ID_RE = re.compile(r"user-(\d+)")
_TRAILING_ID_RE = re.compile(r"/(\d+)$")
_BEFORE_RE = re.compile(r"before=(\d+)")


class TLSAdapter(HTTPAdapter):
    """IMVU's API rejects modern TLS defaults, so relax the security level."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class IMVUError(Exception):
    """Raised when an IMVU API operation fails in a non-recoverable way."""


class TwoFactorRequired(IMVUError):
    """Raised when IMVU asks for a security code sent to the account email."""


@dataclass
class UserCard:
    """A friendly summary of an IMVU user, built from denormalized records."""

    user_id: str
    name: str = ""
    avatar_name: str = ""
    image: str = ""
    followers: int = 0
    following: int = 0
    online: bool = False
    date_added: str = ""

    @property
    def profile_url(self):
        return f"https://www.imvu.com/next/av/{self.avatar_name}/" if self.avatar_name else ""

    def as_dict(self):
        return {
            "user_id": self.user_id,
            "name": self.name or self.avatar_name or f"user-{self.user_id}",
            "avatar_name": self.avatar_name,
            "image": self.image,
            "followers": self.followers,
            "following": self.following,
            "online": self.online,
            "date_added": self.date_added,
            "profile_url": self.profile_url,
        }


@dataclass
class Page:
    """One page of a paginated list endpoint."""

    cards: list = field(default_factory=list)
    total_count: int = 0
    next_before: str = None


def _id_from_url(url):
    matches = _USER_ID_RE.findall(url or "")
    return matches[-1] if matches else None


class IMVUClient:
    """Thin wrapper around the IMVU profile API used for follow automation."""

    def __init__(self, username, password, timeout=15, base=BASE):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base = base.rstrip("/")
        self.my_user_id = None
        self.session = requests.Session()
        self.session.mount("https://", TLSAdapter())
        self.session.verify = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json; charset=utf-8",
                "Content-Type": "application/json; charset=utf-8",
                "X-imvu-application": "next_desktop/1",
            }
        )

    @classmethod
    def anonymous(cls, **kwargs):
        """Create a client for read-only (guest) access — no login required.

        IMVU's profile / user / subscriber endpoints answer without a session,
        so a guest can look up public stats for any avatar.
        """
        return cls(username=None, password=None, **kwargs)

    # ------------------------------------------------------------------ #
    # Low-level request helper with retries + rate-limit backoff
    # ------------------------------------------------------------------ #
    def _request(self, method, url, *, retries=3, backoff=2.0, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        last_exc = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
                raise IMVUError(f"Сетевая ошибка: {exc}") from exc

            if resp.status_code == 429 and attempt < retries:
                wait = self._retry_after(resp, backoff * (2 ** attempt))
                time.sleep(wait)
                continue
            return resp
        raise IMVUError(f"Запрос не удался: {last_exc}")

    @staticmethod
    def _retry_after(resp, default):
        try:
            return max(float(resp.headers.get("Retry-After", default)), default)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def login(self, code=None):
        """Authenticate and store the session sauce + own user id.

        ``code`` is the optional email security code (2FA). When IMVU asks
        for one, :class:`TwoFactorRequired` is raised — call ``login`` again
        with the code the user received.
        """
        data = {"username": self.username, "password": self.password}
        if code:
            data["2fa_code"] = str(code).strip()
        resp = self._request(
            "POST",
            f"{self.base}/login",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=data,
        )
        if resp.status_code not in (200, 201):
            if self._is_2fa_challenge(resp):
                raise TwoFactorRequired(
                    "Требуется код подтверждения: IMVU отправил код безопасности "
                    "на почту аккаунта. Введите его, чтобы продолжить."
                )
            raise IMVUError(self._login_error(resp))

        data = resp.json()
        login_key = data.get("id")
        denorm = data.get("denormalized", {})
        login_data = denorm.get(login_key, {}).get("data", {})
        sauce = login_data.get("sauce", "")
        if not sauce:
            raise IMVUError("Не удалось получить токен сессии (sauce)")
        user_url = login_data.get("user", {}).get("id", "")
        match = _TRAILING_ID_RE.search(user_url)
        if not match:
            raise IMVUError("Не удалось определить свой user id из ответа логина")
        self.my_user_id = match.group(1)
        self.session.headers["X-imvu-sauce"] = sauce
        return self.my_user_id

    @staticmethod
    def _is_2fa_challenge(resp):
        """Detect the "security code sent to your email" login challenge."""
        try:
            payload = resp.json()
        except ValueError:
            return False
        text = " ".join(
            str(payload.get(k, "")) for k in ("message", "error", "imvu_error")
        ).lower()
        return "security code" in text or "2fa" in text

    @staticmethod
    def _login_error(resp):
        try:
            payload = resp.json()
            msg = payload.get("message") or payload.get("error")
            if msg:
                return f"Вход не удался: {msg}"
        except ValueError:
            pass
        if resp.status_code in (401, 403):
            return "Вход не удался: неверный логин или пароль"
        return f"Вход не удался (HTTP {resp.status_code})"

    def _require_login(self):
        if not self.my_user_id:
            raise IMVUError("Сначала выполните вход (login)")

    # ------------------------------------------------------------------ #
    # Profile / user lookups
    # ------------------------------------------------------------------ #
    def get_profile_summary(self, user_id=None):
        """Return a :class:`UserCard` for the given (or own) user."""
        uid = str(user_id or self.my_user_id or "").strip()
        if not uid:
            raise IMVUError("Не указан user id")
        resp = self._request("GET", f"{self.base}/profile/profile-user-{uid}")
        if resp.status_code != 200:
            raise IMVUError(f"Профиль user-{uid} недоступен (HTTP {resp.status_code})")
        denorm = resp.json().get("denormalized", {})
        data = denorm.get(f"{self.base}/profile/profile-user-{uid}", {}).get("data", {})
        return UserCard(
            user_id=uid,
            name=data.get("title", ""),
            avatar_name=data.get("avatar_name", ""),
            image=data.get("image", ""),
            followers=data.get("approx_follower_count", 0),
            following=data.get("approx_following_count", 0),
            online=bool(data.get("online", False)),
        )

    def get_full_profile(self, user_id=None):
        """Return an extended public profile dict (profile + user records)."""
        uid = str(user_id or self.my_user_id or "").strip()
        if not uid:
            raise IMVUError("Не указан user id")
        card = self.get_profile_summary(uid)
        info = card.as_dict()
        resp = self._request("GET", f"{self.base}/user/user-{uid}", retries=1)
        if resp.status_code == 200:
            denorm = resp.json().get("denormalized", {})
            data = denorm.get(f"{self.base}/user/user-{uid}", {}).get("data", {})
            info.update(
                display_name=data.get("display_name", card.name),
                gender=data.get("gender", ""),
                age=data.get("age"),
                country=data.get("country", ""),
                created=data.get("created", ""),
                registered=data.get("registered"),
                is_vip=bool(data.get("is_vip", False)),
                is_ap=bool(data.get("is_ap", False)),
                is_creator=bool(data.get("is_creator", False)),
                is_staff=bool(data.get("is_staff", False)),
                tagline=data.get("tagline", ""),
                online=bool(data.get("online", card.online)),
            )
        return info

    def resolve_username(self, username):
        """Resolve an avatar name (or numeric id) to a numeric user id."""
        username = str(username).strip()
        if not username:
            raise IMVUError("Пустой логин/ID")
        if username.isdigit():
            return username
        resp = self._request("GET", f"{self.base}/user", params={"username": username})
        if resp.status_code != 200:
            raise IMVUError(f"Не удалось найти пользователя «{username}»")
        denorm = resp.json().get("denormalized", {})
        for key, record in denorm.items():
            uid = _id_from_url(key)
            if uid and "/user/user-" in key:
                cid = record.get("data", {}).get("legacy_cid")
                return str(cid or uid)
        raise IMVUError(f"Пользователь «{username}» не найден")

    # ------------------------------------------------------------------ #
    # Paginated lists (subscribers / subscriptions)
    # ------------------------------------------------------------------ #
    def _list_page(self, relation, target_id, before=None, limit=25):
        url = f"{self.base}/profile/profile-user-{target_id}/{relation}"
        params = {"limit": limit}
        if before:
            params["before"] = before
        resp = self._request("GET", url, params=params)
        if resp.status_code != 200:
            return Page()

        body = resp.json()
        root_key = body.get("id", "")
        denorm = body.get("denormalized", {})
        root = denorm.get(root_key, {})
        root_data = root.get("data", {})
        total_count = root_data.get("total_count", 0)
        items = root_data.get("items", [])

        cards = []
        for item_url in items:
            uid = _id_from_url(item_url)
            if not uid:
                continue
            edge = denorm.get(item_url, {})
            date_added = edge.get("data", {}).get("date_added", "")
            ref = edge.get("relations", {}).get("ref", "")
            profile = denorm.get(ref, {}).get("data", {})
            cards.append(
                UserCard(
                    user_id=uid,
                    name=profile.get("title", ""),
                    avatar_name=profile.get("avatar_name", ""),
                    image=profile.get("image", ""),
                    followers=profile.get("approx_follower_count", 0),
                    online=bool(profile.get("online", False)),
                    date_added=date_added,
                )
            )

        next_before = None
        next_url = root.get("relations", {}).get("next", "")
        if next_url:
            m = _BEFORE_RE.search(next_url)
            if m:
                next_before = m.group(1)
        return Page(cards=cards, total_count=total_count, next_before=next_before)

    def get_subscribers_page(self, target_id, before=None, limit=25):
        """Return (ids, total_count, next_before) — kept for backward compat."""
        page = self._list_page("subscribers", target_id, before=before, limit=limit)
        return [c.user_id for c in page.cards], page.total_count, page.next_before

    def iter_subscribers(self, target_id, limit=25):
        """Yield :class:`UserCard` objects for every subscriber of a target."""
        yield from self._iter_relation("subscribers", target_id, limit)

    def iter_subscriptions(self, user_id=None, limit=50):
        """Yield :class:`UserCard` objects for everyone a user follows."""
        self._require_login()
        uid = str(user_id or self.my_user_id)
        yield from self._iter_relation("subscriptions", uid, limit)

    def _iter_relation(self, relation, target_id, limit):
        before = None
        seen = set()
        while True:
            page = self._list_page(relation, target_id, before=before, limit=limit)
            if not page.cards:
                break
            for card in page.cards:
                if card.user_id not in seen:
                    seen.add(card.user_id)
                    yield card
            if not page.next_before or page.next_before == before:
                break
            before = page.next_before

    def get_subscriptions(self, user_id=None, limit=50):
        """Return (ids, total_count) for the first page of subscriptions."""
        self._require_login()
        uid = str(user_id or self.my_user_id)
        page = self._list_page("subscriptions", uid, limit=limit)
        return [c.user_id for c in page.cards], page.total_count

    # ------------------------------------------------------------------ #
    # Friends (mutual social graph) — uses /user/.../friends (offset paged)
    # ------------------------------------------------------------------ #
    def _friends_page(self, target_id, start_index=0, limit=50):
        url = f"{self.base}/user/user-{target_id}/friends"
        resp = self._request(
            "GET", url, params={"limit": limit, "start_index": start_index}
        )
        if resp.status_code != 200:
            return Page()
        body = resp.json()
        root_key = body.get("id", "")
        denorm = body.get("denormalized", {})
        root = denorm.get(root_key, {})
        root_data = root.get("data", {})
        cards = []
        for item_url in root_data.get("items", []):
            uid = _id_from_url(item_url)
            if not uid:
                continue
            data = denorm.get(f"{self.base}/user/user-{uid}", {}).get("data", {})
            cards.append(
                UserCard(
                    user_id=uid,
                    name=data.get("display_name", ""),
                    avatar_name=data.get("username", ""),
                    image=data.get("thumbnail_url", ""),
                    online=bool(data.get("online", False)),
                )
            )
        next_index = None
        relations = root.get("relations", {})
        next_url = relations.get("next", "") if isinstance(relations, dict) else ""
        m = re.search(r"start_index=(\d+)", next_url or "")
        if m:
            next_index = int(m.group(1))
        return Page(cards=cards, total_count=root_data.get("total_count", 0),
                    next_before=next_index)

    def iter_friends(self, user_id=None, limit=50):
        """Yield :class:`UserCard` objects for a user's IMVU friends."""
        uid = str(user_id or self.my_user_id or "").strip()
        if not uid:
            raise IMVUError("Не указан user id")
        start = 0
        seen = set()
        while True:
            page = self._friends_page(uid, start_index=start, limit=limit)
            if not page.cards:
                break
            for card in page.cards:
                if card.user_id not in seen:
                    seen.add(card.user_id)
                    yield card
            if page.next_before is None or page.next_before == start:
                break
            start = page.next_before

    def get_friends(self, user_id=None, limit=50):
        """Return a list of friend cards (dicts) for a user."""
        return [c.as_dict() for c in self.iter_friends(user_id=user_id, limit=limit)]

    def top_subscriptions(self, n=5, max_scan=3000, limit=50):
        """Return the ``n`` accounts I follow that have the most followers.

        Subscription list pages already carry each target's approximate
        follower count, so this needs no extra per-user requests — it just
        scans my subscriptions (up to ``max_scan``) and keeps the top ``n``.
        """
        self._require_login()
        scanned = 0
        best = {}
        for card in self.iter_subscriptions(limit=limit):
            best[card.user_id] = card
            scanned += 1
            if scanned >= max_scan:
                break
        ranked = sorted(best.values(), key=lambda c: c.followers, reverse=True)
        return [c.as_dict() for c in ranked[:n]], scanned

    # ------------------------------------------------------------------ #
    # Follow / unfollow
    # ------------------------------------------------------------------ #
    def is_following(self, target_id):
        """Return True if the logged-in account follows ``target_id``."""
        self._require_login()
        url = (
            f"{self.base}/profile/profile-user-{self.my_user_id}"
            f"/subscriptions/profile-user-{target_id}"
        )
        resp = self._request("GET", url, retries=1)
        return resp.status_code == 200

    def follow(self, target_id):
        """Follow a single user, returning the HTTP status code (0 on error)."""
        self._require_login()
        url = f"{self.base}/profile/profile-user-{self.my_user_id}/subscriptions"
        body = {"id": f"{self.base}/profile/profile-user-{target_id}"}
        try:
            resp = self._request(
                "POST", url, json=body, params={"limit": 50}, retries=1
            )
            return resp.status_code
        except IMVUError:
            return 0

    def unfollow(self, target_id):
        """Unfollow a single user, returning True on success."""
        self._require_login()
        url = (
            f"{self.base}/profile/profile-user-{self.my_user_id}"
            f"/subscriptions/profile-user-{target_id}"
        )
        try:
            resp = self._request("DELETE", url, retries=1)
        except IMVUError:
            return False
        return resp.status_code in (200, 204)

    def get_non_followers(self, limit=50, exclude=None):
        """Return ids that I follow but who do NOT follow me back.

        ``exclude`` is an optional iterable of user ids to keep (whitelist).
        """
        self._require_login()
        following = {c.user_id for c in self.iter_subscriptions(limit=limit)}
        followers = {c.user_id for c in self.iter_subscribers(self.my_user_id, limit=limit)}
        keep = set(str(x) for x in (exclude or ()))
        return sorted(following - followers - keep)

    def get_relationship_stats(self, limit=50):
        """Compute mutual / fans / non-mutual relationship counts.

        Returns a dict with counts and small id lists:
        - ``following`` / ``followers`` totals
        - ``mutual``       : follow each other
        - ``fans``         : follow me, I don't follow back
        - ``non_followers``: I follow, they don't follow back
        """
        self._require_login()
        following = {c.user_id for c in self.iter_subscriptions(limit=limit)}
        followers = {c.user_id for c in self.iter_subscribers(self.my_user_id, limit=limit)}
        mutual = following & followers
        fans = followers - following
        non_followers = following - followers
        return {
            "following": len(following),
            "followers": len(followers),
            "mutual": len(mutual),
            "fans": len(fans),
            "non_followers": len(non_followers),
            "non_follower_ids": sorted(non_followers),
            "fan_ids": sorted(fans),
        }
