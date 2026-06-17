"""CLI: auto-unfollow everyone the logged-in IMVU account follows.

Credentials are read from environment variables so nothing sensitive is stored
in the source:

    IMVU_USERNAME=you IMVU_PASSWORD=secret python imvu_unfollow.py

Optional:
    IMVU_DELAY            seconds between requests (default 0.3)
    IMVU_NON_FOLLOWERS=1  only unfollow people who don't follow you back
"""

import os
import sys
import time

from imvu_client import IMVUClient, IMVUError


def main():
    username = os.environ.get("IMVU_USERNAME")
    password = os.environ.get("IMVU_PASSWORD")
    if not (username and password):
        sys.exit("Задайте IMVU_USERNAME и IMVU_PASSWORD в окружении.")

    delay = float(os.environ.get("IMVU_DELAY", "0.3"))
    only_non_followers = os.environ.get("IMVU_NON_FOLLOWERS") in ("1", "true", "yes")

    client = IMVUClient(username, password)
    client.login()
    print(f"Вошёл! User ID: {client.my_user_id}")

    total = 0
    if only_non_followers:
        targets = client.get_non_followers()
        print(f"Невзаимных подписок: {len(targets)}")
        for uid in targets:
            if client.unfollow(uid):
                total += 1
                print(f"  [{total}] user-{uid}")
            else:
                print(f"  пропуск user-{uid}")
            time.sleep(delay)
    else:
        while True:
            cards = list(client.iter_subscriptions())
            if not cards:
                break
            for card in cards:
                if client.unfollow(card.user_id):
                    total += 1
                    print(f"  [{total}] {card.name}")
                else:
                    print(f"  пропуск {card.name}")
                time.sleep(delay)

    print(f"Итого отписок: {total}")


if __name__ == "__main__":
    try:
        main()
    except IMVUError as exc:
        sys.exit(f"Ошибка: {exc}")
