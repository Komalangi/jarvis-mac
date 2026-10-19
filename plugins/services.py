# -*- coding: utf-8 -*-
"""Мониторинг своих сайтов и серверов. Список — в ~/JARVIS/Сервисы.json:
  {"sites": ["https://example.com", "https://api.example.com/health"]}
Каждые 5 минут проверяю; упал — скажу, поднялся — тоже. «Как мои сайты»."""
import json
import os
import time
import urllib.error
import urllib.request

PATH = os.path.expanduser("~/JARVIS/Сервисы.json")
state = {}


def register(api):
    def sites():
        try:
            with open(PATH, encoding="utf-8") as f:
                return [s for s in json.load(f).get("sites", []) if isinstance(s, str) and s.startswith("http")]
        except Exception:
            return []

    def probe(url):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-monitor"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status < 500, r.status, int((time.time() - t0) * 1000)
        except urllib.error.HTTPError as e:
            return e.code < 500, e.code, int((time.time() - t0) * 1000)
        except Exception:
            return False, 0, 0

    def host(url):
        return url.split("://", 1)[-1].split("/", 1)[0]

    def check():
        for url in sites():
            ok, code, ms = probe(url)
            prev = state.get(url)
            fails = 0 if ok else (prev or {}).get("fails", 0) + 1
            state[url] = {"ok": ok, "code": code, "ms": ms, "fails": fails, "t": time.time()}
            if fails == 2:                      # два провала подряд, чтобы не паниковать из-за одного таймаута
                api.core.notify("JARVIS · сервисы", "%s не отвечает" % host(url), sound=True)
                api.say("Тревога: %s не отвечает уже десять минут." % host(url), prio=3)
            if ok and prev and prev.get("fails", 0) >= 2:
                api.say("%s снова в строю." % host(url), prio=2)

    def report(raw="", t="", m=None):
        ss = sites()
        if not ss:
            return "Список сайтов пуст. Добавь их в файл Сервисы.json в папке JARVIS."
        check()
        down = [host(u) for u in ss if not state.get(u, {}).get("ok")]
        if not down:
            avg = sum(state[u]["ms"] for u in ss) // len(ss)
            return "Все сайты на связи: %d, средний ответ %d миллисекунд." % (len(ss), avg)
        return "Не отвечают: %s." % ", ".join(down)

    if not os.path.exists(PATH):
        try:
            os.makedirs(os.path.dirname(PATH), exist_ok=True)
            with open(PATH, "w", encoding="utf-8") as f:
                json.dump({"sites": []}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    api.loop(check, 300, first_delay=60)
    api.command(r"как (там )?мои сайты|проверь сайты|статус сайтов|статус сервисов", report)
    api.tool("services_status", "Доступность сайтов и серверов пользователя из его списка мониторинга.", None, report)
    api.route("GET", "/api/ext/services", lambda q: {"sites": state})
