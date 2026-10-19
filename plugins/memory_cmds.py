# -*- coding: utf-8 -*-
"""Голосовые команды памяти и настройки мозга."""
import re


def register(api):
    import jarvis_brain as B

    def rem(raw, t, m):
        fact = raw.strip()
        fact = re.sub(r"^\s*(джарвис[, ]*)?запомни[,:]?\s*(что\s+)?", "", fact, flags=re.I)
        return B.remember(fact)

    def what(raw, t, m):
        fs = B.facts()
        if not fs:
            return "Пока я ничего о тебе не запомнил. Скажи «запомни, что…»."
        return "Я помню: " + "; ".join(f["text"] for f in fs[-6:]) + "."

    def fgt(raw, t, m):
        q = re.sub(r"^.*?забудь[,:]?\s*(что\s+)?", "", raw.strip(), flags=re.I)
        return B.forget(q)

    def setup(raw, t, m):
        if B.configured():
            return "Мозг подключён, модель %s. Спрашивай что угодно." % B.conf()["model"]
        return B.SETUP_HINT

    api.command(r"^(джарвис )?запомни\b", rem)
    api.command(r"что ты (обо мне )?помнишь|что ты знаешь обо мне", what)
    api.command(r"\bзабудь\b", fgt)
    api.command(r"настро[йи]\w* мозг|подключи мозг|статус мозга|мозг подключен", setup)
