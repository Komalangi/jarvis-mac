# -*- coding: utf-8 -*-
"""Быстрые заметки голосом: «заметка купить фильтры», «запиши идею …», «прочитай заметки».
Хранятся в ~/JARVIS/Заметки.md — обычный файл, можно открыть в любом редакторе."""
import os
import re
import time

PATH = os.path.expanduser("~/JARVIS/Заметки.md")


def register(api):
    def add(text):
        text = " ".join(str(text).split()).strip(" .,:")
        if not text:
            return "Что записать?"
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        new = not os.path.exists(PATH)
        with open(PATH, "a", encoding="utf-8") as f:
            if new:
                f.write("# Заметки JARVIS\n\n")
            f.write("- %s — %s\n" % (time.strftime("%d.%m %H:%M"), text))
        return "Записал."

    def items():
        try:
            with open(PATH, encoding="utf-8") as f:
                return [l[2:].strip() for l in f if l.startswith("- ")]
        except OSError:
            return []

    def read(raw="", t="", m=None):
        it = items()
        if not it:
            return "Заметок пока нет. Скажи «заметка» и текст."
        return "Последние заметки: " + "; ".join(x.split(" — ", 1)[-1] for x in it[-5:]) + "."

    def add_voice(raw, t, m):
        body = re.sub(r"^\s*(джарвис[, ]*)?(заметка|запиши( заметку| идею| мысль)?|запомни на заметку)[,:]?\s*", "",
                      raw.strip(), flags=re.I)
        return add(body)

    api.command(r"^(джарвис )?(заметка|запиши)\b", add_voice)
    api.command(r"прочитай заметки|мои заметки|последние заметки|что я записывал", read)
    api.tool("add_note", "Добавить заметку в файл заметок пользователя.",
             {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, add)
    api.tool("read_notes", "Последние заметки пользователя (до 30).", None,
             lambda: "\n".join(items()[-30:]) or "Заметок нет.")
