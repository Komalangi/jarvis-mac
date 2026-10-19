# -*- coding: utf-8 -*-
"""Минимальный MCP-клиент (stdio): подключает внешние MCP-серверы как инструменты для LLM-мозга.

Конфиг ~/JARVIS/mcp.json:
{
  "servers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Docs"]}},
  "allow": ["files_read_*", "files_list_*"]        # только эти инструменты видны мозгу (маски fnmatch)
}
По умолчанию (пустой allow) наружу не торчит ничего: мозг не сможет случайно отправить сообщение или удалить файл.
"""

import fnmatch
import json
import os
import re
import subprocess
import threading
import time

CONF = os.path.expanduser("~/JARVIS/mcp.json")
PROTO = "2025-06-18"


class Server:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.proc = None
        self.next_id = 1
        self.waiters = {}
        self.lock = threading.Lock()
        self.tools = []

    def start(self):
        env = dict(os.environ, **{str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        self.proc = subprocess.Popen([self.cfg["command"]] + [str(a) for a in self.cfg.get("args", [])],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=env, text=True, encoding="utf-8", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": PROTO, "capabilities": {},
                                    "clientInfo": {"name": "jarvis", "version": "3.1"}}, timeout=40)
        self.notify("notifications/initialized")
        self.tools = self.request("tools/list", {}, timeout=20).get("tools", [])

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            w = self.waiters.pop(msg.get("id"), None) if "method" not in msg else None
            if w:
                w["msg"] = msg
                w["ev"].set()

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params, timeout=30):
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        w = {"ev": threading.Event(), "msg": None}
        self.waiters[rid] = w
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not w["ev"].wait(timeout):
            self.waiters.pop(rid, None)
            raise TimeoutError("MCP %s: нет ответа на %s" % (self.name, method))
        msg = w["msg"]
        if "error" in msg:
            raise RuntimeError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result", {})

    def call(self, tool, args):
        res = self.request("tools/call", {"name": tool, "arguments": args or {}}, timeout=60)
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return ("Ошибка инструмента: " if res.get("isError") else "") + (text or "(пусто)")


def _safe(s):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)


def register_all(register_tool, log=print):
    try:
        with open(CONF, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    allow = cfg.get("allow") or []
    if not allow:
        log("[mcp] allow пуст — инструменты не публикуются")

    def boot(name, scfg):
        try:
            srv = Server(name, scfg)
            srv.start()
        except Exception as e:
            log("[mcp] %s не стартовал: %s" % (name, e))
            return
        for t in srv.tools:
            tname = _safe("%s_%s" % (name, t["name"]))[:64]
            if not any(fnmatch.fnmatch(tname, pat) for pat in allow):
                continue
            register_tool(tname, (t.get("description") or t["name"])[:900],
                          t.get("inputSchema") or {"type": "object", "properties": {}},
                          (lambda s, n: lambda **a: s.call(n, a))(srv, t["name"]))
        log("[mcp] %s: %d инструментов на сервере" % (name, len(srv.tools)))

    for name, scfg in (cfg.get("servers") or {}).items():
        threading.Thread(target=boot, args=(name, scfg), daemon=True).start()
        time.sleep(0.2)
