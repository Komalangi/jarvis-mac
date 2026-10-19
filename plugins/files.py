# -*- coding: utf-8 -*-
"""Файлы: поиск по содержимому (Spotlight) и разбор Загрузок с возможностью отката."""
import os
import shutil
import subprocess
import time

TYPES = {
    "Картинки": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tiff", ".bmp"},
    "Документы": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".pages", ".md", ".odt", ".epub"},
    "Таблицы": {".xls", ".xlsx", ".csv", ".numbers", ".ods"},
    "Презентации": {".ppt", ".pptx", ".key"},
    "Архивы": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz"},
    "Установщики": {".dmg", ".pkg", ".iso"},
    "Видео": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "Аудио": {".mp3", ".wav", ".m4a", ".flac", ".aac"},
    "Код": {".py", ".js", ".ts", ".json", ".html", ".css", ".swift", ".sh", ".yaml", ".yml"},
}
PARTIAL = (".crdownload", ".download", ".part", ".tmp", ".opdownload")
DL = os.path.expanduser("~/Downloads")


def register(api):
    def content_search(query, limit=8):
        q = str(query).strip()
        if not q:
            return "Что искать?"
        try:
            out = subprocess.run(["mdfind", "-onlyin", os.path.expanduser("~"), q], capture_output=True, text=True,
                                 timeout=15).stdout.splitlines()
        except Exception:
            return "Поиск не отработал."
        out = [p for p in out if "/Library/" not in p and "/." not in p][:int(limit)]
        return "\n".join(out) or "Ничего не нашёл."

    def kind(ext):
        for k, exts in TYPES.items():
            if ext in exts:
                return k
        return "Прочее"

    def plan(min_age=3600):
        res = []
        if not os.path.isdir(DL):
            return res
        now = time.time()
        for f in os.listdir(DL):
            p = os.path.join(DL, f)
            if f.startswith(".") or not os.path.isfile(p) or f.lower().endswith(PARTIAL):
                continue
            if now - os.path.getmtime(p) < min_age:
                continue
            res.append((p, kind(os.path.splitext(f)[1].lower())))
        return res

    def summary():
        pl = plan()
        if not pl:
            return "В Загрузках нечего разбирать."
        cnt = {}
        for _, k in pl:
            cnt[k] = cnt.get(k, 0) + 1
        return "К разбору %d файлов: %s." % (len(pl), ", ".join("%s %d" % kv for kv in cnt.items()))

    def sort_downloads(raw="", t="", m=None):
        pl = plan()
        if not pl:
            return "В Загрузках нечего разбирать."
        moves = []
        for p, k in pl:
            dst_dir = os.path.join(DL, "Разобрано", k)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, os.path.basename(p))
            if os.path.exists(dst):
                base, ext = os.path.splitext(os.path.basename(p))
                dst = os.path.join(dst_dir, "%s_%d%s" % (base, int(time.time()), ext))
            try:
                shutil.move(p, dst)
                moves.append([p, dst])
            except OSError:
                pass
        api.save("downloads_moves.json", {"t": time.time(), "moves": moves})
        return "Разобрал файлов: %d. Они в папке «Разобрано», скажи «верни загрузки» для отмены." % len(moves)

    def undo(raw="", t="", m=None):
        data = api.data("downloads_moves.json", {}) or {}
        n = 0
        for src, dst in data.get("moves", []):
            if os.path.exists(dst) and not os.path.exists(src):
                try:
                    shutil.move(dst, src)
                    n += 1
                except OSError:
                    pass
        api.save("downloads_moves.json", {"t": 0, "moves": []})
        return "Вернул на место файлов: %d." % n if n else "Откатывать нечего."

    api.tool("search_file_contents", "Поиск файлов по СОДЕРЖИМОМУ текста (Spotlight), а не по имени.",
             {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
              "required": ["query"]}, content_search)
    api.tool("downloads_summary", "Что лежит в Загрузках и сколько файлов можно разобрать по типам.", None, summary)
    api.tool("sort_downloads", "Разложить старые файлы из Загрузок по папкам по типу (обратимо). Делай только по просьбе.",
             None, lambda: sort_downloads())
    api.command(r"где написано (.+)|найди по содержимому (.+)|найди в тексте (.+)",
                lambda raw, t, m: "Нашёл: " + content_search(m.group(1) or m.group(2) or m.group(3), 5).replace("\n", "; "))
    api.command(r"разбери загрузки|почисти загрузки|разложи загрузки", sort_downloads)
    api.command(r"верни загрузки|отмени разбор", undo)
    api.command(r"что в загрузках", lambda raw, t, m: summary())
