# -*- coding: utf-8 -*-
"""Страж: новые автозапуски, место на диске, обновления macOS."""
import os
import shutil
import time

LAUNCH_DIRS = ["~/Library/LaunchAgents", "/Library/LaunchAgents", "/Library/LaunchDaemons"]


def register(api):
    core = api.core

    def launch_items():
        out = set()
        for d in LAUNCH_DIRS:
            d = os.path.expanduser(d)
            if os.path.isdir(d):
                out.update(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".plist"))
        return out

    def alert(text, prio=2):
        core.notify("JARVIS · страж", text, sound=True)
        api.say(text, prio=prio)

    def listeners():
        """Программы, принимающие соединения из сети (не только с этого мака)."""
        out = core.sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=10)
        res = set()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 9:
                continue
            addr = parts[8]
            if addr.startswith(("127.0.0.1:", "[::1]:", "localhost:")):
                continue
            res.add("%s %s" % (parts[0], addr.rsplit(":", 1)[-1]))
        return res

    def check():
        st = api.data("guard.json", {}) or {}
        ls = listeners()
        if "ports" in st:
            new = sorted(ls - set(st["ports"]))
            if new:
                core.notify("JARVIS · страж", "Открыт доступ из сети: " + ", ".join(new[:3]))
                api.say("Новая программа слушает сеть: %s. Если это не твой сервер — проверь." % new[0].replace(" ", ", порт "), prio=1)
        st["ports"] = sorted(ls | set(st.get("ports", [])))[-200:]
        now = launch_items()
        known = set(st.get("launch", []))
        if known:
            new = sorted(now - known)
            if new:
                names = ", ".join(os.path.basename(p).replace(".plist", "") for p in new[:3])
                alert("Появился новый автозапуск: %s. Если это не ты ставил — стоит проверить." % names, 3)
        st["launch"] = sorted(now)
        today = time.strftime("%Y-%m-%d")
        du = shutil.disk_usage("/")
        free = du.free / du.total
        if free < 0.10 and st.get("disk_day") != today:
            st["disk_day"] = today
            alert("На системном диске осталось %d процентов, около %d гигабайт. Пора чистить кэши." % (
                round(free * 100), du.free // 1024 ** 3))
        if time.time() - st.get("upd_t", 0) > 86400:
            st["upd_t"] = time.time()
            out = core.sh(["softwareupdate", "-l"], timeout=120)
            labels = [l.split("Label:")[1].strip() for l in out.splitlines() if "Label:" in l]
            st["updates"] = labels[:5]
            if labels and st.get("upd_notified") != labels[0]:
                st["upd_notified"] = labels[0]
                api.say("Доступно обновление macOS: %s." % labels[0], prio=1)
        api.save("guard.json", st)

    def report(raw="", t="", m=None):
        st = api.data("guard.json", {}) or {}
        du = shutil.disk_usage("/")
        sec = core.SENSE.read_security()
        bad = [k for k, v in (("FileVault", sec.get("filevault")), ("брандмауэр", sec.get("firewall")),
                              ("SIP", sec.get("sip")), ("Gatekeeper", sec.get("gatekeeper"))) if v is False]
        parts = ["Защита: %s." % ("всё включено" if not bad else "выключено — " + ", ".join(bad)),
                 "Автозапусков отслеживаю %d." % len(st.get("launch", [])),
                 "Открытых в сеть портов: %d." % len(listeners()),
                 "Свободно на диске %d процентов." % round(du.free / du.total * 100)]
        if st.get("updates"):
            parts.append("Ждёт обновление: %s." % st["updates"][0])
        return " ".join(parts)

    api.loop(check, 600, first_delay=45)
    api.tool("security_report", "Сводка по защите Mac: FileVault, брандмауэр, SIP, Gatekeeper, автозапуски, диск, обновления.",
             None, report)
    api.command(r"проверь защиту|как (там )?защита|отчет стража|безопасност", report)
