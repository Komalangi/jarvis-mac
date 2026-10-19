# -*- coding: utf-8 -*-
"""Привычки: вода и любые свои отметки.
«выпил воды», «сколько я выпил воды», «отметь привычку зарядка», «статистика привычек».
Напоминание о воде — днём раз в 2 часа, если не отмечался (выключить: «не напоминай про воду»)."""
import re
import time


def register(api):
    core = api.core

    def data():
        return api.data("habits.json", {"log": [], "water_remind": True}) or {"log": [], "water_remind": True}

    def mark(name, n=1):
        d = data()
        d["log"].append({"t": int(time.time()), "h": name, "n": n})
        d["log"] = d["log"][-3000:]
        api.save("habits.json", d)

    def count(name, since):
        return sum(e.get("n", 1) for e in data()["log"] if e["h"] == name and e["t"] >= since)

    def water(raw, t, m):
        n = 1
        mm = re.search(r"(\d+) стакан", t)
        if mm:
            n = int(mm.group(1))
        mark("вода", n)
        c = count("вода", core.day_start(0))
        tail = " Норма почти выполнена." if c >= 6 else ""
        return "Отметил. Сегодня стаканов воды: %d.%s" % (c, tail)

    def water_stat(raw, t, m):
        return "Сегодня стаканов воды: %d." % count("вода", core.day_start(0))

    def habit(raw, t, m):
        name = m.group(2).strip()
        mark(name)
        week = count(name, core.day_start(6))
        return "Отметил «%s». За неделю: %d %s." % (name, week, core.plural(week, ("раз", "раза", "раз")))

    def stats(raw="", t="", m=None):
        since = core.day_start(6)
        agg = {}
        for e in data()["log"]:
            if e["t"] >= since:
                agg[e["h"]] = agg.get(e["h"], 0) + e.get("n", 1)
        if not agg:
            return "За неделю привычек не отмечено."
        return "За неделю: " + ", ".join("%s %d" % kv for kv in sorted(agg.items(), key=lambda kv: -kv[1])) + "."

    def toggle_remind(raw, t, m):
        d = data()
        d["water_remind"] = not re.search(r"не напоминай|хватит", t)
        api.save("habits.json", d)
        return "Буду напоминать про воду." if d["water_remind"] else "Хорошо, про воду больше не напоминаю."

    def remind():
        d = data()
        h = time.localtime().tm_hour
        if not d.get("water_remind", True) or not (10 <= h < 21) or core.idle_seconds() > 300:
            return
        last = max([e["t"] for e in d["log"] if e["h"] == "вода"] + [d.get("last_remind", 0), core.day_start(0) + 9 * 3600])
        if time.time() - last > 2 * 3600:
            d["last_remind"] = int(time.time())
            api.save("habits.json", d)
            api.say("Пара часов без воды. Стакан воды, и скажи «выпил воды».", prio=1)

    api.loop(remind, 900, first_delay=300)
    api.command(r"сколько (я )?(выпил|попил)\w* вод", water_stat)
    api.command(r"(выпил|попил|выпила)\w* (\d+ стакан\w* )?вод", water)
    api.command(r"(не )?напоминай (мне )?(про|о) вод|хватит напоминать (про|о) вод", toggle_remind)
    api.command(r"отметь (привычку )?(.+)", habit)
    api.command(r"статистика привычек|мои привычки", stats)
    api.tool("habits_stats", "Статистика привычек пользователя за неделю (вода и т.п.).", None, stats)
