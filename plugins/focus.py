# -*- coding: utf-8 -*-
"""Автофокус: пока ты долго сидишь в Xcode/редакторе, Джарвис не болтает (важное скажет всегда).
Выключить: «автофокус выключи», включить: «автофокус включи»."""
import time

DEV = ("xcode", "code", "cursor", "pycharm", "intellij", "webstorm", "android studio", "sublime", "zed", "nova")
CALLS = ("zoom.us", "zoom", "facetime", "microsoft teams", "teams", "skype", "webex", "slack huddle", "discord", "telegram call")
st = {"since": 0, "away": 0, "armed": True, "call": False}


def register(api):
    core = api.core

    def enabled():
        return (api.data("focus.json", {}) or {}).get("on", True)

    def tick():
        if not enabled():
            return
        front = core.frontmost_app().lower()
        now = time.time()
        if front in CALLS:                       # звонок: не мешаем, пока окно звонка активно
            if not st["call"]:
                core.save_settings({"quiet_until": now + 20 * 60})
            st["call"] = True
            return
        st["call"] = False
        if any(front == d or front.startswith(d) for d in DEV):
            st["away"] = 0
            st["since"] = st["since"] or now
            if st["armed"] and now - st["since"] > 600 and core.talk_mode() != "quiet":
                core.save_settings({"quiet_until": now + 30 * 60})
                st["armed"] = False
        else:
            st["away"] = st["away"] or now
            if now - st["away"] > 300:
                st.update(since=0, armed=True)

    def toggle(raw, t, m):
        on = bool(__import__("re").search(r"включи|вкл", t)) and "выключ" not in t
        api.save("focus.json", {"on": on})
        return "Автофокус включён: во время долгой работы в редакторе я помолчу." if on else "Автофокус выключен."

    api.loop(tick, 20, first_delay=20)
    api.command(r"автофокус", toggle)
