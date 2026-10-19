# -*- coding: utf-8 -*-
"""Календарь (Calendar.app) и расширенный брифинг."""
import subprocess

SCRIPT = '''
set d1 to current date
set time of d1 to 0
set d2 to d1 + (%d * days)
set out to ""
tell application "Calendar"
 repeat with c in calendars
  try
   set evs to (every event of c whose start date >= d1 and start date < d2)
   repeat with e in evs
    set sd to start date of e
    set mm to (minutes of sd) as string
    if (minutes of sd) < 10 then set mm to "0" & mm
    if allday event of e then
     set out to out & "весь день " & (summary of e) & linefeed
    else
     set out to out & ((hours of sd) as string) & ":" & mm & " " & (summary of e) & linefeed
    end if
   end repeat
  end try
 end repeat
end tell
return out
'''


def register(api):
    core = api.core

    def today(days=1):
        try:
            r = subprocess.run(["osascript", "-e", SCRIPT % max(1, min(int(days), 14))], capture_output=True,
                               text=True, timeout=45)
        except Exception:
            return "Календарь не ответил."
        if r.returncode != 0:
            return "Нет доступа к Календарю: разреши JARVIS управлять Calendar в настройках приватности."
        lines = sorted(l for l in r.stdout.splitlines() if l.strip())
        return "\n".join(lines) or "Событий нет."

    def voice(raw, t, m):
        ev = today(1)
        if ev == "Событий нет." or ev.startswith(("Нет доступа", "Календарь")):
            return ev if ev != "Событий нет." else "В календаре на сегодня пусто."
        lines = ev.splitlines()
        return "На сегодня событий %d. %s." % (len(lines), "; ".join(lines[:5]))

    def extended(raw="", t="", m=None):
        base = core.brief_phrase()
        ev = voice(raw, t, m)
        return base + " " + ev

    api.tool("calendar_events", "События из Календаря на сегодня или на несколько дней вперёд.",
             {"type": "object", "properties": {"days": {"type": "integer", "description": "1 = сегодня"}}}, today)
    api.command(r"что в календаре|какие (у меня )?встречи|что у меня сегодня|мое расписание|календарь на", voice)
    api.command(r"расширенный брифинг|полный брифинг|утренний доклад", extended)
