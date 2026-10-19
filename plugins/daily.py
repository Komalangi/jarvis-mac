# -*- coding: utf-8 -*-
"""Итоги дня и недели; вечером Джарвис сам подводит итог, если ты работал."""
import time


def register(api):
    core = api.core

    def summary(days_ago=0):
        tot = core.worked(core.day_start(days_ago), core.day_start(days_ago - 1) if days_ago else None)
        if not tot:
            return None
        parts = ["%s — %s" % (p, core.fmt_minutes(s)) for p, s in sorted(tot.items(), key=lambda kv: -kv[1])[:4]]
        return "В работе %s: %s." % (core.fmt_minutes(sum(tot.values())), ", ".join(parts))

    def today(raw="", t="", m=None):
        s = summary(0)
        arts = core.articles_since(core.day_start(0))
        extra = (" Статей сегодня: %d." % len(arts)) if arts else ""
        return ((s or "Сегодня рабочий таймер не запускался.") + extra)

    def week(raw="", t="", m=None):
        days = []
        for d in range(7):
            tot = core.worked(core.day_start(d), core.day_start(d - 1) if d else None)
            days.append(sum(tot.values()))
        total = sum(days)
        if not total:
            return "За неделю рабочий таймер не запускался."
        best = max(range(7), key=lambda i: days[i])
        names = ["сегодня", "вчера"] + ["%d дней назад" % i for i in range(2, 7)]
        return "За неделю в работе %s, в среднем %s в день. Больше всего — %s: %s." % (
            core.fmt_minutes(total), core.fmt_minutes(total / 7), names[best], core.fmt_minutes(days[best]))

    def evening():
        lt = time.localtime()
        st = api.data("daily.json", {}) or {}
        today_s = time.strftime("%Y-%m-%d")
        if not (19 <= lt.tm_hour < 23) or st.get("evening") == today_s or core.idle_seconds() > 180:
            return
        s = summary(0)
        if not s:
            return
        st["evening"] = today_s
        api.save("daily.json", st)
        api.say("Подведу итог дня. " + s + " Хороший вечер, пора выдохнуть.", prio=1)

    api.loop(evening, 600, first_delay=120)
    api.tool("day_summary", "Сколько и над чем пользователь работал сегодня.", None, today)
    api.tool("week_summary", "Статистика рабочего времени за неделю.", None, week)
    api.command(r"итог дня|итоги дня|подведи итог|сколько я сегодня работал", today)
    api.command(r"итоги недели|статистика недели|сколько я работал за неделю", week)
