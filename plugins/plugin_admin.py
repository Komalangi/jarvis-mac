# -*- coding: utf-8 -*-
"""Управление плагинами голосом: «отключи плагин музыка», «включи плагин музыка». Вступает в силу после перезапуска."""
import difflib


def register(api):
    import jarvis_plugins as P

    def find(name):
        name = name.strip().lower()
        known = sorted(set(P.LOADED) | set(api.data("plugins_off.json", []) or []))
        aliases = {"музык": "music", "окн": "windows", "заметк": "notes", "привычк": "habits", "сайт": "services",
                   "сервис": "services", "страж": "guard", "фокус": "focus", "календар": "agenda", "проект": "projects",
                   "тем": "appearance", "ярлык": "shortcuts", "файл": "files", "итог": "daily", "буфер": "context"}
        for k, v in aliases.items():
            if name.startswith(k):
                return v
        if name in known:
            return name
        c = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        return c[0] if c else None

    def toggle(raw, t, m):
        on = m.group(1).startswith("вкл")
        name = find(m.group(2))
        if not name or name == "plugin_admin":
            return "Не знаю такого плагина."
        off = set(api.data("plugins_off.json", []) or [])
        (off.discard if on else off.add)(name)
        api.save("plugins_off.json", sorted(off))
        return "Плагин %s %s. Изменения — после перезапуска JARVIS." % (name, "включён" if on else "отключён")

    api.command(r"(включи|отключи|выключи) плагин (.+)", toggle)
