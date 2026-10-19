# -*- coding: utf-8 -*-
"""Музыка: «что играет», и Марк-0 пускается в пляс, когда начинается новый трек (Music / Spotify)."""
import subprocess

APPS = ("Music", "Spotify")
st = {"last": ""}


def register(api):
    core = api.core

    def now_playing():
        for app in APPS:
            script = ('if application "%s" is running then\n tell application "%s"\n'
                      ' if player state is playing then return (artist of current track) & " — " & (name of current track)\n'
                      ' end tell\nend if\nreturn ""' % (app, app))
            try:
                out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                out = ""
            if out:
                return out
        return ""

    def tick():
        cur = now_playing()
        if cur and st["last"] and cur != st["last"] and core.talk_mode() != "quiet":
            core.pet_trick("dance")
        st["last"] = cur

    def say(raw, t, m):
        cur = now_playing()
        return ("Сейчас играет: %s." % cur) if cur else "Сейчас ничего не играет."

    api.loop(tick, 8, first_delay=15)
    api.tool("now_playing", "Что сейчас играет в Music или Spotify.", None, lambda: now_playing() or "Ничего не играет.")
    api.command(r"что (сейчас )?играет|какая (это )?песня|что за трек", say)


# ───────────── плейлисты Apple Music по названию и по настроению ─────────────
# ~/Library/Application Support/JARVIS/music.json: {"focus": "Lo-fi", "party": "Вечеринка", "relax": "Chill"}
MOODS = [(r"фокус|работ|сосредоточ|концентрац", "focus"), (r"вечеринк|танц|бодр|энерг", "party"),
         (r"спокойн|расслаб|отдых|чил", "relax"), (r"сон|засып|спать", "sleep")]


def _playlists():
    try:
        out = subprocess.run(["osascript", "-e", 'tell application "Music" to get name of user playlists'],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [x.strip() for x in out.split(", ") if x.strip()]


def _play(name):
    import difflib
    pls = _playlists()
    if not pls:
        return "В Apple Music не вижу плейлистов."
    low = {p.lower(): p for p in pls}
    hit = low.get(name.lower()) or next((p for k, p in low.items() if name.lower() in k), None)
    if not hit:
        c = difflib.get_close_matches(name.lower(), list(low), n=1, cutoff=0.5)
        hit = low[c[0]] if c else None
    if not hit:
        return "Плейлист «%s» не нашёл. Есть: %s." % (name, ", ".join(pls[:6]))
    subprocess.run(["osascript", "-e", 'tell application "Music" to play playlist %s' % json_q(hit)],
                   capture_output=True, timeout=15)
    return "Включаю «%s»." % hit


def json_q(s):
    import json
    return json.dumps(s, ensure_ascii=False)


_old_register = register


def register(api):  # noqa: F811  расширяем базовую регистрацию
    import re
    _old_register(api)

    def by_mood(raw, t, m):
        cfg = api.data("music.json", {}) or {}
        for rx, key in MOODS:
            if re.search(rx, t):
                if cfg.get(key):
                    return _play(cfg[key])
                return ("Для настроения «%s» плейлист не задан. Скажи «включи плейлист» и название, "
                        "или впиши его в music.json." % key)
        return None

    api.command(r"(включи|поставь) плейлист (.+)", lambda raw, t, m: _play(m.group(2)))
    api.command(r"(включи|поставь) музыку (для|под) (.+)", by_mood)
    api.tool("play_playlist", "Включить плейлист Apple Music по названию (неточное совпадение допустимо).",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, _play)
