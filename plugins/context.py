# -*- coding: utf-8 -*-
"""Контекст: активное окно, выделенный текст, история буфера, взгляд на экран."""
import os
import re
import subprocess
import tempfile
import time
from collections import deque

CLIP = deque(maxlen=30)           # только в памяти — на диск буфер не пишем
SECRET_APPS = ("1password", "bitwarden", "keychain", "keeper", "lastpass", "dashlane", "enpass")
_last = [""]


def register(api):
    core = api.core

    def active_window():
        script = ('tell application "System Events"\n set p to first application process whose frontmost is true\n'
                  ' set n to name of p\n set w to ""\n try\n set w to name of front window of p\n end try\n'
                  ' return n & "|" & w\nend tell')
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=6)
            app, _, title = r.stdout.strip().partition("|")
        except Exception:
            return "Не удалось определить активное окно."
        return "Активная программа: %s%s." % (app or "неизвестно", (", окно «%s»" % title) if title else "")

    def selection():
        s = core.copy_selection()
        return s[:3000] if s else "Ничего не выделено."

    def clip_history(n=5):
        items = list(CLIP)[-int(n):]
        return "\n".join("%d) %s" % (i + 1, x[:300]) for i, x in enumerate(reversed(items))) or "История буфера пуста."

    def poll():
        try:
            front = core.frontmost_app().lower()
            if any(k in front for k in SECRET_APPS):
                return
            cur = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3,
                                 env=dict(os.environ, LANG="ru_RU.UTF-8")).stdout
        except Exception:
            return
        cur = cur.strip()
        if cur and cur != _last[0] and len(cur) < 4000:
            _last[0] = cur
            CLIP.append(cur)

    def look(question="Опиши коротко, что на экране и в чём может быть проблема."):
        import jarvis_brain as B
        if not B.configured():
            return "Для зрения нужен ключ Anthropic: " + B.SETUP_HINT
        d = tempfile.mkdtemp(prefix="jarvis_shot_")
        png, jpg = os.path.join(d, "s.png"), os.path.join(d, "s.jpg")
        r = subprocess.run(["screencapture", "-x", png], capture_output=True, timeout=10)
        if r.returncode != 0 or not os.path.exists(png) or os.path.getsize(png) < 1000:
            return "Не могу сделать снимок. Включи JARVIS в разделе «Запись экрана» в настройках приватности."
        subprocess.run(["sips", "-Z", "1600", "-s", "format", "jpeg", png, "--out", jpg], capture_output=True, timeout=15)
        try:
            return B.vision(question, jpg if os.path.exists(jpg) else png)
        finally:
            for f in (png, jpg):
                try:
                    os.remove(f)
                except OSError:
                    pass
            try:
                os.rmdir(d)
            except OSError:
                pass

    api.loop(poll, 3, first_delay=5)
    api.tool("active_window", "Какая программа и какое окно сейчас активны у пользователя.", None, active_window)
    api.tool("selected_text", "Получить текст, который пользователь сейчас выделил в активном окне.", None, selection)
    api.tool("clipboard_history", "Последние элементы буфера обмена (новые первыми).",
             {"type": "object", "properties": {"n": {"type": "integer"}}}, clip_history)
    api.tool("look_at_screen", "Сделать снимок экрана и ответить на вопрос о том, что на нём видно "
             "(ошибки, текст, интерфейс). Используй, когда пользователь говорит «посмотри», «что на экране».",
             {"type": "object", "properties": {"question": {"type": "string"}}}, look)

    api.command(r"что (у меня )?в буфере|история буфера|что я копировал",
                lambda raw, t, m: "Последнее в буфере: " + (CLIP[-1][:200] if CLIP else "пусто") + ".")
    api.command(r"что я сейчас делаю|какое (сейчас )?окно|что (у меня )?открыто",
                lambda raw, t, m: active_window())
    api.command(r"что (у меня )?на экране|посмотри на экран|что ты видишь|объясни ошибку на экране",
                lambda raw, t, m: look())
