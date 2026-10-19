# -*- coding: utf-8 -*-
"""Вебхук: другие скрипты могут говорить через JARVIS.
    ./jarvis-say "Сборка завершена"        (скрипт рядом с jarvis.py)
или POST /api/ext/say {"text": "...", "emotion": "happy", "prio": 2} с заголовком X-Jarvis-Token."""
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
        return {"ok": True, "reply": r.get("say", "")}

    api.route("POST", "/api/ext/say", say)
    api.route("POST", "/api/ext/command", command)
    api.route("GET", "/api/ext/plugins", lambda q: __import__("jarvis_plugins").status())
    api.command(r"какие (у тебя )?плагины|список плагинов",
                lambda raw, t, m: "Плагинов загружено %d: %s." % (
                    len(__import__("jarvis_plugins").LOADED), ", ".join(__import__("jarvis_plugins").LOADED)))
