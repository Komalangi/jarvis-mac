# -*- coding: utf-8 -*-
"""LLM-мозг JARVIS: свободная речь, вызов инструментов, долговременная память.

Подключается как fallback: если ни одно правило из jarvis.py не сработало, запрос уходит сюда.
Ключ Anthropic берётся из переменной ANTHROPIC_API_KEY или из Связки ключей macOS (service «jarvis-anthropic»):
    security add-generic-password -s jarvis-anthropic -a jarvis -w 'sk-ant-...'
Настройки: ~/Library/Application Support/JARVIS/brain.json
    {"enabled": true, "model": "...", "max_tokens": 600, "user_name": "Имя"}
"""

import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque

import jarvis_plugins as PL

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_ROUNDS = 6
HISTORY_TTL = 600

_core = None
_key = {"v": None, "t": 0}
_hist = deque(maxlen=16)
_last_used = [0.0]
_hinted = [False]
last_error = [""]


def init(core):
    global _core
    _core = core


# ───────────────────────── ключ и настройки ─────────────────────────

def conf():
    c = {"enabled": True, "model": DEFAULT_MODEL, "max_tokens": 600, "user_name": ""}
    c.update(_core.load_json("brain.json", {}) if _core else {})
    return c


def get_key():
    if _key["v"] is not None and time.time() - _key["t"] < 300:
        return _key["v"]
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", "jarvis-anthropic", "-w"],
                               capture_output=True, text=True, timeout=5)
            k = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            k = ""
    _key.update(v=k, t=time.time())
    return k


def configured():
    return bool(conf().get("enabled", True) and get_key())


SETUP_HINT = ("Чтобы я понимал свободную речь, нужен ключ Anthropic. Выполни в терминале: security "
              "add-generic-password -s jarvis-anthropic -a jarvis -w, вставь ключ — и всё заработает.")


# ───────────────────────── память ─────────────────────────

def facts():
    return (_core.load_json("memory.json", {}) or {}).get("facts", [])


def remember(text):
    text = " ".join(str(text).split())[:300]
    if not text:
        return "Нечего запоминать."
    fs = facts()
    if any(f["text"].lower() == text.lower() for f in fs):
        return "Это я уже знаю."
    fs.append({"t": int(time.time()), "text": text})
    _core.save_json("memory.json", {"facts": fs[-300:]})
    return "Запомнил: " + text


def forget(query):
    q = str(query).lower().strip()
    fs = facts()
    keep = [f for f in fs if q not in f["text"].lower()]
    _core.save_json("memory.json", {"facts": keep})
    n = len(fs) - len(keep)
    return "Забыл записей: %d." % n if n else "Ничего подходящего не нашёл."


def recall(query=""):
    q = str(query).lower().strip()
    hits = [f["text"] for f in facts() if not q or q in f["text"].lower()]
    return "\n".join("- " + h for h in hits[-30:]) or "Пока ничего не помню по этой теме."


# ───────────────────────── вызов API ─────────────────────────

def _post(payload, timeout=60):
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
        "content-type": "application/json", "x-api-key": get_key(), "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError("HTTP %s %s" % (e.code, msg))


def _emotions():
    try:
        return sorted(_core.JV.EMOTIONS)
    except Exception:
        return ["calm", "happy", "curious", "witty", "concerned"]


def system_prompt():
    c = _core
    lines = [
        "Ты — J.A.R.V.I.S., голосовой ассистент и системный монитор на Mac пользователя.",
        "Стиль: вежливый, спокойный, с сухим британским юмором, но по делу. Отвечай по-русски.",
        "Твой ответ будет ОЗВУЧЕН: никакого markdown, списков, эмодзи и ссылок; без символов и сокращений, которые "
        "плохо читаются вслух. Обычно 1–3 коротких предложения; длиннее — только если просят подробности.",
        "Действуй через инструменты, а не рассуждай: нужно открыть программу, изменить громкость, поставить таймер — "
        "вызови jarvis_command с обычной русской фразой. Данные о системе, окне, буфере, проектах, календаре бери "
        "из инструментов, не выдумывай.",
        "Не вызывай необратимые действия (выключение, перезагрузка, удаление) — предложи пользователю сделать это "
        "самому. Если не уверен в намерении, коротко переспроси.",
        "В самом начале ответа поставь одну эмоцию в квадратных скобках из списка: %s. Например: [witty] Готово." %
        ", ".join(_emotions()),
        "Сейчас: %s." % time.strftime("%A, %d.%m.%Y, %H:%M"),
    ]
    name = str(conf().get("user_name") or "").strip()
    if name:
        lines.insert(1, "Пользователя зовут %s." % name)
    fs = facts()[-40:]
    if fs:
        lines.append("Что ты знаешь о пользователе:\n" + "\n".join("- " + f["text"] for f in fs))
    return "\n".join(lines)


def _tools_spec():
    return [{"name": n, "description": t["description"], "input_schema": t["input_schema"]}
            for n, t in sorted(PL.TOOLS.items())]


def _run_tool(name, args):
    t = PL.TOOLS.get(name)
    if not t:
        return "Нет такого инструмента.", True
    try:
        out = t["fn"](**(args or {}))
        return (str(out) if out is not None else "Готово.")[:4000], False
    except Exception as e:
        return "Ошибка: %s" % e, True


def _split_emotion(text):
    m = re.match(r"\s*\[(\w+)\]\s*", text)
    if m and m.group(1) in _emotions():
        return m.group(1), text[m.end():].strip()
    return None, re.sub(r"^\s*\[\w+\]\s*", "", text).strip()


def _clean_for_speech(s):
    s = re.sub(r"[*_`#>]+", "", s)
    s = re.sub(r"https?://\S+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def ask(text):
    """Главный вход. Возвращает {"say", "emotion"} или None, если мозг недоступен."""
    if not configured():
        return None
    now = time.time()
    if now - _last_used[0] > HISTORY_TTL:
        _hist.clear()
    _last_used[0] = now
    msgs = list(_hist) + [{"role": "user", "content": text}]
    new_from = len(msgs) - 1
    c = conf()
    final = ""
    try:
        for _ in range(MAX_ROUNDS):
            resp = _post({"model": c["model"], "max_tokens": int(c["max_tokens"]), "system": system_prompt(),
                          "tools": _tools_spec(), "messages": msgs})
            blocks = resp.get("content", [])
            msgs.append({"role": "assistant", "content": blocks})
            if resp.get("stop_reason") != "tool_use":
                final = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                break
            results = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    out, err = _run_tool(b["name"], b.get("input"))
                    results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out, "is_error": err})
            msgs.append({"role": "user", "content": results})
        else:
            final = "Это заняло больше шагов, чем я рассчитывал. Давай попробуем проще."
    except Exception as e:
        last_error[0] = str(e)
        print("[мозг] ошибка: %s" % e, flush=True)
        return None
    # в историю идёт только чистый текст, без громоздких tool-блоков
    emo, spoken = _split_emotion(final)
    spoken = _clean_for_speech(spoken)
    if not spoken:
        return None
    _hist.append({"role": "user", "content": text})
    _hist.append({"role": "assistant", "content": spoken})
    r = {"say": spoken}
    if emo:
        r["emotion"] = emo
    return r


def vision(question, image_path, max_tokens=500):
    """Вопрос по картинке (скриншоту). Возвращает текст."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mt = "image/png" if ext == "png" else "image/jpeg"
    resp = _post({"model": conf()["model"], "max_tokens": max_tokens, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": b64}},
        {"type": "text", "text": question}]}]}, timeout=90)
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


# ───────────────────────── встроенные инструменты ─────────────────────────

BLOCKED = re.compile(r"выключи\s+(компьютер|мак)|перезагруз|заверши\s+работ|удали|сотри|форматир", re.I)


def register_builtin_tools():
    c = _core

    def jarvis_command(phrase):
        if BLOCKED.search(phrase):
            return "Это действие нельзя выполнять через мозг: пусть пользователь скажет сам."
        r = c.text_command(phrase, llm=False, log=False)
        if isinstance(r, dict):
            r = r.get("say", "")
        return str(r or "Команда выполнена.")

    def system_status():
        return c.status_phrase()

    def now():
        return time.strftime("%A, %d.%m.%Y, %H:%M:%S (%Z)")

    S = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}
    add = PL.PluginAPI(c, "core").tool
    add("jarvis_command", "Выполнить любую встроенную команду JARVIS обычной русской фразой: открыть/закрыть программу, "
        "громкость, пауза/трек, таймер, напоминание, помидор, погода, который час, скриншот, VPN, поиск файлов, "
        "протоколы (фокус, вечеринка, призрак), диагностика, калькулятор, шутка. Опасные команды заблокированы.",
        S({"phrase": {"type": "string", "description": "команда как её сказал бы человек, напр. «громкость на 30»"}},
          ["phrase"]), jarvis_command)
    add("system_status", "Краткий отчёт о состоянии Mac: нагрузка, память, диск, температура, сеть.", S({}), system_status)
    add("current_time", "Точные текущие дата и время.", S({}), now)
    add("remember", "Запомнить факт о пользователе или его предпочтение навсегда (имя, привычки, проекты, правила).",
        S({"fact": {"type": "string"}}, ["fact"]), lambda fact: remember(fact))
    add("forget", "Забыть сохранённые факты, содержащие подстроку.", S({"query": {"type": "string"}}, ["query"]), forget)
    add("recall", "Показать, что JARVIS помнит (опционально по подстроке).", S({"query": {"type": "string"}}), recall)
