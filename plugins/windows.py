# -*- coding: utf-8 -*-
"""Окна: «окно влево», «окно вправо», «окно на весь экран», «окно по центру», «окно наверх/вниз».
Нужен Универсальный доступ для JARVIS (он уже выдан для VPN-кнопки)."""
import subprocess

SCRIPT = '''
tell application "Finder" to set b to bounds of window of desktop
set sw to item 3 of b
set sh to item 4 of b
set top to 25
set h to sh - top
tell application "System Events"
  set p to first application process whose frontmost is true
  if (name of p) is in {"JARVIS", "Python"} then return "self"
  tell front window of p
    %s
  end tell
end tell
return "ok"
'''
LAYOUTS = {
    "left": "set position to {0, top}\n    set size to {sw / 2, h}",
    "right": "set position to {sw / 2, top}\n    set size to {sw / 2, h}",
    "full": "set position to {0, top}\n    set size to {sw, h}",
    "center": "set size to {sw * 0.66, h * 0.8}\n    set position to {sw * 0.17, top + h * 0.1}",
    "top": "set position to {0, top}\n    set size to {sw, h / 2}",
    "bottom": "set position to {0, top + h / 2}\n    set size to {sw, h / 2}",
}
WORDS = [(r"влево|налево|слева|левую", "left"), (r"вправо|направо|справа|правую", "right"),
         (r"весь экран|разверни|максимум|во весь", "full"), (r"центр", "center"),
         (r"наверх|вверх|сверху|верхнюю", "top"), (r"вниз|снизу|нижнюю", "bottom")]


def register(api):
    import re

    def place(layout):
        if layout not in LAYOUTS:
            return "Не знаю такого расположения."
        try:
            r = subprocess.run(["osascript", "-e", SCRIPT % LAYOUTS[layout]], capture_output=True, text=True, timeout=8)
        except Exception:
            return "Окно не послушалось."
        if r.stdout.strip() == "self":
            return "Переключись на нужное окно — своё я двигать не буду."
        if r.returncode != 0:
            return "Нет доступа к окнам: включи JARVIS в «Универсальном доступе»."
        return "Готово."

    def voice(raw, t, m):
        for rx, lay in WORDS:
            if re.search(rx, t):
                return place(lay)
        return "Куда двинуть окно: влево, вправо, на весь экран, по центру, наверх или вниз?"

    api.command(r"\bокн[оа]\b.*(влево|налево|слева|вправо|направо|справа|весь экран|разверни|центр|наверх|вверх|вниз|снизу|сверху|половин)", voice)
    api.tool("place_window", "Переместить активное окно: left, right, full, center, top, bottom.",
             {"type": "object", "properties": {"layout": {"type": "string", "enum": list(LAYOUTS)}},
              "required": ["layout"]}, place)
