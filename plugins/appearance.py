# -*- coding: utf-8 -*-
"""Темы оформления дашборда и виджета: «тема красная», «тема стандартная», «какие есть темы оформления»."""
import re

THEMES = [  # (ключ, корень слова, название)
    ("arc", r"голуб|стандарт|обычн|реактор|исходн", "голубой реактор"),
    ("ice", r"ледян|син|лед", "ледяная синяя"),
    ("violet", r"фиолет|пурпур|лилов", "фиолетовая"),
    ("red", r"красн|марк|боев", "красная боевая"),
    ("gold", r"золот|оранж|янтар", "золотая"),
    ("green", r"зелен|изумруд", "зелёная"),
]


def register(api):
    core = api.core

    def set_theme(key):
        core.save_settings({"theme": key})
        return "Тема «%s» включена." % next(n for k, _, n in THEMES if k == key)

    def by_voice(raw, t, m):
        for key, rx, _ in THEMES:
            if re.search(rx, t):
                return set_theme(key)
        return "Темы оформления: " + ", ".join(n for _, _, n in THEMES) + ". Скажи, например, «тема красная»."

    api.command(r"\bтем[ауы]\b.*\b(голуб|стандарт|обычн|ледян|син|фиолет|пурпур|красн|боев|золот|оранж|зелен)|"
                r"тем[ауы]? оформления|сменить тему|смени тему|поменяй тему", by_voice)
    api.tool("set_theme", "Сменить тему оформления дашборда: arc, ice, violet, red, gold, green.",
             {"type": "object", "properties": {"theme": {"type": "string", "enum": [k for k, _, _ in THEMES]}},
              "required": ["theme"]}, lambda theme: set_theme(theme))
