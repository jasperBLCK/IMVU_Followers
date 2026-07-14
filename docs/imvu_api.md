# IMVU REST API — карта эндпоинтов

Справочник по публичному REST API IMVU (`https://api.imvu.com`), собранный
обходом графа связей (`relations`) и энумерацией сервисов. Здесь описано и то,
что уже используется в `imvu_client.py`, и то, что было найдено дополнительно
(feed, presence, product, room, manifest, wishlist, hashtag, order, wallet …).

> Ничего из этого не является официальной документацией IMVU — это результат
> реверса поведения API. Формат ответов и наличие полей могут меняться.

## Оглавление
- [Базовые понятия](#базовые-понятия)
- [Формат ответа (denormalized envelope)](#формат-ответа-denormalized-envelope)
- [Механизм обнаружения (discovery)](#механизм-обнаружения-discovery)
- [Аутентификация](#аутентификация)
- [Сервисы](#сервисы)
  - [user / users](#user--users)
  - [profile (+ subscribers / subscriptions)](#profile--subscribers--subscriptions)
  - [presence](#presence)
  - [feed / feed_element](#feed--feed_element)
  - [profile_outfit](#profile_outfit)
  - [product](#product)
  - [room](#room)
  - [manifest](#manifest)
  - [wishlist / hashtag / outfit / photo](#прочие-сервисы-требуют-сессию)
  - [order / wallet / cart / account](#коммерция-и-аккаунт-требуют-сессию)
- [Что уже использует приложение](#что-уже-использует-приложение)
- [Что можно построить дополнительно](#что-можно-построить-дополнительно)

---

## Базовые понятия

| Параметр | Значение |
|----------|----------|
| Base URL | `https://api.imvu.com` |
| Формат | JSON, «denormalized» граф-структура (см. ниже) |
| Пагинация (профили) | курсор `before=<id>`, ссылка в `relations.next` |
| Пагинация (друзья/поиск) | offset `start_index` / `limit` |
| Заголовки | `X-imvu-application: next_desktop/1`, после логина — `X-imvu-sauce: <token>` |
| TLS | сервер отклоняет современные дефолты; клиент понижает `SECLEVEL=1` (см. `TLSAdapter`) |

Единый ID пользователя (`legacy_cid`) используется во всех URL как `user-<id>` /
`profile-user-<id>` / `presence-<id>` и т.п.

## Формат ответа (denormalized envelope)

Почти все GET-ответы имеют вид:

```jsonc
{
  "status": "success",
  "id": "https://api.imvu.com/<корневой-ресурс>",   // ключ корневой записи
  "denormalized": {
    "https://api.imvu.com/<node-url>": {
      "data":      { /* поля объекта */ },
      "relations": { "<имя>": "https://api.imvu.com/<связанный-url>", ... },
      "updates":   "..."
    },
    ...
  },
  "http": { "status": 200 }
}
```

Список-эндпоинты кладут в `data` корневой записи `{ "items": [<url>, ...],
"total_count": N }`, а сами объекты — соседними ключами в `denormalized`.
Ошибки: `{ "status": "failure", "error": "<CODE>", "message": "...", "details": {...} }`.

Разбор этого конверта уже реализован в `IMVUClient._list_page` и
`_id_from_url` — новые эндпоинты можно парсить тем же способом.

## Механизм обнаружения (discovery)

API само-описываемое: каждый узел отдаёт `relations` — готовые URL связанных
ресурсов. Обойдя их в ширину, можно найти весь достижимый граф.

Коды ошибок помогают энумерировать сервисы, даже не имея сессии:

| Ошибка | Значение |
|--------|----------|
| `REST_DISCOVER_001 Service X does not exist` | такого сервиса нет |
| `AUTHENTICATION-005/006`, `REST-AUTH-401` | сервис есть, но нужна сессия |
| `<SVC>-NODE-006`, `SERVICE-001 Path Not Supported` | сервис есть, но неверный путь/аргументы |
| `ROUTER-002` | метод не поддержан (напр. GET там, где нужен POST) |
| `NODE-001 Couldnt find node` | ресурс не найден |

Т.е. `400 …-NODE-006` и `401 AUTHENTICATION-*` — признак **существующего**
сервиса; `404 REST_DISCOVER_001` — несуществующего.

---

## Сервисы

Легенда: 🟢 — доступно анонимно (гость), 🔒 — требует сессию (sauce).

### user / users

**🟢 `GET /user/user-<id>`** — расширенный публичный профиль.
Поля `data`:
```
created, registered, gender, display_name, age, country, state,
avatar_image, avatar_portrait_image, badge_level, greeter_score,
is_vip, is_ap, is_ap_plus, is_ap_plus_founder, is_creator, is_adult,
is_ageverified, is_staff, is_greeter, is_host, is_discussion_moderator,
has_nft, has_legacy_vip, vip_tier, vip_platform,
relationship_status, orientation, looking_for, interests, tagline,
username, legacy_cid, persona_type, availability, online, thumbnail_url
```
`relations`: `profile`, `profile_outfit`, `wishlist`, `personal_feed`,
`presence`, `hashtag`, `hashtag_category`, `common_hashtags`, `vip_nfts`,
`current_room`.

**🟢 `GET /user?username=<nick>`** — резолв ника в ID (используется в
`resolve_username`). Возвращает запись `user-<id>` + список поиска.

**🟢 `GET /user?<filters>`** — поиск аватаров (например `gender=M`,
`limit`, курсор `previous`/`next`). `data`: `items`, `total_count`.

**🟢 `GET /users?keyword=<q>`** — быстрый поиск пользователей по ключевому
слову; `data.items`.

**🔒 `GET /user/user-<id>/friends?start_index=&limit=`** — список друзей
(offset-пагинация, `relations.next` содержит `start_index=`). Используется в
`iter_friends`.

**🔒** `…/wishlist`, `…/hashtag`, `…/hashtag_category`, `…/outfits`,
`…/current_room` — требуют сессию.

### profile (+ subscribers / subscriptions)

**🟢 `GET /profile/profile-user-<id>`** — компактный профиль.
`data`: `image, title, type, is_persona, reportable, online, avatar_name,
approx_follower_count, approx_following_count`.
`relations`: `subscribers`, `subscriptions`, `presence`, `restgraph_entity`.

**🟢 `GET /profile/profile-user-<id>/subscribers?limit=&before=`** —
подписчики (кто читает этого юзера). Каждый элемент-ребро несёт
`date_added` и `relations.ref` → профиль пользователя.

**🟢 `GET /profile/profile-user-<id>/subscriptions?limit=&before=`** —
подписки (на кого подписан). Та же структура.

**🔒 `GET /profile/profile-user-<me>/subscriptions/profile-user-<id>`** —
проверка «подписан ли я» (`is_following`, 200 = да).

**🔒 `POST /profile/profile-user-<me>/subscriptions`** body
`{"id": "https://api.imvu.com/profile/profile-user-<target>"}` — **подписаться**.

**🔒 `DELETE /profile/profile-user-<me>/subscriptions/profile-user-<id>`** —
**отписаться** (200/204 = успех).

### presence

**🟢 `GET /presence/presence-<id>`** — онлайн-статус. `data`: `{ "online": bool }`.
Дёшево дёргать пачкой для «кто сейчас в сети».

### feed / feed_element

**🟢 `GET /feed/feed-personal-<id>`** — лента активности пользователя.
Элементы `data`: `type` (`photo`, …), `app`, `time`, `payload`.
Пример `payload` для фото: `{width, height, url, thumbnail_url, title, message}`.
`relations`: `elements`, `feed_elements`, `actor`, `actor_profile`, `comments`,
`liked_by`, `liked_by_profile`, `photo`, `photo_details`, `notification_users`,
`next`, `ref`.

**🟢 `GET /feed/feed-personal-<id>/elements?limit=`** — постранично элементы
ленты; каждый `feed_element-<uuid>` → `relations.ref`.

`feed_comment` и `feed_element` существуют как отдельные сервисы (лайки/
комментарии/публикация — POST, требуют сессию).

### profile_outfit

**🟢 `GET /profile_outfit/profile_outfit-<id>`** — «наряд» с витрины профиля.
`data`: `look_url`, `asset_url`, `products` (список продуктов look-а).
`relations`: `user`, `products` (сам список продуктов — 🔒).

### product

**🟢 `GET /product?keywords=&categories=&gender=&limit=`** — поиск товаров
каталога. `data` корня: `items, total_count, ap_upsell_keywords,
av_upsell_keywords, ap_search_denied, av_search_denied`.
Товар `data`:
```
product_id, product_name, creator_cid, creator_name, rating,
product_price, discount_price, product_page, creator_page,
is_bundle, profit, derivation_profit, allows_derivation, product_image,
preview_image, category_path, categories, tags, gender, gender_restriction,
look_url, compatible_body_patterns, has_plus_badge, is_nft, is_boosted,
is_purchasable, is_visible, is_wearable_in_pure, supports_youtube …
```
`relations`: `creator`, `parent`, `ancestor_products`, `uml_products`,
`look_model`, `derivation_fee`, `allow_nft`.
`GET /product/product-<id>` — карточка конкретного товара.

### room

**🟢 `GET /room?limit=`** — поиск/список комнат (`data.items`, `total_count`).
`GET /room/room-<id>` — деталь комнаты (🔒, `AUTHENTICATION-005`).

### manifest

**🟢 `GET /manifest`** — служебные справочники клиента. Возвращает записи
`manifest-category_tree`, `manifest-category_map` и т.п. (`data.manifest`).
Полезно для дерева категорий каталога.

### Прочие сервисы (требуют сессию)

Существуют (подтверждено по кодам ошибок), но отвечают только с сессией:

| Сервис | Назначение (предположительно) |
|--------|-------------------------------|
| `wishlist` | список желаний пользователя |
| `hashtag`, `hashtag_category` | интересы/теги профиля |
| `outfit` | сохранённые образы |
| `photo`, `photo_details`, `album` | фото и альбомы |
| `message`, `conversation` | личные сообщения |
| `event` | события/вечеринки |
| `quest` | задания/квесты |
| `nft`, `vip_nfts` | NFT-инвентарь |
| `creator` | данные создателя контента |
| `subscription` | подписки на уровне узла |

### Коммерция и аккаунт (требуют сессию)

| Сервис | Назначение |
|--------|------------|
| `order` | заказы/покупки (🟢 отвечает пустым списком без сессии) |
| `cart` | корзина |
| `wallet` | баланс кредитов |
| `account` | настройки аккаунта |
| `email` | смена/подтверждение почты (POST, `ROUTER-002` на GET) |
| `gift` | подарки (POST) |

---

## Аутентификация

**`POST /login`** (JSON) — тело `{"username", "password"}`.
Успех: `denormalized[<id>].data.sauce` — токен сессии; его кладут в заголовок
`X-imvu-sauce`. Свой ID берётся из `…data.user.id` (URL `…/user-<id>`).

**Двухфакторный код (2FA).** Для части аккаунтов логин отвечает
`202`/ошибкой `LOGIN-017`:
```json
{"status":"failure","error":"LOGIN-017",
 "message":"A security code has been sent to your email: %email_address",
 "details":{"email_address":"****228@gmail.com","remember_device_supported":true}}
```
Тогда нужно повторить `POST /login` c дополнительными полями
`{"2fa_code":"<код из письма>", "remember_device": true}` (именно JSON и
`remember_device` булевым — форменный запрос поле не пропускает). Неверный код →
`LOGIN-016`. Слишком частые запросы кода → `LOGIN`-лимит («wait 5 minutes»).
`remember_device: true` уменьшает частоту повторных запросов кода на этом
клиенте (сохраняется через cookie сессии).

Реализовано в `IMVUClient.login(code=...)` / `TwoFactorRequired` и в веб-роутерах
`/api/auth/account`, `/api/auth/2fa`, `/api/auth/2fa/resend`.

---

## Что уже использует приложение

`imvu_client.py` покрывает: `POST /login` (+2FA), `GET /user?username=`
(resolve), `GET /user/user-<id>` (full profile), `GET /profile/profile-user-<id>`
(+ `subscribers` / `subscriptions`), `GET /user/user-<id>/friends`,
`is_following`, `follow` (POST), `unfollow` (DELETE), а также агрегаты
`top_subscriptions`, `get_non_followers`, `get_relationship_stats`.

## Что можно построить дополнительно

Найденные, но пока не задействованные возможности:

- **Онлайн-индикатор** через `presence` (дёшево, без сессии) — подсветка
  «в сети» в превью подписчиков/друзей.
- **Лента и фото** через `feed`/`feed_element` — показывать последние посты
  цели или свои; поиск активных аккаунтов.
- **Наряд профиля** (`profile_outfit`) и **каталог** (`product` + `manifest`
  для дерева категорий) — карточка «что на аватаре» и поиск товаров.
- **Поиск аватаров** (`GET /users?keyword=`, `GET /user?gender=…`) — находить
  цели для подписок по ключевым словам/фильтрам вместо только ника.
- **Комнаты** (`room`) — список/поиск публичных комнат.
- **Коммерция** (`wallet`, `order`, `cart`) — баланс кредитов и история
  покупок в дашборде (нужна сессия).

## Как воспроизвести обход

Скрипты разведки не входят в репозиторий, но обход тривиально повторяется:
взять анонимный `IMVUClient.anonymous()`, начать с `user-<id>` / `profile-user-<id>`,
рекурсивно идти по `relations`, а список сервисов проверять запросом
`GET /<service>` и классификацией по кодам из раздела
[Механизм обнаружения](#механизм-обнаружения-discovery).
