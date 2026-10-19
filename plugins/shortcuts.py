# -*- coding: utf-8 -*-
"""Быстрые команды macOS (Shortcuts): «запусти быструю команду Свет в спальне», «какие у меня быстрые команды».
Через них же — HomeKit, умный дом и всё, что умеет приложение «Команды»."""
import difflib
import subprocess


def register(api):
    def names():
        try:
            out = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=15).stdout
        except Exception:
            return []
        return [l.strip() for l in out.splitlines() if l.strip()]

    def run(name):
        all_ = names()
        if not all_:
            return "Не вижу ни одной быстрой команды."
        low = {n.lower(): n for n in all_}
        hit = low.get(name.lower().strip()) or next((n for k, n in low.items() if name.lower().strip() in k), None)
        if not hit:
            close = difflib.get_close_matches(name.lower(), list(low), n=1, cutoff=0.5)
            hit = low[close[0]] if close else None
        if not hit:
            return "Не нашёл быструю команду «%s»." % name
        try:
            r = subprocess.run(["shortcuts", "run", hit], capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return "Команда «%s» запущена, но долго работает." % hit
        out = (r.stdout or "").strip()
        return ("Выполнил «%s»." % hit) + ((" Результат: " + out[:200]) if out else "")

    def list_voice(raw, t, m):
        ns = names()
        if not ns:
            return "Быстрых команд нет. Их можно сделать в приложении «Команды»."
        return "Быстрых команд %d: %s." % (len(ns), ", ".join(ns[:10]))

    api.command(r"(запусти|выполни|включи) (быструю команду|ярлык|шорткат) (.+)", lambda raw, t, m: run(m.group(3)))
    api.command(r"какие (у меня )?(есть )?(быстрые команды|ярлыки|шорткаты)", list_voice)
    api.tool("list_shortcuts", "Список быстрых команд macOS (приложение «Команды»), включая умный дом.", None,
             lambda: "\n".join(names()) or "Нет быстрых команд.")
    api.tool("run_shortcut", "Запустить быструю команду macOS по названию (умный дом, автоматизации).",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, run)
