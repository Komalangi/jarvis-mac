# -*- coding: utf-8 -*-
"""Система плагинов JARVIS.

Каждый файл plugins/*.py (рядом с jarvis.py или в ~/Library/Application Support/JARVIS/plugins)
экспортирует register(api). Через api плагин добавляет:
  api.command(regex, fn)          голосовая/текстовая команда: fn(raw, t, match) -> str | dict | None
  api.tool(name, desc, schema, fn) инструмент для LLM-мозга: fn(**args) -> str
  api.loop(fn, interval)          фоновая задача
  api.route(method, path, fn)     HTTP-маршрут /api/ext/...: fn(body|query) -> dict
  api.say(text)                   сказать голосом / показать
  api.core                        сам модуль jarvis.py (settings, load_json, sh, ...)
Сломанный плагин не роняет ядро: ошибка пишется в ERRORS.
"""

import importlib.util
import os
import re
import threading
import time
import traceback

COMMANDS = []      # (regex, fn, plugin)
TOOLS = {}         # name -> {"description", "input_schema", "fn", "plugin"}
ROUTES = {}        # (METHOD, path) -> fn
LOADED = []
ERRORS = {}


class PluginAPI:
    def __init__(self, core, name):
        self.core = core
        self.name = name

    def command(self, pattern, fn):
        COMMANDS.append((re.compile(pattern), fn, self.name))

    def tool(self, name, description, schema, fn):
        TOOLS[name] = {"description": description, "input_schema": schema or {"type": "object", "properties": {}},
                       "fn": fn, "plugin": self.name}

    def loop(self, fn, interval, first_delay=30):
        def run():
            time.sleep(first_delay)
            while True:
                try:
                    fn()
                except Exception:
                    ERRORS[self.name + ":loop"] = traceback.format_exc(limit=3)
                time.sleep(interval)
        threading.Thread(target=run, daemon=True, name="plugin-" + self.name).start()

    def route(self, method, path, fn):
        ROUTES[(method.upper(), path)] = fn

    def say(self, text, prio=2, trick=None, voice=True):
        c = self.core
        if voice and c.can_talk(prio):
            c._brain["last_talk"] = time.time()
            c.announce(text, trick=trick, os_notify=prio >= 3)
        else:
            c.announce(text, trick=trick, voice=False, os_notify=False)

    def data(self, name, default=None):
        return self.core.load_json(name, default)

    def save(self, name, value):
        self.core.save_json(name, value)


def plugin_dirs(core):
    return [os.path.join(core.HERE, "plugins"), os.path.join(core.SUPPORT, "plugins")]


def load_all(core):
    off = set(core.load_json("plugins_off.json", []) or [])
    for d in plugin_dirs(core):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            name = fn[:-3]
            if name in off or name in LOADED:
                continue
            try:
                spec = importlib.util.spec_from_file_location("jarvis_plugin_" + name, os.path.join(d, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                mod.register(PluginAPI(core, name))
                LOADED.append(name)
            except Exception:
                ERRORS[name] = traceback.format_exc(limit=4)
                print("[плагин %s] ошибка загрузки:\n%s" % (name, ERRORS[name]), flush=True)


def match_command(raw, t):
    for rx, fn, name in COMMANDS:
        m = rx.search(t)
        if not m:
            continue
        try:
            r = fn(raw, t, m)
        except Exception:
            ERRORS[name + ":cmd"] = traceback.format_exc(limit=3)
            continue
        if r:
            return r
    return None


def handle_route(method, path, payload):
    fn = ROUTES.get((method.upper(), path))
    if not fn:
        return None
    try:
        return fn(payload)
    except Exception as e:
        return {"error": str(e)}


def status():
    return {"loaded": list(LOADED), "errors": dict(ERRORS), "commands": len(COMMANDS), "tools": sorted(TOOLS)}
