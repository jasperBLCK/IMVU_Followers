"""CLI: auto-follow the subscribers of a target IMVU account.

Credentials and the target are read from environment variables so nothing
sensitive is stored in the source:

    IMVU_USERNAME=you IMVU_PASSWORD=secret IMVU_TARGET=kilka00192 \
        python imvu_follow.py

Optional: IMVU_MAX (default 2000), IMVU_DELAY (seconds, default 0.8).
"""

import os
import sys
import time

from imvu_client import IMVUClient, IMVUError


def main():
    username = os.environ.get("IMVU_USERNAME")
    password = os.environ.get("IMVU_PASSWORD")
    target = os.environ.get("IMVU_TARGET")
    if not (username and password and target):
        sys.exit("Задайте IMVU_USERNAME, IMVU_PASSWORD и IMVU_TARGET в окружении.")

    max_follows = int(os.environ.get("IMVU_MAX", "2000"))
    delay = float(os.environ.get("IMVU_DELAY", "0.8"))

    client = IMVUClient(username, password)
    client.login()
    print(f"Вошёл! User ID: {client.my_user_id}")

    target_id = client.resolve_username(target)
    done = {client.my_user_id}
    total = 0
    print(f"Старт! Цель: user-{target_id}")

    for card in client.iter_subscribers(target_id):
        if total >= max_follows:
            break
        if card.user_id in done:
            continue
        done.add(card.user_id)

        code = client.follow(card.user_id)
        if code in (200, 201):
            total += 1
            print(f"  [{total}/{max_follows}] {card.name}")
        elif code in (400, 429):
            print("  rate limit, жду 10 сек...")
            time.sleep(10)
            if client.follow(card.user_id) in (200, 201):
                total += 1
                print(f"  [{total}/{max_follows}] {card.name}")
        elif code == 0:
            print(f"  обрыв, пропуск {card.name}")
        else:
            print(f"  [{code}] пропуск {card.name}")
        time.sleep(delay)

    print(f"Итого подписок: {total}")


if __name__ == "__main__":
    try:
        main()
    except IMVUError as exc:
        sys.exit(f"Ошибка: {exc}")
