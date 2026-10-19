# -*- coding: utf-8 -*-
"""Вебхук: другие скрипты могут говорить через JARVIS.
    ./jarvis-say "Сборка завершена"        (скрипт рядом с jarvis.py)
или POST /api/ext/say {"text": "...", "emotion": "happy", "prio": 2} с заголовком X-Jarvis-Token.
POST /api/ext/command {"text": "протокол вечеринка", "speak": true} выполнит команду, как будто её сказали вслух."""
import time


def register(api):
    core = api.core

    def say(body):
        text = str((body or {}).get("text", "")).strip()[:400]
        if not text:
            return {"error": "пустой текст"}
        prio = int((body or {}).get("prio", 2))
        emo = (body or {}).get("emotion") or None
        if core.can_talk(prio):
            core._brain["last_talk"] = time.time()
            core.announce(text, emotion=emo, os_notify=prio >= 3, title=str(body.get("title") or "JARVIS"))
        else:
            core.announce(text, emotion=emo, voice=False, os_notify=True, title=str(body.get("title") or "JARVIS"))
        return {"ok": True}

    def command(body):
        text = str((body or {}).get("text", "")).strip()[:400]
        if not text:
            return {"error": "пустая команда"}
        r = core.clean_reply(core.text_command(text))
        if (body or {}).get("speak") and r.get("say"):
            # как будто команду сказали голосом: ответ звучит, эффекты и трюки Марка видны на экране
            pet = (r.get("pet") or {}).get("cmd")
            core.announce(r["say"], trick=pet if pet in ("flip", "salute", "dance", "spin") else None,
                          emotion=r.get("emotion"), os_notify=False, ui=r.get("ui"))
        return {"ok": True, "reply": r.get("say", "")}

    api.route("POST", "/api/ext/say", say)
    api.route("POST", "/api/ext/command", command)
    api.route("GET", "/api/ext/plugins", lambda q: __import__("jarvis_plugins").status())
    api.command(r"какие (у тебя )?плагины|список плагинов",
                lambda raw, t, m: "Плагинов загружено %d: %s." % (
                    len(__import__("jarvis_plugins").LOADED), ", ".join(__import__("jarvis_plugins").LOADED)))
