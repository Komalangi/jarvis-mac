#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Демо-тур для записи видео: Джарвис сам говорит, меняет темы, зовёт Марка и включает пасхалки.

Запусти JARVIS, открой дашборд, включи запись экрана (например, в OBS) и выполни:
    python3 tools/demo_tour.py            # весь тур, около минуты
    python3 tools/demo_tour.py --list     # показать шаги и не запускать
Перед записью включи «Не беспокоить» и убери из кадра личное: уведомления, браузер, файлы на рабочем столе.
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7777"
TOKEN_FILE = os.path.expanduser("~/Library/Application Support/JARVIS/token")

# (пауза перед шагом, действие, аргумент)
TOUR = [
    (1.0, "say", "Добрый вечер. Все системы в норме, реактор стабилен."),
    (5.0, "say", "Слежу за процессором, памятью, дисками и сетью. И за тобой, когда ты слишком долго без перерыва."),
    (7.0, "say", "Красный боевой."),
    (0.3, "theme", "red"),
    (2.2, "say", "Золотой парадный."),
    (0.3, "theme", "gold"),
    (2.2, "say", "Или зелёный, для спокойствия."),
    (0.3, "theme", "green"),
    (3.0, "theme", "arc"),
    (1.0, "say", "А это Марк-ноль. Он живёт у меня на экране."),
    (4.0, "command", "марк станцуй"),
    (4.0, "command", "протокол вечеринка"),
    (7.0, "command", "я железный человек"),
    (6.0, "say", "Код открыт. Ссылка в описании."),
]


def post(path, body, token):
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-Jarvis-Token": token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def main():
    if "--list" in sys.argv:
        for pause, kind, arg in TOUR:
            print("+%.1fс  %-8s %s" % (pause, kind, arg))
        return
    try:
        token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        urllib.request.urlopen(BASE + "/api/ping", timeout=2)
    except Exception:
        sys.exit("JARVIS не запущен. Открой его и попробуй снова.")
    theme_before = json.loads(urllib.request.urlopen(BASE + "/api/settings", timeout=5).read()).get("theme", "arc")
    print("Тур начнётся через 3 секунды. Включай запись.")
    time.sleep(3)
    try:
        for pause, kind, arg in TOUR:
            time.sleep(pause)
            print("·", kind, arg)
            if kind == "say":
                post("/api/ext/say", {"text": arg, "prio": 3}, token)
            elif kind == "theme":
                post("/api/settings", {"theme": arg}, token)
            elif kind == "command":
                post("/api/ext/command", {"text": arg, "speak": True}, token)
    finally:
        post("/api/settings", {"theme": theme_before}, token)
    print("Готово.")


if __name__ == "__main__":
    main()
