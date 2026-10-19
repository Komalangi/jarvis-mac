#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. — системный монитор для macOS.

Запуск:   python3 ~/Desktop/JARVIS/jarvis.py
При первом запуске соберётся нативное приложение ~/Applications/JARVIS.app —
дальше можно открывать через Spotlight (Cmd+Пробел → JARVIS).

Ничего не удаляет и не меняет в системе: только читает показатели
через стандартные утилиты macOS (top, ps, vm_stat, pmset, ioreg, netstat).
"""

import json
import secrets
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_voice as JV          # noqa: E402  голос: синтез, эмоции, эффекты
import jarvis_sense as SENSE       # noqa: E402  датчики полного дашборда
import jarvis_plugins as PLUG      # noqa: E402  плагины (plugins/*.py)
import jarvis_brain as BRAIN       # noqa: E402  LLM-мозг, память, инструменты
import jarvis_mcp as MCP           # noqa: E402  MCP-клиент

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 7777
URL = "http://127.0.0.1:%d/" % PORT
HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.expanduser("~/Library/Application Support/JARVIS")
DEMO = os.environ.get("JARVIS_DEMO") == "1"
IDLE_EXIT = 15 * 60          # выключиться, если окно закрыто 15 минут
ENV = dict(os.environ, LC_ALL="C", LANG="C")

GB = 1024 ** 3


# ───────────────────────── утилиты ─────────────────────────

def sh(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=ENV).stdout
    except Exception:
        return ""


def num(s, default=0.0):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return default


def sysctl(name):
    return sh(["sysctl", "-n", name]).strip()


# ───────────────────────── состояние ─────────────────────────

LOCK = threading.Lock()
LAST_REQ = [time.time()]
S = {
    "cpu": None,                       # {"user":..,"sys":..,"idle":..}
    "cpu_hist": deque(maxlen=90),
    "net": {"down": 0, "up": 0},
    "net_hist": deque(maxlen=90),
    "procs": [],
    "mem": None,
    "pressure_free": None,
    "battery": None,
    "therm": None,
    "caches": None,
    "cores": [],
}
HOT = {}                               # имя процесса -> сколько замеров подряд он горячий


def static_info():
    if DEMO:
        return {"model": "MacBookPro16,2", "cpu": "Intel(R) Core(TM) i5-1038NG7 CPU @ 2.00GHz",
                "cores": 8, "ram": 8 * GB, "macos": "15.6", "host": "macbook-pro",
                "boot": int(time.time()) - 3 * 86400 - 7200}
    info = {}
    info["model"] = sysctl("hw.model")
    info["cpu"] = sysctl("machdep.cpu.brand_string") or "Apple Silicon"
    info["cores"] = int(num(sysctl("hw.ncpu"), os.cpu_count() or 1))
    info["ram"] = int(num(sysctl("hw.memsize")))
    info["macos"] = sh(["sw_vers", "-productVersion"]).strip()
    info["host"] = socket.gethostname().replace(".local", "")
    m = re.search(r"sec = (\d+)", sysctl("kern.boottime"))
    info["boot"] = int(m.group(1)) if m else None
    return info


INFO = static_info()


# ───────────────────────── сборщики ─────────────────────────

def read_cpu():
    """Загрузка процессора и каждого ядра — напрямую из ядра macOS (без top)."""
    total, cores = SENSE.read_cpu_cores()
    if total:
        S["cores"] = cores
        return total
    return read_cpu_top()


def read_cpu_top():
    out = sh(["top", "-l", "2", "-n", "0", "-s", "1"], timeout=10)
    lines = [l for l in out.splitlines() if l.startswith("CPU usage")]
    if not lines:
        return None
    d = {k: num(v) for v, k in re.findall(r"([\d.,]+)% (user|sys|idle)", lines[-1])}
    if "idle" not in d:
        return None
    return {"user": d.get("user", 0), "sys": d.get("sys", 0), "idle": d["idle"]}


_net_prev = [None, None]


def read_net():
    out = sh(["netstat", "-ib"])
    rx = tx = 0
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) < 10 or not p[0].startswith("en") or not p[2].startswith("<Link#"):
            continue
        if len(p) >= 11:
            rx += int(num(p[6])); tx += int(num(p[9]))
        else:
            rx += int(num(p[5])); tx += int(num(p[8]))
    now = time.time()
    res = {"down": 0, "up": 0}
    if _net_prev[0] is not None:
        dt = max(now - _net_prev[1], 0.5)
        prx, ptx = _net_prev[0]
        res = {"down": max(rx - prx, 0) / dt, "up": max(tx - ptx, 0) / dt}
    _net_prev[0], _net_prev[1] = (rx, tx), now
    return res


def read_procs():
    out = sh(["ps", "-Ao", "pid=,pcpu=,rss=,comm="])
    rows = []
    for line in out.splitlines():
        p = line.strip().split(None, 3)
        if len(p) < 4:
            continue
        name = os.path.basename(p[3].rstrip("/")) or p[3]
        rows.append({"pid": int(num(p[0])), "cpu": num(p[1]),
                     "mem": int(num(p[2])) * 1024, "name": name})
    return rows


def read_mem():
    vm = sh(["vm_stat"])
    m = re.search(r"page size of (\d+)", vm)
    page = int(m.group(1)) if m else 4096

    def g(key):
        mm = re.search(re.escape(key) + r":\s+(\d+)", vm)
        return int(mm.group(1)) * page if mm else 0

    wired, comp = g("Pages wired down"), g("Pages occupied by compressor")
    active, purge = g("Pages active"), g("Pages purgeable")
    anon, filebacked = g("Anonymous pages"), g("File-backed pages")
    app = max(anon - purge, 0) if anon else active
    total = INFO.get("ram") or 0
    swap = sysctl("vm.swapusage")
    sm = re.search(r"used = ([\d.,]+)M", swap)
    level = int(num(sysctl("kern.memorystatus_vm_pressure_level"), 1))
    return {"total": total, "app": app, "wired": wired, "compressed": comp,
            "cached": filebacked + purge, "used": app + wired + comp,
            "swap": num(sm.group(1)) * 1024 * 1024 if sm else 0, "level": level}


def read_pressure_free():
    m = re.search(r"free percentage:\s*(\d+)%", sh(["memory_pressure"], timeout=10))
    return int(m.group(1)) if m else None


def read_disk():
    for path in ("/System/Volumes/Data", "/"):
        try:
            u = shutil.disk_usage(path)
            return {"total": u.total, "free": u.free, "used": u.total - u.free}
        except Exception:
            continue
    return None


def read_battery():
    out = sh(["pmset", "-g", "batt"])
    m = re.search(r"(\d+)%;\s*([^;]+);?\s*([^\n]*)", out)
    if not m:
        return None
    r = re.search(r"(\d+:\d+) remaining", out)
    io = sh(["ioreg", "-rn", "AppleSmartBattery"])

    def gi(k):
        mm = re.search(r'"%s" = (\d+)' % k, io)
        return int(mm.group(1)) if mm else None

    design = gi("DesignCapacity")
    raw = gi("AppleRawMaxCapacity") or gi("NominalChargeCapacity")
    mx = gi("MaxCapacity")
    if not raw and mx and mx > 100:
        raw = mx
    temp = gi("Temperature")
    return {"pct": int(m.group(1)), "state": m.group(2).strip(),
            "ac": "AC Power" in out, "remaining": r.group(1) if r else None,
            "cycles": gi("CycleCount"),
            "health": round(raw / design * 100) if raw and design else None,
            "temp": round(temp / 100, 1) if temp else None}


def read_therm():
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", sh(["pmset", "-g", "therm"]))
    return {"speed_limit": int(m.group(1))} if m else None


def dir_size(path):
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except Exception:
                pass
    return total


# ───────────────────────── демо-данные (для проверки без мака) ─────────────────────────

def demo_tick():
    idle = random.uniform(55, 85)
    S["cpu"] = {"user": (100 - idle) * .6, "sys": (100 - idle) * .4, "idle": idle}
    S["net"] = {"down": random.uniform(2e4, 3e6), "up": random.uniform(5e3, 4e5)}
    names = [("ApplicationsStorageExtension", 83), ("WindowServer", 11), ("kernel_task", 8),
             ("sing-box", 7), ("Telegram", 3.7), ("Claude", 2.6), ("xray", 1.8),
             ("Google Chrome Helper (Renderer)", 1.2), ("Safari", .8), ("update_dyld_sim_shared_cache", .5)]
    S["procs"] = [{"pid": 100 + i, "cpu": c * random.uniform(.8, 1.2), "name": n,
                   "mem": int(random.uniform(.05, .5) * GB)} for i, (n, c) in enumerate(names)]
    S["mem"] = {"total": 8 * GB, "app": 2.7 * GB, "wired": 2.0 * GB, "compressed": .9 * GB,
                "cached": 2.4 * GB, "used": 5.6 * GB, "swap": 0, "level": 1}
    S["pressure_free"] = 46
    S["battery"] = {"pct": 78, "state": "discharging", "ac": False, "remaining": "4:12",
                    "cycles": 612, "health": 84, "temp": 33.4}
    S["therm"] = {"speed_limit": 100}
    S["caches"] = 1.8 * GB


# ───────────────────────── фоновые циклы ─────────────────────────

def fast_loop():
    while True:
        try:
            if DEMO:
                with LOCK:
                    demo_tick()
                time.sleep(2)
            else:
                cpu = read_cpu()              # ~1 сек на замер
                net, procs, mem = read_net(), read_procs(), read_mem()
                with LOCK:
                    if cpu:
                        S["cpu"] = cpu
                    S["net"], S["procs"], S["mem"] = net, procs, mem
                time.sleep(1)
            with LOCK:
                if S["cpu"]:
                    S["cpu_hist"].append(round(100 - S["cpu"]["idle"], 1))
                S["net_hist"].append([S["net"]["down"], S["net"]["up"]])
                hot_now = {p["name"] for p in S["procs"] if p["cpu"] >= 40}
                for n in list(HOT):
                    if n not in hot_now:
                        HOT.pop(n)
                for n in hot_now:
                    HOT[n] = HOT.get(n, 0) + 1
        except Exception as e:
            print("fast_loop:", e, file=sys.stderr)
            time.sleep(2)


def slow_loop():
    while True:
        if not DEMO:
            try:
                pf, bat, th = read_pressure_free(), read_battery(), read_therm()
                with LOCK:
                    S["pressure_free"], S["battery"], S["therm"] = pf, bat, th
            except Exception as e:
                print("slow_loop:", e, file=sys.stderr)
        time.sleep(15)


def caches_loop():
    while True:
        if not DEMO:
            size = dir_size(os.path.expanduser("~/Library/Caches"))
            with LOCK:
                S["caches"] = size
        time.sleep(600)


# ───────────────────────── рекомендации ─────────────────────────

STORAGE = ("Настройки пересчитывают хранилище",
           "Открыто окно «Хранилище» — оно перебирает каждый файл на диске и грузит процессор. "
           "Закрой Системные настройки (Cmd+Q) — нагрузка уйдёт сразу.", "quit_settings")
SPOTLIGHT = ("Spotlight перестраивает индекс",
             "Так бывает после обновления или большого копирования файлов. Займёт от получаса до пары часов — "
             "не мешай, потом поиск будет летать.", None)
VPN = ("VPN нагружает процессор",
       "Ядро VPN шифрует весь трафик. Во время большой загрузки это нормально. Если ничего не качается — "
       "переподключи VPN или смени сервер в Happ.", "vpn")
SIM = ("Остатки симулятора iPhone",
       "Служба симулятора висит в памяти без дела. Перезагрузка мака её уберёт.", None)
ICLOUD = ("Идёт синхронизация iCloud",
          "iCloud Drive выгружает или скачивает файлы. Закончит сам; если затянулось — проверь, не синхронизируется "
          "ли что-то огромное.", None)
PHOTOS = ("Фото разбирает медиатеку",
          "Распознаёт лица и объекты на снимках. Разовая фоновая задача — закончит сама, чаще всего ночью.", None)
XPROTECT = ("Встроенная защита проверяет файлы",
            "macOS проверяет недавно скачанные программы и файлы. Пара минут — и успокоится.", None)

KNOWN = {
    "ApplicationsStorageExtension": STORAGE, "OtherUsersStorageExtension": STORAGE,
    "MailStorageManagement": STORAGE, "StorageManagementService": STORAGE,
    "mds": SPOTLIGHT, "mds_stores": SPOTLIGHT, "mdworker": SPOTLIGHT, "mdworker_shared": SPOTLIGHT,
    "corespotlightd": SPOTLIGHT, "mdsync": SPOTLIGHT,
    "kernel_task": ("Мак сдерживает перегрев",
                    "kernel_task нарочно занимает процессор, чтобы снизить нагрев. Проверь, что у Mac mini свободны "
                    "решётки снизу и сзади, и закрой тяжёлые задачи.", "activity"),
    "WindowServer": ("Высокая нагрузка на графику",
                     "WindowServer рисует всё, что на экране. Помогают меньше окон и вкладок с видео и "
                     "«Уменьшить прозрачность» в Настройки → Универсальный доступ → Дисплей.", None),
    "sing-box": VPN, "xray": VPN, "v2ray": VPN, "openvpn": VPN,
    "photoanalysisd": PHOTOS, "mediaanalysisd": PHOTOS,
    "backupd": ("Идёт резервная копия Time Machine",
                "Пусть доделает — прерывать бэкап не стоит. После него нагрузка спадёт.", None),
    "softwareupdated": ("Скачивается обновление macOS",
                        "Фоновая загрузка. Когда всё будет готово, система сама предложит установить.", None),
    "update_dyld_sim_shared_cache": SIM,
    "bird": ICLOUD, "cloudd": ICLOUD, "fileproviderd": ICLOUD,
    "XprotectService": XPROTECT, "XProtect": XPROTECT, "syspolicyd": XPROTECT,
    "Google Chrome Helper (Renderer)": ("Вкладка Хрома ест процессор",
                                        "Какая-то страница крутит тяжёлый скрипт или видео. В Хроме: Окно → "
                                        "Диспетчер задач — там видно и можно закрыть прожорливую вкладку.", None),
    "com.apple.WebKit.WebContent": ("Страница в Сафари ест процессор",
                                    "Какая-то вкладка гоняет скрипты. Закрой лишние — особенно с видео и рекламой.",
                                    None),
    "steamwebhelper": ("Steam работает в фоне",
                       "Стим обновляет игры или держит магазин открытым. Если не играешь — закрой его "
                       "полностью: Steam → Выход.", None),
}
VPN_CORES = {"sing-box", "xray", "v2ray", "openvpn", "wireguard-go", "tun2socks"}
BROWSERS = {"Safari": "Safari", "Google Chrome": "Chrome", "Opera": "Opera",
            "Yandex": "Яндекс.Браузер", "firefox": "Firefox", "Arc": "Arc"}

LEVEL_ORDER = {"crit": 0, "warn": 1, "info": 2, "tip": 3, "ok": 4}
PENALTY = {"crit": 25, "warn": 12, "info": 3, "tip": 0, "ok": 0}
APPLE_SILICON = "Apple" in (INFO.get("cpu") or "") or os.uname().machine == "arm64"


def fmt_gb(b):
    return ("%.1f ГБ" % (b / GB)).replace(".", ",")


def build_recs(snap):
    recs, seen = [], set()

    def add(level, title, text, act=None):
        if title not in seen:
            seen.add(title)
            recs.append({"level": level, "title": title, "text": text, "act": act})

    procs = snap["procs"]
    names = {p["name"] for p in procs}
    x = snap.get("x") or {}

    # интернет
    if x and x.get("online") is False:
        add("crit", "Нет подключения к интернету",
            "Мак не достучался до сети. Проверь роутер и кабель или Wi-Fi; если включён VPN — переподключи его.",
            "network")

    # процессор
    cpu = snap["cpu"]
    if cpu:
        load = 100 - cpu["idle"]
        if load > 85:
            add("warn", "Процессор на пределе — %d%%" % load,
                "Мак работает на всех ядрах. Глянь «Топ процессора»: обычно виновник один, его и закрой.", "activity")
    for p in sorted(procs, key=lambda q: -q["cpu"])[:8]:
        if p["cpu"] < 40 or HOT.get(p["name"], 0) < 3 or p["pid"] == os.getpid():
            continue
        if p["name"] in KNOWN:
            t, txt, act = KNOWN[p["name"]]
            add("warn", t, txt, act)
        else:
            add("warn", "%s занимает %d%% процессора" % (p["name"], p["cpu"]),
                "Если программа сейчас не нужна — закрой её. Зависла — Мониторинг системы → выбери её → ✕ → "
                "«Завершить принудительно».", "activity")

    # видеоядро
    g = x.get("gpu") or {}
    gh = x.get("gpu_hist") or []
    if g and len(gh) >= 5 and min(gh[-5:]) >= 90:
        add("info", "Видеоядро загружено на %d%%" % g["util"],
            "Графику грузит игра, видео или тяжёлая анимация. Если ничего такого не открыто — проверь вкладки "
            "браузера.")

    # память
    mem = snap["mem"]
    if mem:
        if mem["level"] >= 4:
            add("crit", "Оперативная память на пределе",
                "Мак активно пишет память на диск — отсюда подвисания. Закрой пару тяжёлых программ или вкладок: "
                "в «Топе памяти» видно, кто занимает больше всех.", "activity")
        elif mem["level"] == 2:
            add("warn", "Памяти впритык",
                "Свободной памяти почти нет, система сжимает данные на лету. Закрой то, чем не пользуешься, — "
                "в первую очередь браузер с десятками вкладок.", "activity")
        if mem["swap"] > (4 if APPLE_SILICON else 2) * GB:
            add("info", "Своп разросся до %s" % fmt_gb(mem["swap"]),
                "Система регулярно выгружает память на диск. Не опасно, но замедляет. Перезагрузка обнулит своп, "
                "а меньше вкладок не даст ему расти.")

    # диск
    disk = snap["disk"]
    if disk and disk["total"]:
        free_pct = disk["free"] / disk["total"] * 100
        if free_pct < 10:
            add("crit", "Диск почти заполнен: свободно %s" % fmt_gb(disk["free"]),
                "Когда места меньше десятой части, macOS тормозит и может не поставить обновления. Почисти Загрузки, "
                "Корзину и старые видео или открой «Хранилище».", "storage")
        elif free_pct < 15:
            add("warn", "Место заканчивается: свободно %s" % fmt_gb(disk["free"]),
                "Пора навести порядок: Загрузки и большие файлы удобно смотреть в Настройки → Основные → Хранилище.",
                "storage")

    # батарея (если есть)
    bat = snap["battery"]
    if bat:
        if bat["pct"] <= 15 and not bat["ac"]:
            add("crit", "Заряд %d%%" % bat["pct"], "Подключи зарядку, иначе мак скоро уснёт сам.")
        if bat.get("health") and bat["health"] < 80:
            add("warn", "Аккумулятор изношен: %d%%" % bat["health"],
                "Ёмкость заметно просела. Если мак быстро садится — пора заменить батарею в сервисе.")
        if bat.get("temp") and bat["temp"] >= 40:
            add("warn", "Батарея горячая: %.0f°C" % bat["temp"], "Убери мак с мягкой поверхности и закрой тяжёлые задачи.")

    # перегрев
    th_state = x.get("thermal")
    if th_state is not None and th_state >= 2:
        add("crit" if th_state >= 3 else "warn", "Мак горячий" if th_state == 2 else "Мак перегрелся",
            "Система уже снижает частоты, чтобы остыть. Закрой тяжёлые задачи и проверь, что вентиляции ничего не "
            "мешает.", "activity")
    th = snap["therm"]
    if th and th["speed_limit"] < 100:
        add("crit" if th["speed_limit"] < 70 else "warn",
            "Процессор сбавил частоту до %d%%" % th["speed_limit"],
            "Мак перегрелся и тормозит сам себя. Дай ему остыть и проверь, не забиты ли решётки пылью.")

    # аптайм
    if INFO.get("boot"):
        days = (time.time() - INFO["boot"]) / 86400
        if APPLE_SILICON and days > 14:
            add("info", "Без перезагрузки %d %s" % (days, plural(days, ("день", "дня", "дней"))),
                "На Apple Silicon это не критично, но перезагрузка раз в пару недель освобождает память и ставит "
                "отложенные обновления.")
        elif not APPLE_SILICON and days > 7:
            add("warn", "Без перезагрузки %d %s" % (days, plural(days, ("день", "дня", "дней"))),
                "На Intel-маках копятся своп и подвисшие процессы. Перезагрузись — это бесплатное ускорение.")

    # кэши
    if snap["caches"] and snap["caches"] > 5 * GB:
        add("info", "Кэши разрослись до %s" % fmt_gb(snap["caches"]),
            "Это временные файлы программ. Больше всего обычно у браузеров и Телеграма: в Хроме — Cmd+Shift+Delete, "
            "в Телеграме — Настройки → Данные и память → Использование памяти.")

    # VPN
    v = snap.get("vpn") or {}
    if v.get("installed") and not v.get("on"):
        add("warn", "VPN не подключён",
            "Защищённого соединения сейчас нет. Нажми «Подключить VPN» или скажи: «Джарвис, подключи VPN».", "vpn")

    # периферия
    for d in x.get("peripherals") or []:
        if d["pct"] <= 15:
            add("warn", "%s: заряд %d%%" % (d["name"], d["pct"]),
                "Пора поставить на зарядку — скоро сядет в самый неподходящий момент.")

    # браузеры
    open_br = sorted({b for k, b in BROWSERS.items() if k in names})
    if len(open_br) >= 2:
        add("info", "Открыто сразу %d %s: %s" % (len(open_br), plural(len(open_br), ("браузер", "браузера", "браузеров")),
                                                 ", ".join(open_br)),
            "Каждый держит в памяти свои вкладки и процессы. Оставь один основной — освободится до пары гигабайт.")

    # симулятор
    if "update_dyld_sim_shared_cache" in names:
        add("info", SIM[0], SIM[1])

    # защита — подсказки без штрафа к индексу
    sec = x.get("security") or {}
    if sec.get("firewall") is False:
        add("tip", "Брандмауэр выключен",
            "Дома это не страшно, но включить его — дело минуты: Настройки → Сеть → Брандмауэр.", "firewall")
    if sec.get("filevault") is False:
        add("tip", "Диск не зашифрован",
            "FileVault защитит данные, если мак украдут. Включается в Настройки → Конфиденциальность и "
            "безопасность → FileVault.", "filevault")

    if not any(r["level"] in ("crit", "warn") for r in recs):
        add("ok", "Все системы в норме",
            "Нагрузка спокойная, памяти и места хватает. Мак в отличной форме, Кристофер.")

    recs.sort(key=lambda r: LEVEL_ORDER[r["level"]])
    score = 100 - sum(PENALTY[r["level"]] for r in recs)
    return recs, max(5, min(100, score))


# ───────────────────────── голос (Silero, нейросеть, локально) ─────────────────────────
# Сам синтез, эмоции и эффекты — в jarvis_voice.py. Здесь — настройки и связь с остальным ядром.

SETTINGS_DEFAULT = {"voice": dict(JV.DEFAULT_VOICE), "pet_voice": dict(JV.DEFAULT_PET_VOICE),
                    "talk": "normal", "quiet_until": 0, "weather_city": "", "events": True, "theme": "arc"}


def settings():
    st = load_json("settings.json", {})
    out = {}
    for k, v in SETTINGS_DEFAULT.items():
        if isinstance(v, dict):
            out[k] = dict(v, **(st.get(k) if isinstance(st.get(k), dict) else {}))
        else:
            out[k] = st.get(k, v)
    return out


def save_settings(patch):
    with _lock:
        st = settings()
        for k, v in (patch or {}).items():
            if k not in SETTINGS_DEFAULT:
                continue
            if isinstance(SETTINGS_DEFAULT[k], dict) and isinstance(v, dict):
                st[k] = dict(st[k], **{kk: vv for kk, vv in v.items() if kk in SETTINGS_DEFAULT[k]})
            else:
                st[k] = v
        save_json("settings.json", st)
    return st


def voice_prof(kind="jarvis"):
    return settings()["pet_voice" if kind == "pet" else "voice"]


def synthesize(text, emotion=None, kind="jarvis"):
    """Путь к WAV с фразой (кэшируется) или None."""
    if DEMO:
        return None
    return JV.synthesize(text, voice_prof(kind), emotion or current_emotion())


def warm_tts():
    """Прогрев: загружаем нейросеть и готовим самые частые фразы заранее."""
    try:
        synthesize("Системы онлайн.", "calm")
        for ph in PHRASES:
            synthesize(ph, "calm")
        for cat in ("wake",):
            for ph in BANK.get(cat, [])[:20]:
                synthesize(ph, CAT_EMO.get(cat, "calm"))
    except Exception as e:
        print("warm_tts:", e, file=sys.stderr, flush=True)


PHRASES = ["Как скажешь. Запускаю протокол «Чистый лист». Глушу реактор и закрываю все системы.",
           "Отменяю. Реактор остаётся в строю.",
           "Доброй ночи. Буду ждать.",
           "Перезагружаю систему. Вернусь через минуту — не скучай.",
           "Перезагрузка отменена. Работаем дальше."]


# ───────────────────────── VPN / Happ ─────────────────────────

APP = os.path.expanduser("~/Applications/JARVIS.app")
HAPP_PREFS = os.path.expanduser("~/Library/Preferences/com.happ.plist")
AGENT = os.path.expanduser("~/Library/LaunchAgents/local.jarvis.autostart.plist")
_happ_cache = {"t": 0, "server": None, "installed": False}


def happ_info():
    if time.time() - _happ_cache["t"] > 20:
        _happ_cache["t"] = time.time()
        try:
            import plistlib
            with open(HAPP_PREFS, "rb") as f:
                _happ_cache["server"] = plistlib.load(f).get("Preferences.lastServerName")
        except Exception:
            _happ_cache["server"] = None
        _happ_cache["installed"] = (os.path.exists("/Applications/Happ.app") or
                                    os.path.exists(os.path.expanduser("~/Applications/Happ.app")) or
                                    os.path.exists(HAPP_PREFS))
    return _happ_cache


def socks_open(port=10808):
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=0.3)
        c.close()
        return True
    except Exception:
        return False


_vpn_cache = {"t": 0, "v": (False, False)}


def vpn_connected(fresh=False):
    """Реальное подключение, а не просто запущенный процесс: туннель Happ (172.18.0.1) или прокси xray."""
    if not fresh and time.time() - _vpn_cache["t"] < 3:
        return _vpn_cache["v"]
    tunnel = "172.18.0.1" in sh(["ifconfig"])
    v = ((tunnel or socks_open()), tunnel)
    _vpn_cache.update(t=time.time(), v=v)
    return v


def vpn_status(names):
    if DEMO:
        return {"installed": True, "on": True, "tunnel": True, "happ": True,
                "server": "⚡️Испания", "autostart": True}
    info = happ_info()
    on, tunnel = vpn_connected()
    return {"installed": info["installed"], "on": on, "tunnel": tunnel,
            "happ": "Happ" in names, "server": info["server"],
            "autostart": os.path.exists(AGENT)}


def set_autostart(on):
    """Включает/выключает запуск Happ + JARVIS при входе в систему (LaunchAgent)."""
    if not on:
        try:
            os.remove(AGENT)
        except FileNotFoundError:
            pass
        set_resume(True)
        return False
    set_resume(False)
    script = os.path.join(APP, "Contents", "Resources", "jarvis.py")
    log = os.path.expanduser("~/Library/Logs/JARVIS-autostart.log")
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.jarvis.autostart</string>
<key>ProgramArguments</key><array>
  <string>%s</string><string>%s</string><string>--autostart</string>
</array>
<key>RunAtLoad</key><true/>
<key>StandardOutPath</key><string>%s</string>
<key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (sys.executable, script, log, log)
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    with open(AGENT, "w") as f:
        f.write(plist)
    return True


def ensure_autostart_once():
    """При первой сборке включаем автозапуск (дальше — как выберешь в окне JARVIS)."""
    marker = os.path.join(SUPPORT, "autostart_initialized")
    if os.path.exists(marker):
        if os.path.exists(AGENT):
            set_resume(False)       # чтобы macOS не восстанавливал окна уже с ближайшего выключения
        return
    os.makedirs(SUPPORT, exist_ok=True)
    set_autostart(True)
    open(marker, "w").close()


HAPP_ID = "com.happ"
CLAUDE_ID = "com.anthropic.claudefordesktop"
SAFARI_ID = "com.apple.Safari"
TELEGRAM_ID = "ru.keepcoder.Telegram"
JARVIS_ID = "local.jarvis.monitor"
# что не трогаем при первой зачистке
KEEP_ALWAYS = {"com.apple.finder", HAPP_ID, "su.ffg.happ"}
# что разрешено после запуска по порядку
ALLOWED_AT_LOGIN = KEEP_ALWAYS | {JARVIS_ID, CLAUDE_ID, SAFARI_ID, TELEGRAM_ID}
GUARD_SECONDS = 120


def open_app(bundle_id, fallback_name):
    r = subprocess.run(["open", "-b", bundle_id], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", "-a", fallback_name], capture_output=True)


def open_happ():
    open_app(HAPP_ID, "Happ")


def happ_running():
    return any(a["name"] == "Happ" or a["bundle"] in (HAPP_ID, "su.ffg.happ") for a in foreground_apps())


def click_result():
    try:
        with open(os.path.join(SUPPORT, "click_result")) as f:
            return f.read().strip()
    except Exception:
        return ""


def connect_vpn(log=lambda m: print(m, flush=True)):
    """Включить VPN в Happ: нажимаем кнопку питания, только если VPN точно выключен."""
    if vpn_connected()[0]:
        log("   VPN уже подключён — кнопку не трогаю")
        return True
    if not happ_running():
        open_happ()
        wait_until(happ_running, 25)
    time.sleep(5)                                   # даём Happ прогрузиться (вдруг подключится сам)
    if vpn_connected()[0]:
        log("   Happ подключился сам")
        return True
    for attempt in (1, 2):
        if vpn_connected()[0]:
            return True
        try:
            os.remove(os.path.join(SUPPORT, "click_result"))
        except Exception:
            pass
        subprocess.run(["open", "-n", "-W", APP, "--args", "--connect-happ"], timeout=90)
        res = click_result()
        log("   попытка %d: %s" % (attempt, {
            "clicked": "нажал кнопку в Happ",
            "no_permission": "НЕТ разрешения «Универсальный доступ» для JARVIS",
            "no_window": "не нашёл окно Happ на экране",
            "no_app": "Happ не запустился"}.get(res, res or "нет ответа")))
        if res != "clicked":
            return False
        if wait_until(lambda: vpn_connected()[0], 25, 1):
            log("   VPN подключён")
            return True
    log("   VPN так и не подключился")
    return False


def foreground_apps():
    """Обычные программы с окнами (то, что видно в Доке)."""
    out = sh(["lsappinfo", "list"], timeout=10)
    apps = []
    for block in re.split(r"\n\s*\d+\) ", "\n" + out):
        mp = re.search(r'pid = (\d+) type="(\w+)"', block)
        if not mp or mp.group(2) != "Foreground":
            continue
        mb = re.search(r'bundleID="([^"]*)"', block)
        mn = re.match(r'"([^"]*)"', block)
        apps.append({"name": mn.group(1) if mn else "?", "bundle": mb.group(1) if mb else "",
                     "pid": int(mp.group(1))})
    return apps


def app_running(bundle_id):
    return any(a["bundle"] == bundle_id for a in foreground_apps())


def quit_apps(keep, force_after=4):
    targets = [a for a in foreground_apps()
               if a["bundle"] and a["bundle"] not in keep and a["name"] != "Happ" and a["pid"] != os.getpid()]
    for a in targets:
        try:
            os.kill(a["pid"], signal.SIGTERM)
        except Exception:
            pass
    if not targets:
        return
    time.sleep(force_after)
    alive = {a["pid"] for a in foreground_apps()}
    stubborn = []
    for a in targets:
        if a["pid"] in alive:
            try:
                os.kill(a["pid"], signal.SIGKILL)
                stubborn.append(a["name"])
            except Exception:
                pass
    msg = "  закрыл лишнее: " + ", ".join(a["name"] for a in targets)
    if stubborn:
        msg += "  (добил: " + ", ".join(stubborn) + ")"
    print(msg, flush=True)


def set_resume(enabled):
    """Восстановление окон после перезагрузки (галочка «Снова открывать окна при входе»)."""
    val = "true" if enabled else "false"
    sh(["defaults", "write", "com.apple.loginwindow", "TALLogoutSavesState", "-bool", val])
    sh(["defaults", "write", "com.apple.loginwindow", "LoginwindowLaunchesRelaunchApps", "-bool", val])


def wait_until(check, timeout, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def autostart():
    """Вход в систему: зачистка → VPN → JARVIS → Claude → Safari → Telegram → сторож."""
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)
    log("автозапуск")
    set_resume(False)
    time.sleep(4)                                   # даём системе прогрузиться
    log("   открыто при входе: " + ", ".join(a["name"] for a in foreground_apps()))
    quit_apps(KEEP_ALWAYS)                          # всё, что открылось само

    # 1. VPN
    if happ_info()["installed"]:
        log("1/5 Happ")
        open_happ()
        wait_until(happ_running, 25)
        connect_vpn(log)
    quit_apps(KEEP_ALWAYS)

    # 2. JARVIS
    log("2/5 JARVIS")
    subprocess.Popen(["open", APP])
    wait_until(server_running, 40)
    quit_apps(KEEP_ALWAYS | {JARVIS_ID})

    # 3. Claude
    log("3/5 Claude")
    open_app(CLAUDE_ID, "Claude")
    wait_until(lambda: app_running(CLAUDE_ID), 40)
    time.sleep(3)
    quit_apps(KEEP_ALWAYS | {JARVIS_ID, CLAUDE_ID})

    # 4. Safari
    log("4/5 Safari")
    open_app(SAFARI_ID, "Safari")
    wait_until(lambda: app_running(SAFARI_ID), 30)
    time.sleep(2)
    quit_apps(KEEP_ALWAYS | {JARVIS_ID, CLAUDE_ID, SAFARI_ID})

    # 5. Telegram
    log("5/5 Telegram")
    open_app(TELEGRAM_ID, "Telegram")

    # сторож: ловим запоздавшие автозагрузки
    end = time.time() + GUARD_SECONDS
    while time.time() < end:
        time.sleep(3)
        quit_apps(ALLOWED_AT_LOGIN)
    log("   в итоге открыто: " + ", ".join(a["name"] for a in foreground_apps()))
    log("   VPN: " + ("подключён" if vpn_connected()[0] else "НЕ подключён"))
    log("готово")


def shutdown_mac():
    """Штатное выключение: macOS сам закроет программы (и спросит про несохранённое)."""
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to shut down'],
                       capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return True, ""
    # запасной вариант — через loginwindow
    r2 = subprocess.run(["osascript", "-e", 'tell application "loginwindow" to «event aevtrsdn»'],
                        capture_output=True, text=True, timeout=30)
    if r2.returncode == 0:
        return True, ""
    return False, (r.stderr or r2.stderr or "").strip()[:300]


def restart_mac():
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to restart'],
                       capture_output=True, text=True, timeout=30)
    return (r.returncode == 0), (r.stderr or "").strip()[:300]


# ───────────────────────── голосовые команды ─────────────────────────

def plural(n, forms):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


APPS = {"telegram": ("Telegram", "Телеграм"), "safari": ("Safari", "Сафари"),
        "claude": ("Claude", "Клод"), "happ": ("Happ", "Хапп")}


def status_phrase(s=None):
    s = s or snapshot()
    parts = []
    sc = s["score"]
    parts.append("Индекс здоровья %d." % sc)
    if s.get("cpu"):
        load = round(100 - s["cpu"]["idle"])
        parts.append("Процессор загружен на %d%%." % load)
    x = s.get("x") or {}
    if x.get("gpu"):
        parts.append("Видеоядро — %d%%." % x["gpu"]["util"])
    if s.get("mem"):
        m = s["mem"]
        parts.append("Память: " + ("с запасом." if m["level"] < 2 else "впритык." if m["level"] == 2 else "на пределе."))
    if s.get("disk"):
        gb = round(s["disk"]["free"] / 1e9)
        parts.append("На диске свободно %d %s." % (gb, plural(gb, ("гигабайт", "гигабайта", "гигабайт"))))
    b = s.get("battery")
    if b:
        parts.append("Батарея %d%%%s." % (b["pct"], ", заряжается" if b["state"] == "charging" else ""))
    v = s.get("vpn") or {}
    if v.get("installed"):
        parts.append("VPN подключён." if v.get("on") else "VPN выключен.")
    if x.get("online") is False:
        parts.append("Интернета нет.")
    warns = [r for r in s["recs"] if r["level"] in ("crit", "warn") and not r["title"].startswith("VPN")]
    if warns:
        parts.append("Главное: " + warns[0]["title"].rstrip(".") + ".")
    return " ".join(parts)


def status_reply():
    s = snapshot()
    bad = [r for r in s["recs"] if r["level"] in ("crit", "warn")]
    intro = line("status_intro") or "Докладываю."
    outro = line("status_bad" if bad else "status_good") or ""
    emo = "concerned" if bad and s["score"] < 70 else "proud" if s["score"] >= 90 else "calm"
    return {"say": " ".join(x for x in (intro, status_phrase(s), outro) if x), "emotion": emo,
            "pet": {"cmd": "scan"}}


def voice_command(body):
    cmd = body.get("cmd")
    if cmd == "status":
        return status_reply()
    if cmd == "action":
        return action_command(str(body.get("name", "")))
    if cmd == "vpn":
        if DEMO:
            return "Подключаю VPN."
        on, _ = vpn_connected()
        if on:
            srv = happ_info().get("server")
            return "VPN уже подключён" + (", сервер " + srv.replace("⚡️", "").strip() if srv else "") + "."
        threading.Thread(target=connect_vpn, daemon=True).start()
        return "Подключаю VPN. Это займёт несколько секунд."
    if cmd == "open":
        app = APPS.get(str(body.get("app", "")))
        if not app:
            return "Не понял, что открыть."
        if not DEMO:
            subprocess.Popen(["open", "-a", app[0]])
        return "Открываю " + app[1] + "."
    if cmd == "article":
        return article_command(body)
    if cmd == "text":
        return text_command(str(body.get("text", "")))
    return "Не знаю такой команды."


# ───────────────────────── статьи для Дзена ─────────────────────────
# Каждый канал живёт в своей папке (настраивается в ~/JARVIS/Каналы.json).
# Статья — файл .md в отдельной подпапке темы: первая строка «# Заголовок», дальше текст в Markdown.
# Пишет их Claude по заказу, JARVIS кладёт в буфер и вставляет на Дзене.

JARVIS_DIR = os.path.expanduser("~/JARVIS")
CHANNELS_FILE = os.path.join(JARVIS_DIR, "Каналы.json")
_art = {"channel": None, "seen": None, "news": deque(maxlen=5)}
DEFAULT_CHANNELS = {
    "_как это работает": "Каждый ключ — канал на Дзене. алиасы — как название может услышать распознавание речи; "
                         "сессия — название сессии в Claude; статьи — куда класть статьи (каждая в своей подпапке); "
                         "обложки — куда класть обложки; метка_обложки — если обложки лежат вперемешку с другими "
                         "картинками, JARVIS берёт только файлы с этим словом в названии; описание — пара фраз о канале "
                         "для заказа статьи; подпапка — как называть папку новой статьи; стиль_обложки — каким должен "
                         "быть стиль ({статьи} и {обложки} подставятся); обложка_в_статье — true, если обложка лежит "
                         "в папке статьи файлом 01_обложка.png.",
    "Мой канал": {"алиасы": ["мой канал"], "сессия": "Статьи", "статьи": "~/Documents/Дзен/Статьи",
                  "обложки": "~/Documents/Дзен/Обложки", "метка_обложки": "",
                  "описание": "Канал о технологиях: простым языком, с примерами из жизни."},
    "Буфер": {"сессия": "JARVIS"},
}


def channel_cfg():
    try:
        with open(CHANNELS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
        try:
            os.makedirs(JARVIS_DIR, exist_ok=True)
            with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CHANNELS, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    except Exception:
        data = {}
    src = data if any(isinstance(v, dict) for v in data.values()) else DEFAULT_CHANNELS
    cfg = {k: v for k, v in src.items() if isinstance(v, dict) and not k.startswith("_")}
    cfg.setdefault("Буфер", {"сессия": "JARVIS"})
    return cfg


def channels():
    """Каналы статей (без «Буфера») → варианты, как их может услышать распознавание."""
    out = {}
    for name, v in channel_cfg().items():
        if name != "Буфер":
            al = [str(a).lower() for a in (v.get("алиасы") or [])]
            out[name] = tuple(dict.fromkeys([name.lower()] + al))
    return out


def ch_dir(ch, kind):
    p = channel_cfg().get(ch, {}).get(kind) or ""
    return os.path.expanduser(p) if p else ""


def short_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


def claude_sessions():
    return {k: v.get("сессия") or k for k, v in channel_cfg().items()}


def ensure_article_dirs():
    try:
        os.makedirs(BUFFER_DIR, exist_ok=True)
        for ch in channels():
            for kind in ("статьи", "обложки"):
                d = ch_dir(ch, kind)
                if d and os.path.isdir(os.path.dirname(d)):   # создаём только последнюю папку, не всю цепочку
                    os.makedirs(d, exist_ok=True)
    except Exception:
        pass


def walk_files(root, exts, depth=2):
    """Файлы с нужными расширениями в папке и подпапках (до depth уровней)."""
    out = []
    if not root or not os.path.isdir(root):
        return out
    base = root.rstrip("/").count("/")
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        if cur.count("/") - base >= depth:
            dirs[:] = []
        for n in files:
            if n.lower().endswith(exts) and not n.startswith((".", "_")):
                out.append(os.path.join(cur, n))
    return out


def find_channel(text):
    t = " " + text.lower().replace("ё", "е") + " "
    for ch, keys in channels().items():
        for k in keys:
            k = k.replace("ё", "е")
            if re.search(r"(?<![а-яa-z])" + re.escape(k), t):
                return ch
    return None


def list_articles(channel=None):
    out = []
    for ch in ([channel] if channel else list(channels())):
        for p in walk_files(ch_dir(ch, "статьи"), (".md",)):
            try:
                out.append((os.path.getmtime(p), p, ch))
            except Exception:
                pass
    out.sort(reverse=True)
    return out


def read_article(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().replace("\r\n", "\n").strip().split("\n")
    title = ""
    if lines and lines[0].lstrip().startswith("#"):
        title = lines.pop(0).lstrip("#").strip()
    elif lines:
        title = lines.pop(0).strip()
    return title, "\n".join(lines).strip()


def _inline(s, html):
    import html as H
    if html:
        s = H.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
        return s
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", s)
    return re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r"\1 (\2)", s)


def md_render(md):
    """Markdown → (html, обычный текст). Подзаголовки, жирный, курсив, списки, цитаты."""
    html, plain, lst = [], [], None
    paras = re.split(r"\n\s*\n", md.strip())
    for p in paras:
        ls = [l.rstrip() for l in p.split("\n") if l.strip()]
        if not ls:
            continue
        m = re.match(r"^(#{2,6})\s+(.*)", ls[0])
        if m and len(ls) == 1:
            lvl = min(len(m.group(1)), 3)
            html.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2), True), lvl))
            plain.append(_inline(m.group(2), False))
            continue
        if all(re.match(r"^\s*([-*•]|\d+[.)])\s+", l) for l in ls):
            tag = "ol" if re.match(r"^\s*\d", ls[0]) else "ul"
            items = [re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", l) for l in ls]
            html.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % _inline(i, True) for i in items), tag))
            plain.append("\n".join(("— " if tag == "ul" else "%d. " % (k + 1)) + _inline(i, False)
                                   for k, i in enumerate(items)))
            continue
        if all(l.lstrip().startswith(">") for l in ls):
            q = " ".join(l.lstrip()[1:].strip() for l in ls)
            html.append("<blockquote>%s</blockquote>" % _inline(q, True))
            plain.append(_inline(q, False))
            continue
        html.append("<p>%s</p>" % "<br>".join(_inline(l, True) for l in ls))
        plain.append("\n".join(_inline(l, False) for l in ls))
    return "".join(html), "\n\n".join(plain)


def set_clipboard(plain, html=None):
    """Кладёт в буфер обычный текст и (если есть) HTML — Дзен подхватит подзаголовки и жирный."""
    if DEMO:
        return True
    os.makedirs(SUPPORT, exist_ok=True)
    tp, hp = os.path.join(SUPPORT, "clip.txt"), os.path.join(SUPPORT, "clip.html")
    with open(tp, "w", encoding="utf-8") as f:
        f.write(plain)
    if html:
        with open(hp, "w", encoding="utf-8") as f:
            f.write('<meta charset="utf-8">' + html)
    js = ("ObjC.import('AppKit');var pb=$.NSPasteboard.generalPasteboard;pb.clearContents;"
          "function rd(p){return $.NSString.stringWithContentsOfFileEncodingError($(p),4,null);}"
          + ("pb.setStringForType(rd(%s),$.NSPasteboardTypeHTML);" % json.dumps(hp) if html else "")
          + "pb.setStringForType(rd(%s),$.NSPasteboardTypeString);'ok'" % json.dumps(tp))
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:                      # запасной путь — хотя бы обычный текст
        p = subprocess.run(["pbcopy"], input=plain, text=True, env=dict(os.environ, LANG="ru_RU.UTF-8"))
        return p.returncode == 0
    return True


def frontmost_app():
    try:
        r = subprocess.run(["osascript", "-e", 'tell application "System Events" to get name of first '
                            'application process whose frontmost is true'],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def press_paste():
    """Cmd+V в активное окно. Нужен Универсальный доступ для JARVIS (он уже выдан для VPN)."""
    front = frontmost_app()
    if not front or front.lower().startswith(("jarvis", "python")):
        return False
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0


def appendlog(msg):
    try:
        with open(os.path.join(SUPPORT, "jarvis.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.ctime(), msg))
    except Exception:
        pass


def pick_article(text=""):
    ch = find_channel(text) or None
    arts = list_articles(ch)
    if not arts and _art["channel"] and not ch:
        arts = list_articles(_art["channel"])
    return arts[0] if arts else None


def article_command(body):
    action = body.get("action")
    text = str(body.get("text", "")).lower().replace("ё", "е")
    # распознавание отдаёт всё сказанное за сессию — берём хвост с последней команды
    for key in (("стать",) if action in ("order", "pick") else ("встав",)):
        k = text.rfind(key)
        if k >= 0:
            text = text[k:]
    ensure_article_dirs()
    if action == "order":
        ch = find_channel(text)
        if not ch:
            names = list(channels())
            if not names:
                return "Сначала добавь свой канал в файл Каналы.json в папке JARVIS."
            return "Для какого канала? Скажи: напиши статью в " + " или в ".join(names[:3]) + "."
        m = re.search(r"\b(?:про|о|об|на тему)\s+(.+)$", text)
        topic = m.group(1).strip(" .") if m else ""
        _art["channel"] = ch
        adir = short_path(ch_dir(ch, "статьи"))
        where = "в новую подпапку %s/%s" % (adir, channel_cfg().get(ch, {}).get("подпапка") or "<короткая тема>")
        save = (" Сохрани статью на мой мак %s файлом «<Тема>.md»: первая строка — «# Заголовок», "
                "дальше текст с подзаголовками ##." % where)
        desc = channel_cfg().get(ch, {}).get("описание") or ""
        brief = (" " + desc) if desc else ""
        if topic:
            prompt = "Напиши статью для моего канала «%s» на Дзене на тему: %s.%s" % (ch, topic, brief) + save
        else:
            prompt = ("Напиши статью для моего канала «%s» на Дзене.%s Тему выбери сам: открой Дзен-Студию "
                      "в браузере (я там залогинен), посмотри статистику канала — какие статьи лучше открывают "
                      "и дочитывают, о чём уже писали — и что сейчас в тренде по теме канала. Не повторяй "
                      "уже вышедшие темы. Учти заметки из %s/_о канале.md, если файл есть, и допиши "
                      "туда новые выводы по статистике." % (ch, brief, adir) + save)
        try:
            os.makedirs(JARVIS_DIR, exist_ok=True)
            with open(os.path.join(JARVIS_DIR, "заказы.txt"), "a", encoding="utf-8") as f:
                f.write("%s · %s · %s\n" % (time.strftime("%d.%m %H:%M"), ch, topic or "тема на выбор"))
        except Exception:
            pass
        set_clipboard(prompt)
        session = claude_sessions().get(ch, ch)
        return {"say": "Принял, статья для канала %s. Открываю сессию %s в Клоде и отправляю заказ. "
                       "Как статья ляжет в папку, я сразу скажу." % (ch, session),
                "claude_session": session}
    if action == "pick":
        ch = find_channel(text)
        if ch:
            _art["channel"] = ch
        a = pick_article(text)
        if not a:
            return "В папке %s пока пусто." % (ch or "Статьи")
        title, body_md = read_article(a[1])
        html, plain = md_render(body_md)
        set_clipboard(plain, html)
        return "Взял статью «%s» для %s. Текст в буфере, заголовок дам по команде «вставь заголовок»." % (title, a[2])
    if action in ("paste", "title"):
        a = pick_article(text)
        if not a:
            return "Статей пока нет. Скажи: напиши статью в <канал>, и я оформлю заказ."
        title, body_md = read_article(a[1])
        if action == "title":
            set_clipboard(title)
        else:
            html, plain = md_render(body_md)
            set_clipboard(plain, html)
        if DEMO:
            return "Вставляю."
        time.sleep(0.25)
        if press_paste():
            return "Готово, заголовок на месте." if action == "title" else "Вставил. Проверь подзаголовки и картинки."
        return ("Скопировал %s. Переключись на Дзен, встань курсором куда нужно и жми Cmd+V."
                % ("заголовок" if action == "title" else "текст статьи"))
    return "Не понял, что сделать со статьёй."


def articles_loop():
    """Следит за ~/Статьи: новая статья → в буфер; новая обложка; результат из _буфер → в буфер. И объявляет."""
    ensure_article_dirs()
    seen = {"art": None, "cov": None, "buf": None}

    def newest_buffer():
        try:
            fs = [os.path.join(BUFFER_DIR, n) for n in os.listdir(BUFFER_DIR)
                  if n.lower().endswith((".txt", ".md")) and not n.startswith(".")]
            return max(fs, key=os.path.getmtime) if fs else ""
        except Exception:
            return ""

    while True:
        try:
            arts, covs, buf = list_articles(), list_covers(), newest_buffer()
            na, nc = (arts[0][1] if arts else ""), (covs[0][1] if covs else "")
            if seen["art"] is None:
                seen.update(art=na, cov=nc, buf=buf)
            if na and na != seen["art"] and time.time() - arts[0][0] < 600:
                seen["art"] = na
                time.sleep(1.5)               # даём файлу дописаться
                title, body_md = read_article(na)
                html, plain = md_render(body_md)
                set_clipboard(plain, html)
                _art["channel"] = arts[0][2]
                announce("Статья для %s готова: «%s». Текст уже в буфере. На Дзене скажи «вставь заголовок», "
                         "потом «вставь статью». Или просто Cmd+V." % (arts[0][2], title),
                         trick="salute", title="JARVIS · " + arts[0][2])
                bump("articles")
                if time.localtime().tm_hour < 5:
                    unlock("night")
            if nc and nc != seen["cov"] and time.time() - covs[0][0] < 600:
                seen["cov"] = nc
                time.sleep(1.5)
                set_clipboard_image(nc)
                announce("Обложка для %s готова и лежит в буфере. На Дзене скажи «вставь обложку» "
                         "или «покажи обложку»." % covs[0][2], trick="flip", title="JARVIS · обложка")
                bump("covers")
            if buf and buf != seen["buf"] and time.time() - os.path.getmtime(buf) < 600:
                seen["buf"] = buf
                time.sleep(1)
                with open(buf, encoding="utf-8") as f:
                    set_clipboard(f.read().strip())
                announce("Готово, новый текст в буфере. Встань куда нужно и жми Cmd+V.", title="JARVIS · текст")
        except Exception:
            pass
        time.sleep(4)


# ───────────────────────── объявления (голос JARVIS + Марк-0 + уведомление) ─────────────────────────

_voice_q = deque(maxlen=10)       # что скажет JARVIS (окно забирает через /api/announce)
_pet_q = deque(maxlen=10)         # что покажет Марк-0 (/api/pet/news)


def notify(title, text, sound=False):
    if DEMO:
        return
    try:
        subprocess.run(["osascript", "-e", "display notification %s with title %s%s"
                        % (json.dumps(text[:200]), json.dumps(title), ' sound name "Glass"' if sound else "")],
                       timeout=5)
    except Exception:
        pass


def announce(text, trick=None, title="JARVIS", sound=False, voice=True, emotion=None, os_notify=True, ui=None):
    if not text:
        return
    emotion = emotion or current_emotion()
    if os_notify:
        notify(title, text, sound)
    if not voice:
        _pet_q.append({"text": text, "trick": trick, "mood": EMO_TO_PET.get(emotion, "happy")})
        log_line("jarvis", text, emotion, spoken=False)
        return

    def go():
        try:
            synthesize(text, emotion)       # готовим звук заранее, чтобы текст и голос шли вместе
        except Exception:
            pass
        _voice_q.append({"say": text, "emotion": emotion, "ui": ui})
        if trick:
            _pet_q.append({"text": "", "trick": trick})
    threading.Thread(target=go, daemon=True).start()


def pet_trick(name):
    _pet_q.append({"text": "", "trick": name})


# ───────────────────────── мозг: реплики, эмоции, разговорчивость ─────────────────────────
# Банк реплик — jarvis_phrases.json (категория → варианты). Плейсхолдеры {app}, {pct} и т.д.
# Эмоция фразы меняет голос (темп, высоту), цвет реактора в интерфейсе и настроение Марка-0.

def merge_phrases(base, name):
    """Добавляет к базе реплик свои из ~/JARVIS/<name> (тот же формат: категория → список)."""
    try:
        with open(os.path.join(JARVIS_DIR, name), encoding="utf-8") as f:
            extra = json.load(f)
    except Exception:
        return base
    for k, v in (extra.items() if isinstance(extra, dict) else []):
        if isinstance(v, list) and not k.startswith("_"):
            base[k] = list(base.get(k, [])) + [x for x in v if isinstance(x, str)]
    return base


def load_bank():
    for d in (HERE, os.path.dirname(os.path.abspath(__file__))):
        try:
            with open(os.path.join(d, "jarvis_phrases.json"), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return merge_phrases({k: v for k, v in data.items() if isinstance(v, list) and not k.startswith("_")},
                                     "Мои реплики.json")
        except Exception:
            pass
    return merge_phrases({}, "Мои реплики.json")


BANK = load_bank()

CAT_EMO = {
    "wake": "calm", "unknown": "witty", "greet_morning": "happy", "greet_day": "calm", "greet_evening": "warm",
    "greet_night": "tired", "boot": "proud", "goodbye": "warm", "welcome_back": "happy", "how_are_you": "happy",
    "who_are_you": "proud", "thanks_reply": "warm", "praise_reply": "proud", "insult_reply": "witty",
    "love_reply": "warm", "sorry_reply": "warm", "jokes": "witty", "facts": "curious", "cheer_up": "warm",
    "tired_reply": "warm", "compliments": "happy", "bored": "witty", "good_night": "warm",
    "good_morning_reply": "happy", "stark": "proud", "ultron": "concerned", "friday": "witty", "avengers": "proud",
    "alive": "curious", "age": "witty", "chat": "curious", "quiet_on": "calm", "quiet_off": "happy",
    "talk_more": "excited", "talk_less": "calm", "coin_heads": "witty", "coin_tails": "witty", "dice": "witty",
    "random_number": "calm", "mood_happy": "happy", "easter_party": "excited", "easter_ironman": "proud",
    "creator": "proud", "here": "calm",
    "cpu_high": "concerned", "cpu_calm": "calm", "mem_high": "concerned", "mem_ok": "calm",
    "disk_low": "concerned", "net_offline": "alarmed", "net_online": "happy", "net_heavy": "curious",
    "vpn_on": "proud", "vpn_off": "concerned", "app_launch": "calm", "app_quit": "calm",
    "thermal_warn": "alarmed", "uptime_long": "witty", "peripheral_low": "concerned", "drive_mounted": "curious",
    "drive_unmounted": "calm", "score_up": "happy", "score_down": "concerned", "idle_witty": "witty",
    "idle_calm": "calm", "idle_caring": "warm", "idle_curious": "curious", "idle_proud": "proud",
    "idle_system": "calm", "late_night_work": "tired", "long_session": "warm", "weekend_work": "witty",
    "monday": "calm", "weekday_friday": "happy", "scan_intro": "proud", "scan_good": "proud", "scan_bad": "concerned",
    "dashboard_open": "proud", "dashboard_close": "calm", "widget_show": "calm", "widget_hide": "calm",
    "volume_up": "calm", "volume_down": "calm", "volume_set": "calm", "mute": "calm", "unmute": "calm",
    "media_pause": "calm", "media_next": "happy", "media_prev": "calm", "screenshot": "happy",
    "lock_screen": "calm", "display_sleep": "calm", "sleep_mac": "warm", "restart_ask": "concerned",
    "shutdown_ask": "concerned", "shutdown_cancel": "happy", "open_app": "calm", "close_app": "calm",
    "app_not_found": "witty", "search_web": "curious", "weather_cold": "concerned", "weather_hot": "witty",
    "weather_rain": "concerned", "weather_snow": "happy", "weather_nice": "happy", "time_reply": "calm",
    "date_reply": "calm", "reminder_set": "calm", "reminder_fire": "alarmed", "timer_start": "calm",
    "timer_end": "happy", "pomodoro_end": "proud", "achievement": "excited", "article_ready": "proud",
    "error": "concerned", "quiet_hours": "tired", "status_intro": "calm", "status_good": "proud",
    "status_bad": "concerned",
}
for _k in list(BANK):
    if _k.startswith(("mark_", "app_")) and _k not in CAT_EMO:
        CAT_EMO[_k] = "happy" if _k.startswith("mark_") else "calm"

EMOTION_NAMES = {"calm": "спокоен", "happy": "в духе", "witty": "иронизирует", "concerned": "обеспокоен",
                 "alarmed": "тревога", "proud": "гордится", "excited": "воодушевлён", "tired": "ночной режим",
                 "curious": "любопытствует", "warm": "заботится"}
EMO_TO_PET = {"calm": "calm", "happy": "happy", "witty": "playful", "concerned": "care", "alarmed": "alert",
              "proud": "excited", "excited": "excited", "tired": "sleepy", "curious": "curious", "warm": "love"}

_recent_lines = deque(maxlen=400)
_emo = {"name": "calm", "until": 0.0}
_brain = {"score": 100, "last_talk": 0.0, "said_today": 0, "said_day": None, "cmds": 0, "started": time.time()}
_convo = deque(maxlen=80)


def fill(tpl, kw):
    return re.sub(r"\{(\w+)\}", lambda m: str(kw.get(m.group(1), "")), tpl).replace("  ", " ").strip()


def line(cat, **kw):
    pool = BANK.get(cat) or []
    if not pool:
        return None
    if kw:                                          # фразы с плейсхолдерами, которые нечем заполнить, пропускаем
        ok = [l for l in pool if all(k in kw for k in re.findall(r"\{(\w+)\}", l))]
        pool = ok or pool
    else:
        pool = [l for l in pool if "{" not in l] or pool
    fresh = [l for l in pool if l not in _recent_lines] or pool
    tpl = random.choice(fresh)
    _recent_lines.append(tpl)
    return fill(tpl, kw)


def say_cat(cat, emotion=None, fallback="", **kw):
    text = line(cat, **kw) or fill(fallback, kw)
    emo = emotion or CAT_EMO.get(cat, "calm")
    return {"say": text, "emotion": emo}


def set_emotion(name, secs=40):
    if name in JV.EMOTIONS:
        _emo.update(name=name, until=time.time() + secs)


def base_mood():
    h = time.localtime().tm_hour
    if _brain["score"] < 55:
        return "concerned"
    if 1 <= h < 6:
        return "tired"
    return "calm"


def current_emotion():
    return _emo["name"] if time.time() < _emo["until"] else base_mood()


def log_line(who, text, emotion=None, spoken=True):
    if not text:
        return
    _convo.append({"who": who, "text": str(text)[:400], "t": time.time(), "e": emotion, "spoken": spoken})
    if who == "jarvis" and spoken:
        today = time.strftime("%Y-%m-%d")
        if _brain["said_day"] != today:
            _brain.update(said_day=today, said_today=0)
        _brain["said_today"] += 1
        _brain["last_talk"] = time.time()


def talk_mode():
    st = settings()
    if st.get("quiet_until", 0) > time.time():
        return "quiet"
    return st.get("talk") if st.get("talk") in ("quiet", "normal", "chatty") else "normal"


def can_talk(prio=1):
    """prio: 0 — болтовня, 1 — обычное событие, 2 — важное, 3 — тревога (говорим всегда)."""
    if prio >= 3:
        return True
    mode = talk_mode()
    if mode == "quiet":
        return prio >= 2
    h = time.localtime().tm_hour
    if (h < 7 or h >= 23) and prio < 2 and idle_seconds() > 60:
        return False
    if len(_voice_q) >= 2 and prio < 2:
        return False
    gap = {"chatty": 30, "normal": 75}[mode] if prio >= 1 else {"chatty": 90, "normal": 240}[mode]
    return time.time() - _brain["last_talk"] > gap


def speak_event(cat, prio=1, trick=None, voice_ok=True, **kw):
    """Реплика по событию: голосом, если сейчас уместно говорить, иначе — только текстом у Марка."""
    r = say_cat(cat, **kw)
    if not r["say"]:
        return
    set_emotion(r["emotion"], 30)
    if voice_ok and can_talk(prio):
        _brain["last_talk"] = time.time()
        announce(r["say"], trick=trick, emotion=r["emotion"], os_notify=prio >= 3, title="JARVIS")
    else:
        announce(r["say"], trick=trick, emotion=r["emotion"], voice=False, os_notify=False)


def reply(r):
    """Ответ ядра → словарь для окна: say, emotion, ui, pet."""
    if r is None:
        r = {"say": random.choice(UNKNOWN_REPLIES)}
    if isinstance(r, str):
        r = {"say": r}
    if r.get("_logged"):
        return r
    r["_logged"] = True
    r.setdefault("emotion", current_emotion())
    if r.get("say"):
        set_emotion(r["emotion"], 25)
        log_line("jarvis", r["say"], r["emotion"])
    return r


# ───────────────────────── события системы → реплики ─────────────────────────

APP_CATS = {
    "ru.keepcoder.Telegram": "app_telegram", "org.telegram.desktop": "app_telegram", "com.apple.Safari": "app_safari",
    "com.google.Chrome": "app_chrome", "com.anthropic.claudefordesktop": "app_claude",
    "com.valvesoftware.steam": "app_steam", "com.apple.Music": "app_music", "com.apple.dt.Xcode": "app_xcode",
    "com.apple.Terminal": "app_terminal", "com.googlecode.iterm2": "app_terminal", "com.figma.Desktop": "app_figma",
    "com.adobe.Photoshop": "app_photoshop", "us.zoom.xos": "app_zoom",
}
QUIET_APPS = {"com.apple.finder", "local.jarvis.monitor", "com.happ", "su.ffg.happ", "com.apple.loginwindow",
              "com.apple.systempreferences", "com.apple.ScreenSaver.Engine"}


def fmt_uptime_words(sec):
    d, h = int(sec // 86400), int(sec % 86400 // 3600)
    if d:
        return "%d %s %d %s" % (d, plural(d, ("день", "дня", "дней")), h, plural(h, ("час", "часа", "часов")))
    m = int(sec % 3600 // 60)
    return "%d %s %d %s" % (h, plural(h, ("час", "часа", "часов")), m, plural(m, ("минута", "минуты", "минут")))


def events_loop():
    time.sleep(25)
    cool = {}
    st = {"apps": None, "vpn": None, "online": True, "off_n": 0, "on_n": 0, "vols": None, "mem_hi": False,
          "cpu_hot": 0.0, "cpu_was_hot": False, "score_hist": deque(maxlen=60), "active_since": time.time(),
          "away": False}

    def ready(key, secs):
        if time.time() - cool.get(key, 0) > secs:
            cool[key] = time.time()
            return True
        return False

    while True:
        try:
            if not settings().get("events", True):
                time.sleep(10)
                continue
            now = time.time()
            s = snapshot()
            x = s.get("x") or {}
            _brain["score"] = s["score"]

            # процессор: долго горячо → реплика; остыл → облегчение
            load = 100 - s["cpu"]["idle"] if s.get("cpu") else 0
            if load > 85:
                st["cpu_hot"] = st["cpu_hot"] or now
                if now - st["cpu_hot"] > 30 and ready("cpu_high", 900):
                    top = (s.get("top_cpu") or [{"name": "что-то"}])[0]["name"]
                    speak_event("cpu_high", 2, pct="%d%%" % load, proc=top)
                    st["cpu_was_hot"] = True
            elif load < 45:
                if st["cpu_was_hot"] and ready("cpu_calm", 1200):
                    speak_event("cpu_calm", 0)
                st["cpu_hot"], st["cpu_was_hot"] = 0.0, False

            # память
            m = s.get("mem") or {}
            if m.get("level", 1) >= 2:
                if not st["mem_hi"] and ready("mem_high", 1200):
                    speak_event("mem_high", 2)
                st["mem_hi"] = True
            elif st["mem_hi"]:
                st["mem_hi"] = False
                if ready("mem_ok", 1800):
                    speak_event("mem_ok", 0)

            # диск
            d = s.get("disk")
            if d and d["total"] and d["free"] / d["total"] < 0.10 and ready("disk_low", 3 * 3600):
                speak_event("disk_low", 2, free=fmt_gb(d["free"]))

            # интернет (с защитой от дребезга)
            on = x.get("online", True)
            if on:
                st["on_n"] += 1
                st["off_n"] = 0
            else:
                st["off_n"] += 1
                st["on_n"] = 0
            if st["online"] and st["off_n"] >= 2:
                st["online"] = False
                speak_event("net_offline", 3)
            elif not st["online"] and st["on_n"] >= 2:
                st["online"] = True
                speak_event("net_online", 2)

            # VPN
            v = s.get("vpn") or {}
            if v.get("installed"):
                von = bool(v.get("on"))
                if st["vpn"] is not None and von != st["vpn"] and ready("vpn_change", 60):
                    srv = (v.get("server") or "").replace("⚡️", "").strip()
                    speak_event("vpn_on" if von else "vpn_off", 2, server=srv or "по умолчанию")
                st["vpn"] = von

            # программы: запуск и закрытие
            apps = {a["bundle"] or a["name"]: a["name"] for a in foreground_apps()}
            if st["apps"] is not None:
                for b, name in apps.items():
                    if b in st["apps"] or b in QUIET_APPS:
                        continue
                    if ready("app:" + b, 900):
                        cat = APP_CATS.get(b)
                        if cat and BANK.get(cat):
                            speak_event(cat, 1)
                        elif random.random() < 0.5:
                            speak_event("app_launch", 0, app=app_ru(name))
                for b, name in st["apps"].items():
                    if b not in apps and b not in QUIET_APPS and random.random() < 0.3 and ready("quit:" + b, 1800):
                        speak_event("app_quit", 0, app=app_ru(name))
            st["apps"] = apps

            # перегрев
            if (x.get("thermal") or 0) >= 2 and ready("thermal", 1800):
                speak_event("thermal_warn", 3)

            # периферия
            for dv in x.get("peripherals") or []:
                if dv["pct"] <= 15 and ready("per:" + dv["name"], 6 * 3600):
                    speak_event("peripheral_low", 2, device=dv["name"], pct="%d%%" % dv["pct"])

            # внешние диски
            vols = {vv["name"] for vv in x.get("volumes") or []}
            if st["vols"] is not None:
                for n in vols - st["vols"]:
                    speak_event("drive_mounted", 1, name=n)
                for n in st["vols"] - vols:
                    speak_event("drive_unmounted", 0, name=n)
            st["vols"] = vols

            # индекс здоровья резко изменился
            st["score_hist"].append(s["score"])
            if len(st["score_hist"]) >= 30:
                old = st["score_hist"][0]
                if s["score"] - old >= 15 and ready("score", 1800):
                    speak_event("score_up", 0, score=str(s["score"]))
                elif old - s["score"] >= 15 and ready("score", 1800):
                    worst = next((r["title"] for r in s["recs"] if r["level"] in ("crit", "warn")), "")
                    speak_event("score_down", 1, score=str(s["score"]))
                    if worst:
                        set_emotion("concerned", 60)

            # аптайм
            if s.get("uptime") and s["uptime"] > 14 * 86400 and ready("uptime", 86400):
                speak_event("uptime_long", 0, days="%d %s" % (s["uptime"] // 86400,
                                                              plural(s["uptime"] // 86400, ("день", "дня", "дней"))))

            # долгая работа без перерыва
            idle = idle_seconds()
            if idle > 300:
                st["away"] = True
                st["active_since"] = now
            elif st["away"]:
                st["away"] = False
                st["active_since"] = now - idle
                if ready("welcome", 1200):
                    speak_event("welcome_back", 1)
            if now - st["active_since"] > 100 * 60 and ready("long", 45 * 60):
                speak_event("long_session", 1)
        except Exception as e:
            print("events_loop:", e, file=sys.stderr, flush=True)
        time.sleep(5)


def chatter_loop():
    """Болтовня по своей инициативе: наблюдения, шутки, забота. Частота — по режиму разговорчивости."""
    time.sleep(120)
    said_day = {}
    while True:
        mode = talk_mode()
        gap = {"chatty": (140, 300), "normal": (480, 1000)}.get(mode)
        if not gap:
            time.sleep(60)
            continue
        time.sleep(random.uniform(*gap))
        try:
            if idle_seconds() > 120 or not can_talk(0):
                continue
            if _timer["kind"] == "work" and mode != "chatty":
                continue                                    # во время помидора не отвлекаем
            lt = time.localtime()
            h, wd, today = lt.tm_hour, lt.tm_wday, time.strftime("%Y-%m-%d")
            w = [("idle_witty", 4), ("idle_calm", 3), ("idle_caring", 3), ("idle_curious", 2), ("idle_proud", 1),
                 ("idle_system", 3), ("jokes", 2), ("facts", 2)]
            if h < 5 or h >= 23:
                w.append(("late_night_work", 4))
            for cat, cond in (("monday", wd == 0), ("weekday_friday", wd == 4), ("weekend_work", wd >= 5)):
                if cond and said_day.get(cat) != today:
                    w.append((cat, 6))
            cats = [c for c, k in w for _ in range(k)]
            cat = random.choice(cats)
            said_day[cat] = today
            kw = {}
            if cat == "idle_system":
                s = snapshot()
                m = s.get("mem") or {}
                free = s.get("pressure_free")
                kw = {"cpu": "%d%%" % round(100 - s["cpu"]["idle"]) if s.get("cpu") else "немного",
                      "mem": "%d%%" % (free if free is not None else 50), "score": str(s["score"]),
                      "procs": str(s.get("procs_count") or 0),
                      "uptime": fmt_uptime_words(s["uptime"]) if s.get("uptime") else "давно"}
            r = say_cat(cat, **kw)
            if r["say"]:
                _brain["last_talk"] = time.time()
                set_emotion(r["emotion"], 30)
                announce(r["say"], emotion=r["emotion"], os_notify=False)
        except Exception as e:
            print("chatter_loop:", e, file=sys.stderr, flush=True)


def greet_reply(boot=True):
    h = time.localtime().tm_hour
    cat = "greet_night" if h < 5 else "greet_morning" if h < 12 else "greet_day" if h < 18 else \
        "greet_evening" if h < 23 else "greet_night"
    g = say_cat(cat)
    if not boot:
        return g
    b = line("boot") or ""
    s = snapshot()
    _brain["score"] = s["score"]
    bad = [r for r in s["recs"] if r["level"] in ("crit", "warn")]
    tail = ("Обрати внимание: %s." % bad[0]["title"].rstrip(".")) if bad else ""
    return reply({"say": " ".join(x for x in (g["say"], b, tail) if x), "emotion": g["emotion"],
                  "pet": {"cmd": "wave"}})


# ───────────────────────── новые команды: дашборд, Марк, звук, приложения, погода, общение ─────────────────────────

APP_ALIASES = [
    (r"телеграм|телеге|телегу|телега", "Telegram"), (r"сафари", "Safari"), (r"гугл хром|хром", "Google Chrome"),
    (r"клод", "Claude"), (r"финдер|файндер|файлы", "Finder"), (r"музык", "Music"), (r"заметк", "Notes"),
    (r"календар", "Calendar"), (r"почт", "Mail"), (r"настройк", "System Settings"), (r"терминал", "Terminal"),
    (r"стим", "Steam"), (r"фото\b|фотографи", "Photos"), (r"превью|просмотр", "Preview"),
    (r"калькулятор", "Calculator"), (r"мониторинг|монитор систем|диспетчер задач", "Activity Monitor"),
    (r"хапп|хап\b", "Happ"), (r"фигм", "Figma"), (r"фотошоп", "Adobe Photoshop 2025"), (r"зум", "zoom.us"),
    (r"дискорд", "Discord"), (r"ватсап", "WhatsApp"), (r"спотифай", "Spotify"), (r"ношн", "Notion"),
    (r"яндекс браузер", "Yandex"), (r"опер", "Opera"), (r"икс ?код", "Xcode"), (r"текстов\w* редактор", "TextEdit"),
    (r"напоминани", "Reminders"), (r"ап ?стор|эп ?стор", "App Store"), (r"фейс ?тайм", "FaceTime"),
    (r"сообщени", "Messages"), (r"карты", "Maps"), (r"подкаст", "Podcasts"), (r"книг", "Books"),
    (r"контакт", "Contacts"), (r"слак", "Slack"), (r"трелло", "Trello"),
]
SITES = [(r"ютуб", "https://youtube.com"), (r"вк\b|вконтакте", "https://vk.com"), (r"дзен", "https://dzen.ru"),
         (r"яндекс$|яндекс сайт", "https://ya.ru"), (r"гугл$", "https://google.com"),
         (r"кинопоиск", "https://kinopoisk.ru"), (r"яндекс музык", "https://music.yandex.ru")]
FOLDERS = [(r"загрузк", "~/Downloads"), (r"документ", "~/Documents"), (r"рабочий стол", "~/Desktop"),
           (r"программ|приложени", "/Applications"), (r"домашн", "~")]

_RU_LAT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                   ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                    "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def ru2lat(s):
    return "".join(_RU_LAT.get(c, c) for c in s.lower())


_apps_cache = {"t": 0, "list": []}


def installed_apps():
    if time.time() - _apps_cache["t"] > 300:
        out = []
        for d in ("/Applications", "/System/Applications", "/System/Applications/Utilities",
                  os.path.expanduser("~/Applications"), "/Applications/Utilities"):
            try:
                out += [n[:-4] for n in os.listdir(d) if n.endswith(".app")]
            except Exception:
                pass
        _apps_cache.update(t=time.time(), list=sorted(set(out)))
    return _apps_cache["list"]


APP_RU = {"Calculator": "Калькулятор", "Activity Monitor": "Мониторинг системы", "Notes": "Заметки",
          "Calendar": "Календарь", "Mail": "Почта", "System Settings": "Системные настройки", "Music": "Музыка",
          "Photos": "Фото", "Preview": "Просмотр", "Reminders": "Напоминания", "Messages": "Сообщения",
          "Maps": "Карты", "Books": "Книги", "Contacts": "Контакты", "Podcasts": "Подкасты",
          "TextEdit": "Текстовый редактор", "App Store": "Эп Стор", "FaceTime": "Фейстайм", "Terminal": "Терминал",
          "Google Chrome": "Гугл Хром", "Telegram": "Телеграм", "Safari": "Сафари", "Claude": "Клод",
          "Finder": "Файндер", "Steam": "Стим", "Happ": "Хапп", "zoom.us": "Зум", "Weather": "Погода",
          "Clock": "Часы", "Stocks": "Акции", "Home": "Дом", "Freeform": "Freeform", "Shortcuts": "Быстрые команды"}


def app_ru(name):
    return APP_RU.get(name, name)


def resolve_app(q):
    import difflib
    q = q.strip(" .")
    for rx, name in APP_ALIASES:
        if re.search(rx, q):
            if name.startswith("Adobe Photoshop"):
                return next((a for a in installed_apps() if a.startswith("Adobe Photoshop")), None)
            return name
    lat = re.sub(r"[^a-z0-9]", "", ru2lat(q))
    best, score = None, 0.0
    for a in installed_apps():
        al = re.sub(r"[^a-z0-9]", "", a.lower())
        if not al:
            continue
        sc = difflib.SequenceMatcher(None, lat, al).ratio()
        if lat and (lat in al or al in lat):
            sc += 0.4
        if sc > score:
            best, score = a, sc
    return best if score >= 0.62 else None


def open_thing(q):
    for rx, url in SITES:
        if re.search(rx, q):
            if not DEMO:
                subprocess.Popen(["open", url])
            return say_cat("open_app", app=re.sub(r"^https?://", "", url))
    m = re.search(r"папк\w*\s+(.+)|(загрузки|документы|рабочий стол)", q)
    if m:
        qq = m.group(1) or m.group(2)
        for rx, path in FOLDERS:
            if re.search(rx, qq):
                if not DEMO:
                    subprocess.Popen(["open", os.path.expanduser(path)])
                return {"say": "Открываю папку.", "emotion": "calm"}
    app = resolve_app(q)
    if not app:
        return say_cat("app_not_found", app=q, fallback="Не нашёл программу «{app}».")
    if not DEMO:
        subprocess.Popen(["open", "-a", app])
    return say_cat("open_app", app=app_ru(app))


def close_thing(q):
    app = resolve_app(q)
    if not app:
        return say_cat("app_not_found", app=q, fallback="Не нашёл программу «{app}».")
    if app in ("Finder",):
        return {"say": "Финдер закрыть нельзя — на нём держится весь рабочий стол.", "emotion": "witty"}
    if not DEMO:
        subprocess.Popen(["osascript", "-e", 'tell application "%s" to quit' % app.replace('"', "")])
    return say_cat("close_app", app=app_ru(app))


WORDNUM_EXTRA = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
           "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900, "шестьдесят": 60,
           "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "тысяча": 1000, "тысячу": 1000, "тысячи": 1000}


def parse_num(s):
    s = s.strip().replace(",", ".")
    try:
        return float(s)
    except Exception:
        pass
    total, cur, used = 0, 0, False
    words = dict(NUM_WORDS, **WORDNUM_EXTRA)
    for w in s.split():
        if w not in words:
            return None
        v = words[w]
        used = True
        if v == 1000:
            total += (cur or 1) * 1000
            cur = 0
        else:
            cur += v
    return float(total + cur) if used else None


def calc_command(t):
    m = re.search(r"сколько будет\s+(.+?)\s+(плюс|минус|умножить на|умноженное на|разделить на|делить на|на|x|х|\*|/|\+|-)\s+(.+)$", t)
    if not m:
        return None
    a, op, b = parse_num(m.group(1)), m.group(2), parse_num(m.group(3))
    if a is None or b is None:
        return None
    if op in ("плюс", "+"):
        r = a + b
    elif op in ("минус", "-"):
        r = a - b
    elif op in ("умножить на", "умноженное на", "на", "x", "х", "*"):
        r = a * b
    else:
        if b == 0:
            return {"say": "На ноль не делю. Даже Тони не рискнул бы.", "emotion": "witty"}
        r = a / b
    txt = ("%d" % r) if abs(r - round(r)) < 1e-9 else ("%.2f" % r).rstrip("0").replace(".", ",")
    return {"say": random.choice(["Получается %s.", "Будет %s.", "Ответ: %s.", "%s. Можешь проверить на калькуляторе."]) % txt,
            "emotion": "proud"}


MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
ORD_N = ["", "первое", "второе", "третье", "четвёртое", "пятое", "шестое", "седьмое", "восьмое", "девятое",
         "десятое", "одиннадцатое", "двенадцатое", "тринадцатое", "четырнадцатое", "пятнадцатое", "шестнадцатое",
         "семнадцатое", "восемнадцатое", "девятнадцатое", "двадцатое", "двадцать первое", "двадцать второе",
         "двадцать третье", "двадцать четвёртое", "двадцать пятое", "двадцать шестое", "двадцать седьмое",
         "двадцать восьмое", "двадцать девятое", "тридцатое", "тридцать первое"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def weather_reply(t):
    m = re.search(r"погод\w*\s+(?:в|во)\s+([а-яё\- ]+?)(?:\s+(?:сегодня|сейчас|завтра))?$", t)
    city = m.group(1).strip() if m else settings().get("weather_city", "")
    try:
        w = SENSE.fetch_weather(city)
    except Exception:
        return {"say": "Метеоспутники молчат — не смог получить погоду. Проверь интернет.", "emotion": "concerned"}
    deg = lambda v: ("минус %d" % -v if v < 0 else "%d" % v) + " " + plural(abs(v), ("градус", "градуса", "градусов"))
    where = ("В городе %s" % city.capitalize()) if m else "На улице"
    txt = "%s сейчас %s, %s. Ощущается как %s, ветер %d %s." % (
        where, deg(w["temp"]), w["desc"], deg(w["feels"]), w["wind"], plural(w["wind"], ("метр", "метра", "метров")) +
        " в секунду")
    txt += " Днём до %s." % deg(w["max"])
    d = (w["desc"] + " " + w["desc_en"]).lower()
    cat = ("weather_snow" if "снег" in d or "snow" in d else "weather_rain" if re.search(r"дожд|ливен|морос|rain|drizzle", d)
           else "weather_cold" if w["temp"] <= 0 else "weather_hot" if w["temp"] >= 27
           else "weather_nice" if 14 <= w["temp"] <= 26 else None)
    extra = say_cat(cat) if cat else {"say": "", "emotion": "calm"}
    with SENSE.XLOCK:
        if not m:
            SENSE.X["weather"] = w
    return {"say": (txt + " " + extra["say"]).strip(), "emotion": extra["emotion"]}


def mark_command(t):
    for rx, cmd, cat in [
        (r"иди сюда|ко мне|подойди|прилети|сюда|подлети", "come", "mark_come"),
        (r"потолок|потолку", "ceiling", "mark_wall"),
        (r"стен|залезь|полезай|лазь|карабк", "wall", "mark_wall"),
        (r"полета|лети|взлет|полет", "fly", "mark_fly"),
        (r"спать|усни|поспи|отдыха|баиньки|спи\b", "sleep", "mark_sleep"),
        (r"проснись|подъем|вставай|просыпайся", "wake", "mark_wake"),
        (r"танц|станцуй|потанцуй|зажги", "dance", "mark_dance"),
        (r"сальто|трюк|кувырок", "flip", "mark_generic"),
        (r"спрячь|исчезни|уйди|скройся|прячься", "hide", "mark_hide"),
        (r"появись|вернись|покажись|выходи", "show", "mark_wake"),
        (r"скан|осмотри|проверь экран", "scan", "mark_scan"),
        (r"помаши|привет", "wave", "mark_generic"),
    ]:
        if re.search(rx, t):
            r = say_cat(cat)
            r["pet"] = {"cmd": cmd}
            return r
    r = say_cat("mark_generic")
    r["pet"] = {"cmd": "fly"}
    return r


SMALLTALK = [
    (r"как (у тебя )?дела|как ты\b|как поживаешь|как сам\b|как настроение|как жизнь", "how_are_you"),
    (r"(ты|вы) кто|кто ты|расскажи о себе|что ты такое|представься", "who_are_you"),
    (r"кто тебя (создал|сделал|написал)", "creator"),
    (r"^(спасибо|благодарю|спасибки|пасиб)|спасибо (тебе|большое|джарвис)", "thanks_reply"),
    (r"молодец|умница|ты лучший|красавчик|отлично сработал|ты супер|гений|ты крут|круто сделал", "praise_reply"),
    (r"дурак|тупой|идиот|бесполезн|тупица|глупый|отстой|ты ужасен|кретин", "insult_reply"),
    (r"люблю тебя|ты мне нравишься|обожаю тебя", "love_reply"),
    (r"^(извини|прости)|извини меня|прости меня", "sorry_reply"),
    (r"шутк|анекдот|пошути|рассмеши", "jokes"),
    (r"факт|удиви меня|что интересного", "facts"),
    (r"подбодри|мне грустно|мне плохо|все плохо|поддержи|расстро|грустно|тоскливо", "cheer_up"),
    (r"я устал|нет сил|выдохся|вымотал|устала", "tired_reply"),
    (r"похвали меня|я молодец", "compliments"),
    (r"скучно|скука", "bored"),
    (r"спокойной ночи|доброй ночи|пойду спать|я спать", "good_night"),
    (r"доброе утро", "good_morning_reply"),
    (r"тони старк|\bстарк", "stark"),
    (r"альтрон", "ultron"),
    (r"кто такая пятница|пятниц\w* (ии|интеллект|ассистент|лучше)", "friday"),
    (r"мстител", "avengers"),
    (r"ты живой|у тебя есть чувства|ты чувствуешь|ты настоящий|у тебя есть душа", "alive"),
    (r"сколько тебе лет|твой возраст|когда ты родился", "age"),
    (r"поговори со мной|скажи что-нибудь|давай поболтаем|поболтаем|расскажи что-нибудь", "chat"),
    (r"какое у тебя настроение|ты в настроении|ты в духе", "mood_happy"),
    (r"(джарвис|jarvis)?\s*ты\s+(тут|здесь|на месте|слышишь)", "here"),
]


def extra_command(t):
    # доклад и диагностика
    if re.search(r"как там (мак|компьютер|система|машин)|как дела у мака|состояние систем|статус систем|доклад|"
                 r"отч[её]т о систем|как себя чувствует|как мак", t):
        return status_reply()
    if re.search(r"диагностик|просканируй (систем|мак|компьютер)|полн\w* (скан|провер)|сканирование|проверь систем", t):
        return diagnostics_command()

    # дашборд и виджет
    if re.search(r"(разверни|покажи|открой|выведи|включи)\s+(\w+\s+)?(дашборд|панел|приборн|интерфейс)|"
                 r"на весь экран|полный экран|во весь экран|разверни", t):
        r = say_cat("dashboard_open")
        r["ui"] = {"dashboard": "full"}
        return r
    if re.search(r"(сверни|убери|спрячь|закрой)\s+(\w+\s+)?(дашборд|панел|интерфейс)|в виджет|^сверни", t):
        r = say_cat("dashboard_close")
        r["ui"] = {"dashboard": "widget"}
        return r
    if "виджет" in t:
        hide = re.search(r"скрой|убери|спрячь|выключи|закрой", t)
        r = say_cat("widget_hide" if hide else "widget_show")
        r["ui"] = {"widget": not hide}
        return r
    if re.search(r"голосов\w* лаборатори|настройк\w* голоса", t):
        r = {"say": "Открываю голосовую лабораторию. Выбирай, как мне звучать.", "emotion": "curious",
             "ui": {"dashboard": "full", "voicelab": True}}
        return r

    # Марк
    if re.search(r"\bмарк(?:а|у|ом|е|и)?\b|марк ноль|марк 0", t):
        return mark_command(t)

    # громкость
    if re.search(r"(громкость|звук)\s+(на\s+)?(\S+)(\s+процент\w*)?$", t) and not re.search(r"выключи|включи|верни", t):
        m = re.search(r"(громкость|звук)\s+(на\s+)?(.+?)(\s+процент\w*)?$", t)
        n = parse_num(m.group(3)) if m else None
        if n is not None:
            SENSE.set_volume(n)
            return say_cat("volume_set", pct="%d%%" % n)
    if re.search(r"громче|погромче|прибавь звук|сделай громче", t):
        a = SENSE.read_audio() or {"volume": 50}
        SENSE.set_volume(min(100, (a["volume"] or 50) + 12), False)
        return say_cat("volume_up")
    if re.search(r"тише|потише|убавь звук|сделай тише", t):
        a = SENSE.read_audio() or {"volume": 50}
        SENSE.set_volume(max(0, (a["volume"] or 50) - 12))
        return say_cat("volume_down")
    if re.search(r"(выключи|отключи|убери) звук|без звука", t):
        SENSE.set_volume(muted=True)
        return say_cat("mute")
    if re.search(r"(включи|верни) звук", t):
        SENSE.set_volume(muted=False)
        return say_cat("unmute")

    # музыка
    if re.search(r"(поставь на )?пауз|останови музык|стоп музык|продолжи (музык|воспроизвед)|(включи|играй) музык|воспроизвед", t):
        r = say_cat("media_pause")
        r["ui"] = {"media": "playpause"}
        return r
    if re.search(r"следующ\w* (трек|песн|композици)|переключи (трек|песн)|дальше трек|другую песню", t):
        r = say_cat("media_next")
        r["ui"] = {"media": "next"}
        return r
    if re.search(r"предыдущ\w* (трек|песн|композици)|верни (трек|песн)|прошл\w* (трек|песн)", t):
        r = say_cat("media_prev")
        r["ui"] = {"media": "prev"}
        return r

    # экран и питание
    if re.search(r"скриншот|снимок экрана|сфоткай экран|скрин экрана", t):
        return screenshot_command()
    if re.search(r"заблокируй|блокировк|запри (мак|экран|компьютер)", t):
        threading.Timer(2.5, lock_screen).start()
        return say_cat("lock_screen")
    if re.search(r"(выключи|погаси|усыпи) (экран|монитор|дисплей)", t):
        threading.Timer(2.5, lambda: sh(["pmset", "displaysleepnow"])).start()
        return say_cat("display_sleep")
    if re.search(r"усыпи (мак|компьютер|систем)|спящий режим|режим сна|(отправь|переведи) (мак|компьютер) в сон", t):
        threading.Timer(4, lambda: sh(["pmset", "sleepnow"])).start()
        return say_cat("sleep_mac")
    if re.search(r"перезагру(зи|зка)|ребут", t):
        return {"say": "", "ui": {"restart": True}}

    # время, дата, погода
    if re.search(r"который час|сколько времени|сколько сейчас времени", t):
        lt = time.localtime()
        return say_cat("time_reply", time="%d:%02d" % (lt.tm_hour, lt.tm_min), fallback="Сейчас {time}.")
    if re.search(r"какое (сегодня )?число|какой (сегодня )?день|какая (сегодня )?дата|какой день недели|какое сегодня", t):
        lt = time.localtime()
        date = "%s %s, %s" % (ORD_N[lt.tm_mday], MONTHS_GEN[lt.tm_mon - 1], WEEKDAYS[lt.tm_wday])
        return say_cat("date_reply", date=date, fallback="Сегодня {date}.")
    if re.search(r"погод|градусов на улице|температура на улице|на улице (холодно|тепло)|нужен ли зонт", t):
        return weather_reply(t)

    # калькулятор, монетка, кубик
    r = calc_command(t)
    if r:
        return r
    if re.search(r"монет|орел или решка|орёл или решка", t):
        return say_cat(random.choice(["coin_heads", "coin_tails"]))
    if re.search(r"кубик|игральн\w* кост", t):
        return say_cat("dice", n=str(random.randint(1, 6)), fallback="Выпало {n}.")
    m = re.search(r"случайное число(?: от (\S+) до (\S+))?", t)
    if m:
        a = int(parse_num(m.group(1)) or 1) if m.group(1) else 1
        b = int(parse_num(m.group(2)) or 100) if m.group(2) else 100
        return say_cat("random_number", n=str(random.randint(min(a, b), max(a, b))), fallback="Пусть будет {n}.")

    # разговорчивость
    if re.search(r"помолчи|замолчи|заткнись|тихий режим|не болтай|режим тишины|тишина", t):
        save_settings({"quiet_until": time.time() + 2 * 3600})
        return say_cat("quiet_on")
    if re.search(r"можешь говорить|говори снова|снова говори|выйди из тишины|отключи тишину|хватит молчать", t):
        save_settings({"quiet_until": 0, "talk": "normal" if settings()["talk"] == "quiet" else settings()["talk"]})
        return say_cat("quiet_off")
    if re.search(r"будь разговорчив|говори больше|болтай больше|режим болтун|поболтливее", t):
        save_settings({"talk": "chatty", "quiet_until": 0})
        return say_cat("talk_more")
    if re.search(r"говори меньше|болтай меньше|меньше болтай|поменьше болтай", t):
        save_settings({"talk": "normal" if settings()["talk"] == "chatty" else "quiet"})
        return say_cat("talk_less")

    # голос
    r = voice_settings_command(t)
    if r:
        return r

    # протоколы
    if re.search(r"протокол (фокус|работа)|режим фокус", t):
        res = timer_command("работаю над фокус 25 минут")
        save_settings({"quiet_until": time.time() + 25 * 60})
        return {"say": "Протокол «Фокус». Двадцать пять минут тишины, я на страже. " + "", "emotion": "proud",
                "pet": {"cmd": "sleep"}, "ui": {"protocol": "focus"}, "_t": res}
    if re.search(r"протокол (призрак|стелс|невидимка)", t):
        return {"say": "Протокол «Призрак». Убираю всё лишнее с глаз.", "emotion": "witty",
                "ui": {"widget": False}, "pet": {"cmd": "hide"}}
    if re.search(r"протокол (дом|домой)|я ухожу|ухожу из дома", t):
        threading.Timer(4, lock_screen).start()
        r = say_cat("goodbye")
        r["say"] += " Запираю мак."
        return r
    if re.search(r"красная тревога|боевая тревога|тревога", t):
        return {"say": "Красная тревога! Шучу. Но выглядит эффектно, правда?", "emotion": "alarmed",
                "ui": {"alert": True}, "pet": {"cmd": "flip"}}

    # открыть, закрыть, найти в интернете
    m = re.search(r"(загугли|погугли|поищи в (?:интернете|яндексе|гугле|сети)|найди в (?:интернете|яндексе|гугле|сети)|"
                  r"поиск в интернете)\s+(.+)", t)
    if m:
        q = m.group(2).strip()
        if not DEMO:
            import urllib.parse
            subprocess.Popen(["open", "https://ya.ru/search/?text=" + urllib.parse.quote(q)])
        return say_cat("search_web", q=q)
    m = re.search(r"(?:^|\s)(открой|запусти)\s+(?:мне\s+)?(?:программу\s+|приложение\s+|сайт\s+)?(.+)$", t)
    if m and not re.search(r"стать|обложк", m.group(2)):
        return open_thing(m.group(2))
    m = re.search(r"(?:^|\s)(закрой|заверши|выключи)\s+(?:программу\s+|приложение\s+)?(.+)$", t)
    if m and not re.search(r"компьютер|мак\b|макбук|ноутбук|звук|экран|таймер|помидор|напомина", m.group(2)):
        return close_thing(m.group(2))

    # общение
    for rx, cat in SMALLTALK:
        if re.search(rx, t):
            r = say_cat(cat)
            if cat in ("jokes", "praise_reply", "love_reply"):
                r["pet"] = {"cmd": "happy"}
            if cat == "insult_reply":
                r["pet"] = {"cmd": "sad"}
            return r
    if re.search(r"^(привет|здравствуй|хай|салют|здорово|добрый (день|вечер))", t):
        return greet_reply(boot=False)
    if re.search(r"^(пока|до свидания|до встречи|увидимся|до завтра)", t):
        return say_cat("goodbye")
    return None


VOICE_CYCLE = ["aidar", "eugene"]


def voice_settings_command(t):
    v = settings()["voice"]
    patch, say = None, None
    if re.search(r"(смени|поменяй|другой|следующий) голос", t):
        i = VOICE_CYCLE.index(v["speaker"]) if v["speaker"] in VOICE_CYCLE else -1
        patch = {"speaker": VOICE_CYCLE[(i + 1) % len(VOICE_CYCLE)]}
        say = "Так лучше? Это голос номер %d из %d." % ((i + 1) % len(VOICE_CYCLE) + 1, len(VOICE_CYCLE))
    elif re.search(r"голос\w* (пониже|ниже|глубже|басовитее)|говори ниже", t):
        patch, say = {"pitch": max(-40, v["pitch"] - 6)}, "Опускаю тон. Так солиднее?"
    elif re.search(r"голос\w* (повыше|выше|звонче)|говори выше", t):
        patch, say = {"pitch": min(40, v["pitch"] + 6)}, "Поднимаю тон. Звучу бодрее?"
    elif re.search(r"говори (быстрее|побыстрее)|ускорь речь", t):
        patch, say = {"rate": min(140, v["rate"] + 8)}, "Ускоряюсь. Успевай за мной."
    elif re.search(r"говори (медленнее|помедленнее)|замедли речь", t):
        patch, say = {"rate": max(70, v["rate"] - 8)}, "Сбавляю темп. Так спокойнее."
    elif re.search(r"шлем|как в броне|радио голос", t):
        patch, say = {"fx": "helmet"}, "Включаю голос из шлема. Связь с бронёй установлена."
    elif re.search(r"чистый голос|без эффект|без обработки", t):
        patch, say = {"fx": "clean"}, "Убираю обработку. Только я и микрофон."
    elif re.search(r"голограмм", t):
        patch, say = {"fx": "hologram"}, "Голографический режим. Звучу как из будущего."
    elif re.search(r"(обычный|стандартный|фирменный) голос|голос джарвиса", t):
        patch, say = {"fx": "jarvis", "pitch": JV.DEFAULT_VOICE["pitch"], "rate": JV.DEFAULT_VOICE["rate"]}, \
            "Возвращаю фирменное звучание."
    elif re.search(r"старый голос|как раньше|металлическ", t):
        patch, say = {"fx": "legacy"}, "Включаю старую металлическую обработку. Ностальгия."
    if patch is None:
        return None
    save_settings({"voice": patch})
    return {"say": say, "emotion": "happy", "ui": {"voice": settings()["voice"]}}


def lock_screen():
    if DEMO:
        return
    r = subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'],
                       capture_output=True, timeout=5)
    if r.returncode != 0:
        sh(["pmset", "displaysleepnow"])


def screenshot_command():
    path = os.path.expanduser("~/Desktop/JARVIS %s.png" % time.strftime("%Y-%m-%d в %H.%M.%S"))
    if not DEMO:
        r = subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=15)
        if r.returncode != 0 or not os.path.exists(path):
            return {"say": "Не получилось снять экран. Разреши JARVIS запись экрана в Настройки → Конфиденциальность "
                           "и безопасность → Запись экрана.", "emotion": "concerned"}
    r = say_cat("screenshot")
    r["ui"] = {"flash": True}
    r["pet"] = {"cmd": "scan"}
    return r


def diagnostics_command():
    def finish():
        time.sleep(9)
        s = snapshot()
        bad = [r for r in s["recs"] if r["level"] in ("crit", "warn")]
        if bad:
            r = say_cat("scan_bad", issue=bad[0]["title"].rstrip("."), fallback="Есть проблема: {issue}.")
            txt = r["say"] + " " + bad[0]["text"]
        else:
            r = say_cat("scan_good", fallback="Диагностика завершена. Всё в норме.")
            txt = r["say"] + " " + status_phrase(s)
        announce(txt, emotion=r["emotion"], os_notify=False, ui={"scan_done": True})
    threading.Thread(target=finish, daemon=True).start()
    r = say_cat("scan_intro", fallback="Запускаю полную диагностику.")
    r["ui"] = {"scan": True}
    r["pet"] = {"cmd": "scan"}
    return r


ACTIONS_OPEN = {
    "activity": ["open", "-a", "Activity Monitor"],
    "storage": ["open", "x-apple.systempreferences:com.apple.settings.Storage"],
    "network": ["open", "x-apple.systempreferences:com.apple.Network-Settings.extension"],
    "firewall": ["open", "x-apple.systempreferences:com.apple.Network-Settings.extension?Firewall"],
    "filevault": ["open", "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?FileVault"],
}


def action_command(name):
    """Кнопки интерфейса и облака Марка-0."""
    if name in ACTIONS_OPEN:
        if not DEMO:
            subprocess.Popen(ACTIONS_OPEN[name])
        return {"say": random.choice(["Открываю.", "Сейчас покажу.", "Готово, смотри."]), "emotion": "calm"}
    if name == "quit_settings":
        if not DEMO:
            subprocess.Popen(["osascript", "-e", 'tell application "System Settings" to quit'])
        return {"say": "Закрыл Системные настройки. Процессор выдохнул.", "emotion": "happy"}
    table = {
        "status": lambda: status_reply(),
        "scan": lambda: diagnostics_command(),
        "joke": lambda: say_cat("jokes"),
        "fact": lambda: say_cat("facts"),
        "cheer": lambda: say_cat("cheer_up"),
        "weather": lambda: weather_reply("погода"),
        "vpn": lambda: voice_command({"cmd": "vpn"}),
        "screenshot": lambda: screenshot_command(),
        "lock": lambda: extra_command("заблокируй"),
        "display_sleep": lambda: extra_command("выключи экран"),
        "sleep_mac": lambda: extra_command("усыпи мак"),
        "quiet": lambda: extra_command("помолчи") if talk_mode() != "quiet" else extra_command("можешь говорить"),
        "chatty": lambda: extra_command("будь разговорчивее"),
        "party": lambda: easter_command("протокол вечеринка"),
        "time": lambda: extra_command("который час"),
        "brief": lambda: brief_phrase(),
        "dashboard": lambda: extra_command("разверни дашборд"),
        "widget": lambda: extra_command("сверни в виджет"),
        "music": lambda: extra_command("пауза"),
        "next": lambda: extra_command("следующий трек"),
        "greet": lambda: greet_reply(),
    }
    fn = table.get(name)
    if not fn:
        return {"say": "Такой кнопки я не знаю.", "emotion": "witty"}
    return fn()


# ───────────────────────── состояние на диске ─────────────────────────

def load_json(name, default):
    try:
        with open(os.path.join(SUPPORT, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(name, data):
    try:
        os.makedirs(SUPPORT, exist_ok=True)
        tmp = os.path.join(SUPPORT, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(SUPPORT, name))
    except Exception:
        pass


_lock = threading.Lock()


def norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:.]", " ", str(t).lower().replace("ё", "е"))).strip()


def tail_from(t, keys):
    """Распознавание отдаёт всё сказанное за сессию — берём хвост с последнего ключевого слова."""
    pos = max((t.rfind(k) for k in keys), default=-1)
    return t[pos:] if pos >= 0 else t


# ───────────────────────── числа и время из речи ─────────────────────────

NUM_WORDS = {"ноль": 0, "один": 1, "одну": 1, "одна": 1, "минуту": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
             "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
             "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
             "сорок": 40, "пятьдесят": 50, "полчаса": 0.5, "полтора": 1.5, "полторы": 1.5}


def words_to_num(s):
    """«двадцать пять» → 25, «45» → 45, «полтора» → 1.5. Возвращает (число, сколько слов съели) или (None, 0)."""
    toks = s.split()
    if not toks:
        return None, 0
    m = re.match(r"^(\d+(?:[.,]\d+)?)$", toks[0])
    if m:
        return float(m.group(1).replace(",", ".")), 1
    total, used = 0, 0
    for tok in toks[:2]:
        if tok in NUM_WORDS:
            v = NUM_WORDS[tok]
            if used == 1 and not (total >= 20 and total % 10 == 0 and v < 10):
                break
            total += v
            used += 1
        else:
            break
    return (total, used) if used else (None, 0)


UNIT_SEC = (("секунд", 1), ("минут", 60), ("мин", 60), ("час", 3600))


def parse_duration(s):
    """«через 20 минут», «на полчаса», «25 минут», «час» → секунды и остаток фразы."""
    s = s.strip()
    if s.startswith("полчаса"):
        return 1800, s[len("полчаса"):]
    if re.match(r"^час(а|ов)?\b", s):
        return 3600, re.sub(r"^час(а|ов)?\b", "", s)
    n, used = words_to_num(s)
    if n is None:
        return None, s
    rest = " ".join(s.split()[used:])
    for u, k in UNIT_SEC:
        if rest.startswith(u):
            return int(n * k), re.sub(r"^\w+", "", rest, count=1)
    return None, s


def parse_clock(s):
    """«в 15:00», «в 15 30», «в 9 утра», «в 7 вечера», «завтра в 10» → timestamp и остаток."""
    m = re.search(r"(завтра\s+)?в\s+(\d{1,2})(?:[:.\s](\d{2}))?(?:\s*(утра|дня|вечера|ночи))?\b", s)
    if not m:
        return None, s
    h, mi = int(m.group(2)), int(m.group(3) or 0)
    if m.group(4) in ("дня", "вечера") and h < 12:
        h += 12
    if h > 23 or mi > 59:
        return None, s
    now = time.localtime()
    base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, mi, 0, 0, 0, -1))
    if m.group(1):
        base += 86400
    elif base <= time.time() + 30:
        base += 86400                       # время уже прошло — значит, завтра
    return base, (s[:m.start()] + " " + s[m.end():])


def fmt_clock(ts):
    lt = time.localtime(ts)
    day = "" if lt.tm_yday == time.localtime().tm_yday else "завтра "
    return "%sв %d:%02d" % (day, lt.tm_hour, lt.tm_min)


def fmt_minutes(sec):
    m = int(round(sec / 60))
    if m < 60:
        return "%d %s" % (m, plural(m, ("минута", "минуты", "минут")))
    h, m = divmod(m, 60)
    out = "%d %s" % (h, plural(h, ("час", "часа", "часов")))
    return out + (" %d %s" % (m, plural(m, ("минута", "минуты", "минут"))) if m else "")


# ───────────────────────── 3. напоминания ─────────────────────────

def reminders():
    return load_json("reminders.json", [])


def reminder_command(t):
    if re.search(r"(отмени|удали|очисти|сотри)\w*\s+(все\s+)?напоминани", t):
        rs = reminders()
        if not rs:
            return "Напоминаний и так нет."
        if " все" in t or "очисти" in t:
            save_json("reminders.json", [])
            return "Удалил все напоминания: %d." % len(rs)
        last = max(rs, key=lambda r: r["created"])
        rs.remove(last)
        save_json("reminders.json", rs)
        return "Отменил напоминание: %s." % last["text"]
    if re.search(r"(какие|мои|список|покажи)\w*\s+напоминани|напоминания на сегодня", t):
        rs = sorted(reminders(), key=lambda r: r["at"])
        if not rs:
            return "Напоминаний нет."
        items = ["%s — %s" % (fmt_clock(r["at"]), r["text"]) for r in rs[:5]]
        return "Напоминаний %d. " % len(rs) + ". ".join(items) + "."
    s = re.sub(r"^напомни(ть)?\s*(мне\s*)?", "", tail_from(t, ["напомни"])).strip()
    at, rest = None, s
    m = re.search(r"через\s+(.*)", s)
    if m:
        sec, after = parse_duration(m.group(1))
        if sec:
            at, rest = time.time() + sec, s[:m.start()] + " " + after
    if at is None:
        at, rest = parse_clock(s)
    if at is None:
        return "Когда напомнить? Скажи, например: напомни в 15:00 отправить статью, или через 20 минут."
    text = re.sub(r"^\s*(что(бы)?|о|об|про)\s+", "", re.sub(r"\s+", " ", rest).strip()) or "ты просил напомнить"
    with _lock:
        rs = reminders()
        rs.append({"at": at, "text": text, "created": time.time()})
        save_json("reminders.json", rs)
    bump("reminders")
    said = line("reminder_set", when=fmt_clock(at), what=text)
    return said or "%s %s: %s." % (random.choice(["Хорошо, напомню", "Принял, напомню", "Записал. Напомню",
                                                  "Будет сделано. Напомню", "Есть. Напомню"]), fmt_clock(at), text)


def reminders_loop():
    while True:
        try:
            now = time.time()
            with _lock:
                rs = reminders()
                due = [r for r in rs if r["at"] <= now]
                if due:
                    save_json("reminders.json", [r for r in rs if r["at"] > now])
            for r in due:
                late = now - r["at"] > 900
                txt = line("reminder_fire", what=r["text"]) if not late else None
                announce(txt or ("Напоминаю%s: %s." % (" (пропущенное)" if late else "", r["text"])),
                         trick="salute", title="JARVIS · напоминание", sound=True, emotion="alarmed")
        except Exception:
            pass
        time.sleep(5)


# ───────────────────────── 4. помодоро и учёт времени ─────────────────────────

_timer = {"kind": None, "project": None, "start": 0, "end": 0}


def project_name(s):
    ch = find_channel(s)
    if ch:
        return ch
    s = re.sub(r"\b(\d+|на|минут\w*|час\w*|полчаса)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:40] if s else "без проекта"


def log_work(project, start, end):
    if end - start < 60:
        return
    with _lock:
        log = load_json("timelog.json", [])
        log.append({"p": project, "s": start, "e": end})
        save_json("timelog.json", log[-5000:])


def worked(since, until=None):
    until = until or time.time()
    tot = {}
    for r in load_json("timelog.json", []):
        a, b = max(r["s"], since), min(r["e"], until)
        if b > a:
            tot[r["p"]] = tot.get(r["p"], 0) + (b - a)
    if _timer["kind"] == "work":
        a, b = max(_timer["start"], since), min(time.time(), until)
        if b > a:
            tot[_timer["project"]] = tot.get(_timer["project"], 0) + (b - a)
    return tot


def day_start(days_ago=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - days_ago * 86400


def stop_timer(announce_end=False):
    k, p, st = _timer["kind"], _timer["project"], _timer["start"]
    _timer.update(kind=None, project=None, start=0, end=0)
    if k == "work":
        log_work(p, st, time.time())
    return k, p, time.time() - st


def timer_command(t):
    if re.search(r"(стоп|останови|выключи|отмени|сбрось)\w*\s+(таймер|помидор|помодоро)|закончил работ|хватит работать", t):
        if not _timer["kind"]:
            return "Таймер и так не запущен."
        k, p, el = stop_timer()
        if k == "work":
            return "Остановил. %s%s записал." % (fmt_minutes(el).capitalize(), "" if p == "без проекта" else " на " + p)
        return "Таймер остановлен."
    if re.search(r"сколько\s+(осталось|времени осталось)", t):
        if not _timer["kind"]:
            return "Таймер не запущен."
        return "Осталось %s." % fmt_minutes(max(0, _timer["end"] - time.time()))
    if re.search(r"сколько\s+я\s+(сегодня\s+|вчера\s+|за неделю\s+)?(работал|наработал)|сколько\s+(я\s+)?работал", t):
        if "вчера" in t:
            tot, when = worked(day_start(1), day_start(0)), "Вчера"
        elif "недел" in t:
            tot, when = worked(day_start(6)), "За неделю"
        else:
            tot, when = worked(day_start(0)), "Сегодня"
        if not tot:
            return "%s учёт пуст. Скажи «работаю над проектом», и я начну считать." % when
        parts = ["%s — %s" % (p, fmt_minutes(s)) for p, s in sorted(tot.items(), key=lambda x: -x[1])[:5]]
        return "%s %s. %s." % (when, fmt_minutes(sum(tot.values())), "; ".join(parts))
    s = tail_from(t, ["работаю над", "работаю", "помидор", "помодоро", "таймер"])
    dur = None
    m = re.search(r"(?:на\s+)?((?:\d+|[а-я]+)(?:\s+[а-я]+)?\s+(?:минут\w*|час\w*)|полчаса|час)\b", s)
    if m:
        dur, _ = parse_duration(m.group(1))
    if s.startswith("таймер"):
        if not dur:
            return "На сколько ставить таймер? Например: таймер на 10 минут."
        if _timer["kind"] == "work":
            stop_timer()
        _timer.update(kind="timer", project=None, start=time.time(), end=time.time() + dur)
        return "Таймер на %s пошёл." % fmt_minutes(dur)
    proj = project_name(re.sub(r"^(работаю\s+над|работаю|помидор\w*|помодоро)\s*", "", s))
    dur = dur or 25 * 60
    if _timer["kind"] == "work":
        stop_timer()
    _timer.update(kind="work", project=proj, start=time.time(), end=time.time() + dur)
    what = "" if proj == "без проекта" else " на " + proj
    return "%s %s%s. %s" % (random.choice(["Поехали:", "Засекаю:", "Таймер пошёл:", "Старт:"]), fmt_minutes(dur), what,
                            random.choice(["Отвлекать не буду, скажу, когда время выйдет.",
                                           "Я посторожу время, ты работай.", "Фокус. Я скажу, когда хватит."]))


def timer_loop():
    while True:
        try:
            if _timer["kind"] and time.time() >= _timer["end"]:
                k, p, el = stop_timer()
                if k == "work":
                    what = "" if p == "без проекта" else " на " + p
                    extra = line("pomodoro_end", dur=fmt_minutes(el)) or ""
                    announce(("Время вышло: %s%s. " % (fmt_minutes(el), what)) +
                             (extra or "Сделай перерыв минут пять — встань, пройдись, попей воды."),
                             trick="flip", title="JARVIS · помидор", sound=True, emotion="proud")
                    bump("pomodoros")
                    if 5 <= time.localtime().tm_hour < 8:
                        unlock("early")
                else:
                    announce(line("timer_end") or "Таймер вышел.", trick="salute", title="JARVIS · таймер",
                             sound=True, emotion="happy")
        except Exception:
            pass
        time.sleep(2)


# ───────────────────────── 5. утренний брифинг ─────────────────────────

def articles_since(ts):
    return [a for a in list_articles() if a[0] >= ts]


def brief_phrase(full=True):
    lt = time.localtime()
    h = lt.tm_hour
    hello = ("Доброй ночи." if h < 5 else "Доброе утро." if h < 12 else "Добрый день." if h < 18
             else "Добрый вечер." if h < 23 else "Доброй ночи.")
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    parts = [hello + " Сегодня %s, %d:%02d." % (days[lt.tm_wday], h, lt.tm_min)]
    s = snapshot()
    sc = s["score"]
    parts.append("Мак в норме, индекс %d." % sc if sc >= 80 else "Маку есть на что пожаловаться, индекс %d." % sc)
    v = s.get("vpn") or {}
    if v.get("installed"):
        parts.append("VPN подключён." if v.get("on") else "VPN выключен — скажи «подключи VPN».")
    b = s.get("battery")
    if b and not b["ac"] and b["pct"] < 40:
        parts.append("Заряд %d%%, держи зарядку рядом." % b["pct"])
    today_end = day_start(-1)
    rs = sorted([r for r in reminders() if r["at"] < today_end], key=lambda r: r["at"])
    if rs:
        parts.append("Напоминаний на сегодня %d. Первое %s: %s." % (len(rs), fmt_clock(rs[0]["at"]), rs[0]["text"]))
    else:
        parts.append("Напоминаний на сегодня нет.")
    if full:
        week = articles_since(day_start(6))
        if week:
            by = {}
            for a in week:
                by[a[2]] = by.get(a[2], 0) + 1
            parts.append("За неделю статей: %d — %s." % (len(week), ", ".join("%s %d" % kv for kv in by.items())))
        y = worked(day_start(1), day_start(0))
        if y:
            parts.append("Вчера в работе %s." % fmt_minutes(sum(y.values())))
    return " ".join(parts)


def brief_loop():
    time.sleep(25)                          # даём голосу прогреться
    while True:
        try:
            lt = time.localtime()
            st = load_json("state.json", {})
            today = time.strftime("%Y-%m-%d")
            if 5 <= lt.tm_hour < 14 and st.get("brief_day") != today and idle_seconds() < 120:
                st["brief_day"] = today
                save_json("state.json", st)
                announce(brief_phrase(), trick="salute", title="JARVIS · брифинг")
            track_day()
        except Exception:
            pass
        time.sleep(30)


# ───────────────────────── 6. поиск файлов ─────────────────────────

SKIP_PATHS = ("/Library/", "/.Trash/", "/node_modules/", ".app/", "/.git/", "/Caches/")


def find_files(query, limit=8):
    q = norm(query)
    words = [w for w in q.split() if len(w) > 1]
    if not words:
        return []
    stems = [w[:5] if len(w) >= 8 else w[:4] if len(w) >= 6 else w for w in words]
    home = os.path.expanduser("~")
    cands = []
    if shutil.which("mdfind"):
        longest = max(stems, key=len)
        try:
            out = subprocess.run(["mdfind", "-onlyin", home, "-name", longest], capture_output=True,
                                 text=True, timeout=10).stdout.splitlines()
        except Exception:
            out = []
        cands = out[:3000]
    else:                                   # запасной путь без Spotlight
        for root, dirs, files in os.walk(home):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "Library"]
            for n in files + dirs:
                cands.append(os.path.join(root, n))
            if len(cands) > 20000:
                break
    res = []
    for p in cands:
        if any(x in p + "/" for x in SKIP_PATHS):
            continue
        name = norm(os.path.basename(p))
        if all(s in name for s in stems):
            try:
                mt = os.path.getmtime(p)
            except Exception:
                mt = 0
            exact = q in name
            res.append((exact, mt, p))
    res.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [p for _, _, p in res[:limit]]


def find_command(t):
    s = tail_from(t, ["найди", "покажи", "где лежит", "где файл"])
    s = re.sub(r"^(найди|покажи|где лежит|где файл)\s*", "", s)
    s = re.sub(r"^(мне\s+)?(файл|папку|документ|картинку|фото|файлы)\s*", "", s).strip(" .")
    if not s:
        return "Что найти? Например: найди файл логотип ой всё."
    hits = find_files(s)
    if not hits:
        return "Не нашёл ничего похожего на «%s»." % s
    if not DEMO:
        subprocess.Popen(["open", "-R", hits[0]])
    name = os.path.splitext(os.path.basename(hits[0]))[0]
    more = len(hits) - 1
    return "Нашёл: %s. Показываю в Finder.%s" % (name, (" Похожих ещё %d." % more) if more else "")


# ───────────────────────── 2. работа с выделенным текстом ─────────────────────────

TEXT_ACTIONS = [
    (r"сократи", "Сократи этот текст примерно вдвое. Смысл, факты и интонацию сохрани."),
    (r"канцеляр|штамп", "Убери из текста канцелярит и штампы, перепиши живо и по-человечески. Смысл не меняй."),
    (r"улучши|перепиши", "Улучши этот текст: живее, понятнее, без канцелярита и штампов ИИ. Смысл не меняй."),
    (r"придумай\s+(\S+\s+)?заголов|заголовк\w*\s+для", "Придумай 5 цепляющих заголовков для этого текста, разных по подаче. Без кликбейта и штампов."),
    (r"исправь\s+ошибк|проверь\s+ошибк|орфограф", "Исправь орфографию и пунктуацию. Больше ничего не меняй."),
    (r"переведи", None),
]
LANGS = {"англ": "английский", "русск": "русский", "немец": "немецкий", "испан": "испанский",
         "француз": "французский", "итальян": "итальянский", "китай": "китайский"}
BUFFER_DIR = os.path.join(JARVIS_DIR, "Буфер")


def copy_selection():
    """Копирует выделенный текст из активного окна (Cmd+C) и возвращает его."""
    if DEMO:
        return "тестовый текст"
    front = frontmost_app()
    if not front or front.lower().startswith(("jarvis", "python")):
        return ""
    subprocess.run(["pbcopy"], input="", text=True)
    subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "c" using command down'],
                   capture_output=True, timeout=5)
    time.sleep(0.5)
    out = subprocess.run(["pbpaste"], capture_output=True, text=True,
                         env=dict(os.environ, LANG="ru_RU.UTF-8")).stdout
    return out.strip()


def text_action_command(t):
    instr = None
    for pat, ins in TEXT_ACTIONS:
        if re.search(pat, t):
            if ins is None:
                lang = next((v for k, v in LANGS.items() if k in t), "английский")
                ins = "Переведи этот текст на %s язык. Живо и естественно, не дословно." % lang
            instr = ins
            break
    if not instr:
        return None
    sel = copy_selection()
    if not sel:
        return "Сначала выдели текст, потом скажи команду ещё раз."
    os.makedirs(BUFFER_DIR, exist_ok=True)
    prompt = ("%s\n\nТекст:\n<<<\n%s\n>>>\n\nГотовый результат сохрани на мой мак файлом .txt в папку ~/JARVIS/Буфер "
              "(только итоговый текст, без пояснений) и покажи его здесь." % (instr, sel[:20000]))
    set_clipboard(prompt)
    session = claude_sessions().get("Буфер", "JARVIS")
    bump("text_actions")
    return {"say": "Взял выделенный текст. Отправляю в Клод, результат положу в буфер.", "claude_session": session}


# ───────────────────────── 1. обложки ─────────────────────────

def list_covers(channel=None):
    out = []
    cfg = channel_cfg()
    for ch in ([channel] if channel else list(channels())):
        mark = (cfg.get(ch, {}).get("метка_обложки") or "").lower()
        for p in walk_files(ch_dir(ch, "обложки"), (".png", ".jpg", ".jpeg", ".webp")):
            if mark and mark not in os.path.basename(p).lower():
                continue
            try:
                out.append((os.path.getmtime(p), p, ch))
            except Exception:
                pass
    out.sort(reverse=True)
    return out


def set_clipboard_image(path):
    if DEMO:
        return True
    js = ("ObjC.import('AppKit');var img=$.NSImage.alloc.initWithContentsOfFile(%s);"
          "if(img.isNil()){'no'}else{var pb=$.NSPasteboard.generalPasteboard;pb.clearContents;"
          "pb.writeObjects($([img]));'ok'}" % json.dumps(path))
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=10)
    return r.stdout.strip() == "ok"


def cover_command(t):
    ch = find_channel(t) or _art["channel"]
    if re.search(r"встав|скопируй|возьми", t):
        cs = list_covers(find_channel(t))
        if not cs:
            return "Обложек пока нет. Скажи: сделай обложку."
        set_clipboard_image(cs[0][1])
        if re.search(r"встав", t) and not DEMO:
            time.sleep(0.25)
            if press_paste():
                return "Вставил обложку."
        return "Обложка в буфере. Если поле обложки на Дзене не принимает вставку, скажи «покажи обложку» и перетащи файл."
    if re.search(r"покажи|где", t):
        cs = list_covers(find_channel(t))
        if not cs:
            return "Обложек пока нет."
        if not DEMO:
            subprocess.Popen(["open", "-R", cs[0][1]])
        return "Показываю обложку в Finder."
    arts = list_articles(ch) if ch else list_articles()
    if not arts:
        return "Сначала нужна статья — обложку делаю под неё."
    _, path, ch = arts[0]
    title, _ = read_article(path)
    folder = os.path.basename(os.path.dirname(path))
    c = channel_cfg().get(ch, {})
    cdir = ch_dir(ch, "обложки")
    style = (c.get("стиль_обложки") or "Стиль — как у прошлых обложек канала в {обложки}. Сделай 2–3 варианта.")
    style = " " + style.replace("{статьи}", short_path(ch_dir(ch, "статьи"))).replace("{обложки}", short_path(cdir))
    if c.get("обложка_в_статье"):
        dest = "в папку статьи %s файлом «01_обложка.png»" % short_path(os.path.dirname(path))
    else:
        dest = "в папку %s/%s, файлы назови по варианту (например «1 минимал.png»)" % (short_path(cdir), folder)
    prompt = ("Сделай обложку 1600×900 для статьи «%s» (файл %s) для канала «%s» на Дзене.%s Сохрани PNG на мой мак %s."
              % (title, short_path(path), ch, style, dest))
    set_clipboard(prompt)
    session = claude_sessions().get(ch, ch)
    return {"say": "Делаем обложку к статье «%s». Отправляю заказ в сессию %s." % (title, session),
            "claude_session": session}


# ───────────────────────── 7. свои команды ─────────────────────────

CUSTOM_DIR = os.path.expanduser("~/JARVIS")
CUSTOM_FILE = os.path.join(CUSTOM_DIR, "Мои команды.json")
CUSTOM_EXAMPLE = {
    "_как это работает": "Слева фраза, справа действия по порядку. app: программа, url: сайт, folder: папка, "
                         "file: файл, say: что ответит JARVIS. Сохрани файл — JARVIS подхватит за минуту.",
    "рабочий режим": ["app:Claude", "url:https://dzen.ru/profile/editor",
                      "say:Рабочий режим включён. Дзен и Клод на месте."],
    "открой статьи": ["folder:~/Documents/Дзен/Статьи", "say:Открываю папку со статьями."],
}


def custom_commands():
    try:
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {norm(k): v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}
    except FileNotFoundError:
        try:
            os.makedirs(CUSTOM_DIR, exist_ok=True)
            with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
                json.dump(CUSTOM_EXAMPLE, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {norm(k): v for k, v in CUSTOM_EXAMPLE.items() if not k.startswith("_")}
    except Exception:
        return {}


def run_custom(actions):
    said = []
    for a in actions:
        kind, _, val = str(a).partition(":")
        val = val.strip()
        if kind == "say":
            said.append(val)
            continue
        if DEMO:
            continue
        try:
            if kind == "app":
                subprocess.Popen(["open", "-a", val])
            elif kind == "url" and re.match(r"^https?://", val):
                subprocess.Popen(["open", val])
            elif kind in ("folder", "file"):
                subprocess.Popen(["open", os.path.expanduser(val)])
            time.sleep(0.4)
        except Exception:
            pass
    return " ".join(said) or "Готово."


# ───────────────────────── 8. ачивки, пасхалки ─────────────────────────

ACHIEVEMENTS = {
    "article_1": "Первая статья через JARVIS",
    "article_5": "Пять статей — разгон пошёл",
    "article_10": "Десять статей. Это уже конвейер",
    "article_25": "25 статей. Дзен тебя запомнил",
    "article_50": "50 статей. Легенда редактора",
    "cover_1": "Первая обложка",
    "pomo_1": "Первый помидор",
    "pomo_10": "Десять помидоров — фокус-машина",
    "pomo_50": "50 помидоров. Броня из концентрации",
    "rem_10": "Десять напоминаний — ничего не забыто",
    "text_10": "Десять текстов отполировано",
    "streak_3": "Три дня подряд с JARVIS",
    "streak_7": "Неделя подряд с JARVIS",
    "streak_30": "Месяц подряд. Мы теперь команда",
    "night": "Ночной писатель: статья после полуночи",
    "early": "Ранняя пташка: помидор до восьми утра",
    "ironman": "Я — Железный человек",
    "party": "Протокол «Вечеринка»",
}
THRESHOLDS = {"articles": [1, 5, 10, 25, 50], "covers": [1], "pomodoros": [1, 10, 50],
              "reminders": [10], "text_actions": [10]}
PREFIX = {"articles": "article_", "covers": "cover_", "pomodoros": "pomo_", "reminders": "rem_", "text_actions": "text_"}


def unlock(key):
    with _lock:
        st = load_json("stats.json", {})
        got = st.setdefault("ach", [])
        if key in got or key not in ACHIEVEMENTS:
            return False
        got.append(key)
        save_json("stats.json", st)
    announce(line("achievement", name="«%s»" % ACHIEVEMENTS[key]) or
             "Ачивка разблокирована: «%s»!" % ACHIEVEMENTS[key], trick="flip", title="JARVIS · ачивка", sound=True,
             emotion="excited", ui={"achievement": ACHIEVEMENTS[key]})
    return True


def bump(counter):
    with _lock:
        st = load_json("stats.json", {})
        st[counter] = st.get(counter, 0) + 1
        n = st[counter]
        save_json("stats.json", st)
    if n in THRESHOLDS.get(counter, []):
        unlock(PREFIX[counter] + str(n))


def track_day():
    today = time.strftime("%Y-%m-%d")
    with _lock:
        st = load_json("stats.json", {})
        if st.get("last_day") == today:
            return
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        st["streak"] = st.get("streak", 0) + 1 if st.get("last_day") == yesterday else 1
        st["last_day"] = today
        save_json("stats.json", st)
        streak = st["streak"]
    for n in (3, 7, 30):
        if streak == n:
            unlock("streak_%d" % n)


def achievements_phrase():
    st = load_json("stats.json", {})
    got = st.get("ach", [])
    if not got:
        return "Ачивок пока нет. Начни с помидора: скажи «работаю над проектом»."
    names = [ACHIEVEMENTS[k] for k in got if k in ACHIEVEMENTS]
    return "Ачивок %d из %d. Последние: %s." % (len(names), len(ACHIEVEMENTS), "; ".join(names[-3:]))


EASTER = [
    (r"я\s+железный\s+человек", ["Знаю. Броня отполирована, реактор заряжен.",
                                  "Это мы уже поняли. Пресс-конференция в два часа."], "salute", "ironman"),
    (r"протокол\s+вечеринк", ["Протокол «Вечеринка» активирован. Марк-0, танцуй. Музыку не включаем, как договаривались."],
     "dance", "party"),
    (r"(покажи|сделай)\s+трюк|сальто", ["Марк-0, покажи класс.", "Смотри, что он умеет."], "flip", None),
    (r"(джарвис|jarvis)?\s*ты\s+(тут|здесь|на месте)", ["Всегда на связи.", "Тут. Реактор в строю."], "salute", None),
    (r"спасибо[,\s]+(джарвис|jarvis)", ["Обращайся.", "Всегда пожалуйста.", "Для этого я и существую."], None, None),
    (r"кто\s+тебя\s+(создал|сделал)", ["Кристофер собрал меня вместе с Claude. Тони бы одобрил."], "salute", None),
    (r"который\s+час|сколько\s+времени", None, None, None),
    (r"(мои\s+)?(ачивк|достижени)", None, None, None),
]


EASTER_CATS = {"ironman": "easter_ironman", "party": "easter_party"}


def easter_command(t):
    for pat, lines, trick, ach in EASTER:
        if re.search(pat, t):
            if pat.startswith("который"):
                lt = time.localtime()
                return "Сейчас %d:%02d." % (lt.tm_hour, lt.tm_min)
            if lines is None:
                return achievements_phrase()
            if trick:
                pet_trick(trick)
            if ach:
                threading.Timer(4, unlock, args=(ach,)).start()
            r = say_cat(EASTER_CATS[ach]) if ach in EASTER_CATS else {"say": random.choice(lines)}
            if not r.get("say"):
                r["say"] = random.choice(lines)
            if ach == "party":
                r["ui"] = {"party": True}
                r["emotion"] = "excited"
            if ach == "ironman":
                r["ui"] = {"ironman": True}
            return r
    return None


# ───────────────────────── справка: «какие у тебя команды» ─────────────────────────

HELP_CATS = [
    ("система", ("систем", "мак", "компьют"), [
        "«Как там мак?» — доклад о состоянии, «запусти диагностику» — полное сканирование",
        "«Подключи VPN», «открой Телеграм» — любую программу, «закрой Сафари»",
        "«Сделай скриншот», «заблокируй экран», «выключи экран», «усыпи мак»",
        "«Перезагрузи мак» и «выключи компьютер» — пять секунд, чтобы сказать «отмена»",
    ]),
    ("дашборд", ("дашборд", "панел", "виджет", "экран"), [
        "«Разверни дашборд» — всё состояние мака на весь экран",
        "«Сверни в виджет» — компактная панель на рабочем столе",
        "«Покажи виджет», «скрой виджет». Горячая клавиша — Option Command J",
    ]),
    ("Марк", ("марк", "робот", "помощник"), [
        "«Марк, иди сюда», «Марк, полетай», «Марк, на стену», «Марк, спать», «Марк, станцуй»",
        "Клик по Марку — фраза, погладь курсором — замурчит, двойной клик — облако с функциями",
        "Марка можно схватить мышкой и бросить",
    ]),
    ("звук", ("звук", "музык", "громк"), [
        "«Громче», «тише», «громкость на 30», «выключи звук», «включи звук»",
        "«Пауза», «следующий трек», «предыдущий трек»",
    ]),
    ("общение", ("общени", "поговор", "болт"), [
        "«Расскажи шутку», «расскажи факт», «подбодри меня», «как дела», «поговори со мной»",
        "«Помолчи» — два часа тишины, «можешь говорить», «будь разговорчивее», «говори меньше»",
        "«Подбрось монетку», «брось кубик», «сколько будет семь умножить на восемь»",
    ]),
    ("голос", ("голос",), [
        "«Смени голос», «голос ниже», «голос выше», «говори быстрее», «говори медленнее»",
        "«Голос как в шлеме», «голограмма», «чистый голос», «обычный голос»",
        "«Голосовая лаборатория» — все настройки голоса на экране",
    ]),
    ("статьи", ("стать", "дзен"), [
        "«Напиши статью в <канал>», можно с темой: «про нейросети» (каналы — в ~/JARVIS/Каналы.json)",
        "«Вставь заголовок» и «вставь статью» — на Дзене",
        "«Возьми статью для <канал>» — положить последнюю в буфер",
        "«Сделай обложку», «вставь обложку», «покажи обложку»",
    ]),
    ("тексты", ("текст", "редакт"), [
        "Выдели текст и скажи: «сократи текст», «улучши текст», «убери канцелярит»",
        "«Придумай заголовки», «исправь ошибки», «переведи на английский»",
    ]),
    ("время", ("врем", "напомин", "таймер", "помидор", "погод"), [
        "«Напомни в 15:00 отправить статью», «напомни через 20 минут …», «какие напоминания»",
        "«Работаю над проектом» — помидор на 25 минут, «таймер на 10 минут», «сколько осталось»",
        "«Который час», «какое сегодня число», «какая погода», «погода в Сочи»",
        "«Утренний брифинг», «сколько я сегодня работал»",
    ]),
    ("для души", ("душ", "развлеч", "весел", "протокол"), [
        "«Протокол вечеринка», «протокол фокус», «протокол призрак», «красная тревога»",
        "«Я Железный человек», «мои ачивки», «найди файл логотип»",
    ]),
    ("свои команды", ("свои", "мои команд", "сво"), None),
]
_help = {"until": 0}


def help_categories_phrase():
    names = [c[0] for c in HELP_CATS]
    _help["until"] = time.time() + 90
    return {"say": "Команды разбиты на %d категорий: %s и %s. Какую открыть? Или скажи «все»."
                   % (len(names), ", ".join(names[:-1]), names[-1]),
            "expect": [c[0] for c in HELP_CATS] + [a for c in HELP_CATS for a in c[1]] + ["все", "всё"]}


def help_category(t):
    if re.search(r"^(все|всё|все категории|всё подряд|все команды)$", t.strip()):
        parts = []
        for name, _, items in HELP_CATS:
            items = items if items is not None else custom_items()
            parts.append(name.capitalize() + ": " + "; ".join(items) + ".")
        _help["until"] = 0
        return " ".join(parts)
    for name, aliases, items in HELP_CATS:
        if name in t or any(a in t for a in aliases):
            items = items if items is not None else custom_items()
            _help["until"] = time.time() + 90      # можно сразу спросить следующую категорию
            return {"say": "%s. %s. Ещё категорию?" % (name.capitalize(), ". ".join(items)),
                    "expect": [c[0] for c in HELP_CATS] + [a for c in HELP_CATS for a in c[1]] + ["все", "всё"]}
    return None


def custom_items():
    names = list(custom_commands().keys())
    if not names:
        return ["Пока своих команд нет. Их можно добавить в файл «Мои команды» в папке JARVIS в домашней папке"]
    return ["«%s»" % n for n in names] + ["Добавить свои — в файле «Мои команды» в папке JARVIS"]


UNKNOWN_REPLIES = [
    "Такой команды в моих протоколах нет. Скажи «что ты умеешь», и я напомню, что есть.",
    "Перевожу на язык машин… Ошибка 404: команда не найдена.",
    "Это было сильно, но я не понял. Попробуй ещё раз, попроще.",
    "Мои сервоприводы озадачены. Такого я пока не умею.",
    "Хм. Звучит как задача для Марка-1. Я пока Марк-0.",
    "Не расслышал смысл. Повтори, или спроси «какие у тебя команды».",
    "Команда не распознана. Реактор в норме, а вот словарь надо расширять.",
    "Тони бы понял. Я — пока нет. Сформулируй по-другому.",
    "Запрос ушёл в никуда. Попробуй иначе — я быстро учусь.",
    "Это не по протоколу. Но идею я запомнил бы, если бы умел мечтать.",
    "Анализирую… анализирую… Нет, это выше моего уровня допуска.",
    "Такого в инструкции нет. Могу рассказать, что есть: спроси «что ты умеешь».",
    "Принял сигнал, но не смог расшифровать. Повтори медленнее?",
    "Кристофер, такой команды нет. Но звучало уверенно.",
    "Я слышу слова, но не вижу команды. Давай ещё раз.",
    "Команда отклонена системой непонимания. Шучу. Просто не знаю такую.",
    "Если это была шутка — смешно. Если команда — я её не знаю.",
    "Мне бы апгрейд для такого. Пока могу то, что в справке.",
]
WAKE_FILLERS = ["Слушаю.", "Да?", "На связи.", "Я тут."]


# ───────────────────────── маршрутизатор фраз ─────────────────────────

BASE_TRIGGERS = ["напомни", "напоминани", "таймер", "помидор", "помодоро", "работаю над", "закончил работ",
                 "хватит работать", "сколько я", "сколько осталось", "сколько работал", "брифинг", "что на сегодня",
                 "найди файл", "найди папку", "найди документ", "найди картинку", "где лежит", "где файл",
                 "сократи", "улучши текст", "улучши это", "перепиши текст", "канцеляр", "придумай заголов",
                 "переведи", "исправь ошибк", "обложк", "трюк", "сальто", "протокол вечеринк", "железный человек",
                 "ты тут", "ты здесь", "спасибо джарвис", "кто тебя", "который час", "ачивк", "достижени",
                 "какие команды", "какие у тебя команды", "какие у тебя есть команды", "что ты умеешь",
                 "что ты можешь", "список команд", "справк", "помощь", "какие категории"]


def triggers():
    return BASE_TRIGGERS + list(custom_commands().keys())


HELP_RE = r"какие\s+(у\s+тебя\s+)?(есть\s+)?команд|что\s+ты\s+(умеешь|можешь)|список\s+команд|справк|помощь|какие\s+категори"


def text_command(raw, llm=True, log=True):
    """Разбор команды. llm=False — только правила и плагины (так мозг вызывает команды как инструмент)."""
    t = norm(raw)
    if log:
        log_line("you", raw)
        _brain["cmds"] += 1
    if re.search(HELP_RE, t):
        return help_categories_phrase()
    if time.time() < _help["until"]:
        r = help_category(t)
        if r:
            return r
    for phrase, actions in custom_commands().items():
        if phrase and phrase in t:
            return run_custom(actions)
    r = PLUG.match_command(raw, t)
    if r:
        return r
    if "напомни" in t or "напоминани" in t:
        return reminder_command(t)
    if not re.search(r"таймер|помидор|помодоро|работаю над|стать|обложк|встав|найди файл|найди папку|"
                     r"найди документ|найди картинку|где лежит|где файл|сократи|улучши|канцеляр|переведи|"
                     r"исправь ошибк|придумай заголов|брифинг|что на сегодня", t):
        r = extra_command(t)
        if r:
            return r
    if "обложк" in t:
        k = t.rfind("обложк")
        head = t[:k]
        v = max((head.rfind(w) for w in ("сделай", "встав", "покажи", "скопируй", "возьми", "где", "нужна", "нарисуй")),
                default=-1)
        return cover_command(t[v:] if v >= 0 else t[k:])
    if re.search(r"таймер|помидор|помодоро|работаю над|закончил работ|хватит работать|сколько\s+(я|осталось|работал|времени осталось)", t) \
            and not re.search(r"сколько\s+времени$", t):
        return timer_command(t)
    if "брифинг" in t or "что на сегодня" in t:
        return brief_phrase()
    if re.search(r"найди|где лежит|где файл", t):
        return find_command(t)
    r = easter_command(t)
    if r:
        return r
    if "встав" in t:
        return article_command({"action": "title" if "заголов" in t else "paste", "text": t})
    r = text_action_command(tail_from(t, ["сократи", "улучши", "перепиши", "канцеляр", "придумай", "переведи", "исправь"]))
    if r:
        return r
    # статьи — старые команды
    if "стать" in t or "встав" in t:
        if "встав" in t and "заголов" in t:
            return article_command({"action": "title", "text": t})
        if "встав" in t:
            return article_command({"action": "paste", "text": t})
        if re.search(r"напиши|написать|сделай|нужна|подготовь|закажи", t):
            return article_command({"action": "order", "text": t})
        return article_command({"action": "pick", "text": t})
    r = extra_command(t)
    if r:
        return r
    if llm:
        r = BRAIN.ask(raw)
        if r:
            return r
    r = say_cat("unknown", fallback=random.choice(UNKNOWN_REPLIES))
    if llm and not BRAIN.configured() and not BRAIN._hinted[0] and BRAIN.conf().get("enabled", True):
        BRAIN._hinted[0] = True
        r["say"] = r["say"] + " " + BRAIN.SETUP_HINT
    r["pet"] = {"cmd": "confused"}
    return r


# ───────────────────────── мини-Джарвис (питомец) ─────────────────────────

PET_FALLBACK = {
    "pokes": ["Эй, я на дежурстве!", "Чем помочь?"],
    "day": ["Реактор стабилен, настроение тоже."],
}


def load_phrases():
    for d in (HERE, os.path.dirname(os.path.abspath(__file__))):
        try:
            with open(os.path.join(d, "pet_phrases.json"), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return merge_phrases(data, "Мои реплики Марка.json")
        except Exception:
            pass
    return PET_FALLBACK


PET_PHRASES = load_phrases()
_pet = {"recent": deque(maxlen=80), "alert_seen": {}, "greet_day": None,
        "active_since": time.time(), "was_away": False, "long_said": 0, "last_tick": time.time()}


def idle_seconds():
    """Сколько секунд не трогали мышь/клавиатуру (macOS HIDIdleTime)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0
    except Exception:
        return 0


MOODS = {"night": "calm", "late": "calm", "health": "care", "long_work": "care", "robot": "playful",
         "workshop": "playful", "pokes": "playful", "motivation": "excited", "friday": "excited",
         "weekend": "excited", "back": "love", "morning": "happy", "monday": "sad", "mac_good": "happy",
         "charging": "happy", "questions": "curious", "texts": "smart", "marketing": "smart", "dzen": "smart",
         "evening": "calm", "day": "calm", "jokes": "playful", "facts": "curious", "compliments": "love",
         "focus": "smart", "breaks": "care",
         "stroke": "love", "stroke_long": "love", "grab": "excited", "dangle": "playful", "thrown": "excited",
         "landed_dizzy": "dizzy", "fall": "alert", "climb_wall": "smart", "ceiling": "playful",
         "window_sit": "calm", "window_walk": "playful", "fly": "excited", "land": "happy", "peek": "curious",
         "chase": "playful", "scan": "smart", "menu_open": "happy", "menu_close": "calm", "sleep": "sleepy",
         "wake": "happy", "jarvis_talk": "playful", "idle": "calm", "walk": "calm", "mischief": "playful"}


def pick(*cats, **kw):
    pool = [(l, c) for c in cats for l in PET_PHRASES.get(c, [])]
    if not pool:
        pool = [(l, c) for c, v in PET_PHRASES.items() for l in v if "{" not in l]
    if kw:
        pool = [x for x in pool if all(k in kw for k in re.findall(r"\{(\w+)\}", x[0]))] or pool
    else:
        pool = [x for x in pool if "{" not in x[0]] or pool
    fresh = [x for x in pool if x[0] not in _pet["recent"]] or pool
    line_, cat = random.choice(fresh)
    _pet["recent"].append(line_)
    _pet["mood"] = MOODS.get(cat, "calm")
    return fill(line_, kw) if kw else line_


def with_voice(d):
    """Озвучку готовим ДО ответа, чтобы пузырь и голос у Марка шли одновременно."""
    if d.get("text"):
        d.setdefault("mood", "alert" if d.get("level") == "warn" else _pet.get("mood", "calm"))
        if d.get("speak") and not DEMO:
            try:
                synthesize(d["text"], "happy", kind="pet")
            except Exception:
                pass
    return d


def pet_poke():
    return with_voice({"text": pick("pokes"), "level": "info", "speak": True})


def pet_say(cat, speak=False, **kw):
    if cat not in PET_PHRASES:
        return {}
    return with_voice({"text": pick(cat, **kw), "level": "info", "speak": speak})


def pet_news():
    if _pet_q:
        n = _pet_q.popleft()
        return {"text": n.get("text", ""), "trick": n.get("trick"), "level": "info", "speak": False,
                "mood": n.get("mood")}
    return {}


def pet_tip():
    now = time.time()
    lt = time.localtime()
    h, wd = lt.tm_hour, lt.tm_wday            # wd: 0 — понедельник
    # учёт непрерывной работы: перерыв от 5 минут сбрасывает счётчик
    idle = idle_seconds()
    if idle > 300:
        _pet["was_away"] = True
        _pet["active_since"] = now
    elif _pet["was_away"]:
        _pet["was_away"] = False
        _pet["active_since"] = now - idle
        return {"text": pick("back"), "level": "info", "speak": False}
    worked = now - _pet["active_since"]

    # утреннее приветствие — один раз в день
    if 6 <= h < 12 and _pet["greet_day"] != lt.tm_yday:
        _pet["greet_day"] = lt.tm_yday
        cat = "monday" if wd == 0 else "friday" if wd == 4 else "weekend" if wd >= 5 else "morning"
        return {"text": pick(cat, "morning") if cat != "morning" else pick("morning"),
                "level": "info", "speak": True}

    s = snapshot()
    alerts = []
    b = s.get("battery")
    if b and not b["ac"] and b["pct"] <= 20:
        alerts.append("Заряд %d%%. Найди розетку, пока я не начал экономить на шутках." % b["pct"])
    x = s.get("x") or {}
    if x.get("online") is False:
        alerts.append("Интернет пропал. Проверь роутер — а я пока поищу сигнал антенной.")
    v = s.get("vpn") or {}
    if v.get("installed") and not v.get("on"):
        alerts.append("VPN не подключён. Скажи Джарвису «подключи VPN» — он всё сделает сам.")
    for r in s["recs"]:
        if r["level"] in ("crit", "warn") and not r["title"].startswith("VPN"):
            alerts.append(r["title"].rstrip(".") + ". " + r["text"])
            break
    for a in alerts:                          # одно и то же предупреждение — не чаще раза в 20 минут
        if now - _pet["alert_seen"].get(a, 0) > 1200:
            _pet["alert_seen"][a] = now
            return {"text": a, "level": "warn", "speak": False}

    # долгая работа без перерыва: больше 90 минут, напоминаем не чаще раза в 25 минут
    if worked > 5400 and now - _pet["long_said"] > 1500:
        _pet["long_said"] = now
        return {"text": pick("long_work"), "level": "warn", "speak": True}

    # обычная болтовня: категория по времени + тематические
    if 0 <= h < 5:
        time_cat = "night"
    elif h >= 22:
        time_cat = "late"
    elif h >= 18:
        time_cat = "evening"
    elif h < 12:
        time_cat = "morning"
    else:
        time_cat = "day"
    weights = [(time_cat, 3), ("texts", 2), ("marketing", 2), ("health", 2), ("robot", 3),
               ("workshop", 2), ("motivation", 2), ("questions", 1), ("jokes", 3), ("facts", 2),
               ("compliments", 1), ("focus", 2), ("breaks", 1), ("dzen", 2), ("idle", 4), ("mischief", 3)]
    if wd == 0:
        weights.append(("monday", 1))
    if wd == 4:
        weights.append(("friday", 2))
    if wd >= 5:
        weights.append(("weekend", 2))
    if b and b.get("ac"):
        weights.append(("charging", 1))
    if not s["recs"] or all(r["level"] in ("ok", "tip") for r in s["recs"]):
        weights.append(("mac_good", 1))
    if time_cat == "night":
        weights += [("night", 2), ("breaks", 1)]
    cats = [c for c, w in weights for _ in range(w)]
    line_ = pick(random.choice(cats))
    # Джарвис теперь разговорчивый — Марк вслух говорит реже, чтобы не перебивать
    return {"text": line_, "level": "info", "speak": random.random() < 0.15 and time.time() - _brain["last_talk"] > 60}


def group_name(n):
    n = re.sub(r"\s+Helper.*$", "", n)
    if n.startswith("com.apple.WebKit"):
        return "Safari (WebKit)"
    return n


def snapshot():
    with LOCK:
        snap = {
            "cpu": S["cpu"], "cpu_hist": list(S["cpu_hist"]),
            "net": S["net"], "net_hist": list(S["net_hist"]),
            "procs": list(S["procs"]), "mem": S["mem"],
            "pressure_free": S["pressure_free"], "battery": S["battery"],
            "therm": S["therm"], "caches": S["caches"], "cores": list(S["cores"]),
        }
    snap["disk"] = (read_disk() if not DEMO else
                    {"total": 245 * 10 ** 9, "free": 81.6 * 10 ** 9, "used": 163.4 * 10 ** 9})
    snap["vpn"] = vpn_status({p["name"] for p in snap["procs"]})
    snap["x"] = SENSE.snapshot() if not DEMO else demo_x()
    snap["recs"], snap["score"] = build_recs(snap)

    procs = snap.pop("procs")
    snap["top_cpu"] = [{"name": p["name"], "cpu": round(p["cpu"], 1), "pid": p["pid"]}
                       for p in sorted(procs, key=lambda x: -x["cpu"])[:8]]
    groups = {}
    for p in procs:
        g = groups.setdefault(group_name(p["name"]), {"name": group_name(p["name"]), "mem": 0, "n": 0})
        g["mem"] += p["mem"]; g["n"] += 1
    snap["top_mem"] = sorted(groups.values(), key=lambda g: -g["mem"])[:8]
    snap["procs_count"] = len(procs)
    snap["uptime"] = int(time.time() - INFO["boot"]) if INFO.get("boot") else None
    snap["time"] = time.time()
    return snap


def full_state():
    """Всё для дашборда и виджета: датчики + состояние Джарвиса."""
    s = snapshot()
    _brain["score"] = s["score"]
    emo = current_emotion()
    st = settings()
    rs = sorted(reminders(), key=lambda r: r["at"])
    s["jarvis"] = {
        "emotion": emo, "emotion_name": EMOTION_NAMES.get(emo, emo), "talk": talk_mode(),
        "quiet_until": st.get("quiet_until", 0), "said_today": _brain["said_today"], "cmds": _brain["cmds"],
        "alive": int(time.time() - _brain["started"]), "lines": sum(len(v) for v in BANK.values()),
        "voice": st["voice"], "pet_voice": st["pet_voice"],
        "timer": dict(_timer) if _timer["kind"] else None,
        "reminder": rs[0] if rs else None, "reminders": len(rs),
        "convo": list(_convo)[-24:],
        "streak": load_json("stats.json", {}).get("streak", 0),
        "ach": len(load_json("stats.json", {}).get("ach", [])), "ach_total": len(ACHIEVEMENTS),
    }
    return s


def demo_x():
    return {"gpu": {"util": random.randint(5, 40)}, "gpu_hist": [random.randint(3, 40) for _ in range(60)],
            "disk_io": {"read": random.uniform(1e5, 9e6), "write": random.uniform(1e5, 4e6)},
            "disk_hist": [[random.uniform(1e5, 9e6), random.uniform(1e5, 4e6)] for _ in range(60)],
            "volumes": [{"name": "Backup", "total": 1e12, "free": 4.2e11}],
            "netinfo": {"iface": "en1", "ip": "192.168.0.170", "kind": "Wi-Fi"}, "ping": 23.4,
            "ping_hist": [random.uniform(15, 40) for _ in range(60)], "online": True,
            "peripherals": [{"name": "Magic Mouse", "pct": 64}, {"name": "Magic Keyboard", "pct": 12}],
            "security": {"filevault": False, "firewall": False, "sip": True, "gatekeeper": True},
            "audio": {"volume": 40, "muted": False}, "assertions": ["Music"], "thermal": 0,
            "hw": {"name": "Mac mini", "chip": "Apple M4", "memory": "16 GB", "gpu_cores": "10", "p_cores": 4,
                   "e_cores": 6, "displays": [{"name": "Maestro226DX", "res": "1920 x 1080 @ 60.00Hz"}]},
            "weather": {"city": "Moscow", "temp": 9, "feels": 7, "desc": "пасмурно", "wind": 3, "humidity": 78,
                        "max": 11, "min": 5}, "load": [2.1, 2.4, 2.2]}


# ───────────────────────── веб-сервер ─────────────────────────

SERVER_TOKEN = secrets.token_hex(16)
OPEN_POST = {"/api/tts"}          # безобидные команды без токена


def write_token():
    os.makedirs(SUPPORT, exist_ok=True)
    path = os.path.join(SUPPORT, "token")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(SERVER_TOKEN)
    os.chmod(path, 0o600)


STATIC = {"/": "ui.html", "/index.html": "ui.html", "/widget.html": "widget.html", "/pet.html": "pet.html"}
MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".css": "text/css", ".js": "text/javascript",
        ".png": "image/png", ".svg": "image/svg+xml", ".wav": "audio/wav"}


def clean_reply(r):
    r = reply(r)
    return {k: v for k, v in r.items() if not k.startswith("_")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, cache=False):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def js(self, obj):
        self.send(200, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def query(self):
        from urllib.parse import parse_qs, urlparse
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def do_GET(self):
        LAST_REQ[0] = time.time()
        path = self.path.split("?")[0]
        if path in STATIC:
            try:
                with open(os.path.join(HERE, STATIC[path]), "rb") as f:
                    html = f.read().replace(b"</head>", ('<script>window.JARVIS_TOKEN="%s"</script></head>'
                                                         % SERVER_TOKEN).encode(), 1)
                self.send(200, html, "text/html; charset=utf-8")
            except Exception:
                self.send(500, "%s не найден рядом с jarvis.py" % STATIC[path], "text/plain; charset=utf-8")
        elif path.startswith("/fonts/") or path.startswith("/assets/"):
            name = os.path.normpath(path.lstrip("/"))
            full = os.path.join(HERE, name)
            if name.startswith(("fonts", "assets")) and os.path.isfile(full):
                with open(full, "rb") as f:
                    self.send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"), cache=True)
            else:
                self.send(404, "нет файла", "text/plain")
        elif path == "/api/static":
            self.js(INFO)
        elif path.startswith("/api/ext/"):
            r = PLUG.handle_route("GET", path, self.query())
            self.js(r if r is not None else {"error": "нет маршрута"})
        elif path == "/api/stats":
            self.js(full_state())
        elif path == "/api/ping":
            self.send(200, "pong", "text/plain")
        elif path == "/api/idle":
            self.js({"idle": 0 if DEMO else round(idle_seconds())})
        elif path == "/api/pet/tip":
            self.js(with_voice(pet_tip()))
        elif path == "/api/pet/say":
            q = self.query()
            self.js(pet_say(q.get("cat", ""), speak=q.get("speak") == "1", **({"app": q["app"]} if q.get("app") else {})))
        elif path == "/api/announce":
            item = _voice_q.popleft() if _voice_q else None
            if item:
                log_line("jarvis", item["say"], item.get("emotion"))
            self.js(item or {"say": ""})
        elif path == "/api/triggers":
            self.js(triggers())
        elif path == "/api/pet/news":
            self.js(pet_news())
        elif path == "/api/pet/poke":
            self.js(pet_poke())
        elif path == "/api/greet":
            self.js(clean_reply(greet_reply()))
        elif path == "/api/settings":
            self.js(dict(settings(), speakers=JV.SPEAKERS, fx_list=JV.FX_LIST, emotions=EMOTION_NAMES,
                         voice_error=JV.last_error()))
        elif path == "/api/log":
            self.js(list(_convo))
        elif path == "/api/tts_audio":
            q = self.query()
            text = q.get("text", "")[:400]
            wav = None if DEMO or not text else synthesize(text, q.get("emotion"), kind=q.get("kind", "pet"))
            if wav:
                with open(wav, "rb") as f:
                    self.send(200, f.read(), "audio/wav")
            else:
                self.send(404, "нет звука", "text/plain")
        else:
            self.send(404, "not found", "text/plain")

    def do_POST(self):
        LAST_REQ[0] = time.time()
        path = self.path.split("?")[0]
        if path not in OPEN_POST and self.headers.get("X-Jarvis-Token") != SERVER_TOKEN:
            self.send(403, json.dumps({"error": "нет токена"}), "application/json")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if path.startswith("/api/ext/"):
            r = PLUG.handle_route("POST", path, body)
            self.js(r if r is not None else {"error": "нет маршрута"})
            return
        if path == "/api/happ/open":
            if not DEMO:
                open_happ()
            self.js({"ok": True})
        elif path == "/api/tts":
            wav = synthesize(str(body.get("text", ""))[:2500], body.get("emotion"), kind=body.get("kind", "jarvis"))
            self.js({"path": wav, "error": JV.last_error(), "duration": JV.wav_duration(wav) if wav else 0})
        elif path == "/api/happ/connect":
            if not DEMO:
                threading.Thread(target=connect_vpn, daemon=True).start()
            self.js({"ok": True, "result": click_result()})
        elif path == "/api/happ/last":
            self.js({"result": click_result()})
        elif path == "/api/voice":
            self.js(clean_reply(voice_command(body)))
        elif path == "/api/action":
            self.js(clean_reply(action_command(str(body.get("name", "")))))
        elif path == "/api/settings":
            self.js(save_settings(body))
        elif path == "/api/log":
            log_line(str(body.get("who", "you")), str(body.get("text", "")), body.get("emotion"))
            self.js({"ok": True})
        elif path == "/api/shutdown":
            ok, err = (True, "") if DEMO else shutdown_mac()
            self.js({"ok": ok, "error": err})
        elif path == "/api/restart":
            ok, err = (True, "") if DEMO else restart_mac()
            self.js({"ok": ok, "error": err})
        elif path == "/api/autostart":
            on = bool(body.get("on"))
            res = on if DEMO else set_autostart(on)
            self.js({"autostart": res})
        else:
            self.send(404, "not found", "text/plain")


def open_browser():
    chrome = "/Applications/Google Chrome.app"
    if os.path.exists(chrome):
        subprocess.Popen(["open", "-na", "Google Chrome", "--args",
                          "--app=" + URL, "--window-size=1440,900"])
    else:
        subprocess.Popen(["open", URL])


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>JARVIS</string>
<key>CFBundleDisplayName</key><string>JARVIS</string>
<key>CFBundleIdentifier</key><string>local.jarvis.monitor</string>
<key>CFBundleExecutable</key><string>JARVIS</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>3.0</string>
<key>CFBundleVersion</key><string>3</string>
<key>NSAppleEventsUsageDescription</key><string>JARVIS управляет звуком, блокировкой экрана и выключением мака по твоим командам.</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSMicrophoneUsageDescription</key><string>JARVIS слушает голосовые команды, например «выключи компьютер».</string>
<key>NSSpeechRecognitionUsageDescription</key><string>JARVIS распознаёт голосовые команды на русском.</string>
<key>NSAppTransportSecurity</key><dict>
  <key>NSAllowsLocalNetworking</key><true/>
  <key>NSAllowsArbitraryLoads</key><true/>
</dict>
%s
</dict></plist>
"""


def app_is_fresh(binary, src):
    """Собранное окно актуально: это бинарник (не старый скрипт) и он новее исходников."""
    try:
        with open(binary, "rb") as f:
            if f.read(2) == b"#!":
                return False
        newest = max([os.path.getmtime(src), os.path.getmtime(os.path.abspath(__file__))] +
                     [os.path.getmtime(os.path.join(HERE, n)) for n in CORE_FILES if os.path.exists(os.path.join(HERE, n))])
        return os.path.getmtime(binary) >= newest
    except Exception:
        return False


CORE_FILES = ("jarvis.py", "jarvis_voice.py", "jarvis_sense.py", "ui.html", "widget.html", "pet.html",
              "pet_phrases.json", "jarvis_phrases.json", "jarvis_plugins.py", "jarvis_brain.py", "jarvis_mcp.py")


def copy_core(res):
    """Кладёт ядро внутрь приложения, чтобы ему не нужен был доступ к Рабочему столу."""
    os.makedirs(res, exist_ok=True)
    for name in CORE_FILES:
        if os.path.exists(os.path.join(HERE, name)):
            shutil.copy2(os.path.join(HERE, name), os.path.join(res, name))
    for sub in ("fonts", "assets", "plugins"):
        src = os.path.join(HERE, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(res, sub), dirs_exist_ok=True)


VENV = os.path.join(SUPPORT, "venv")
VENV_PY = os.path.join(VENV, "bin", "python")
VENV_PKGS = ["numpy", "scipy", "torch"]


def setup_voice():
    """Отдельное окружение Python под этот мак (arm64) с нейросетью Silero + модель голоса."""
    if not os.path.exists(VENV_PY):
        print("Готовлю окружение для голоса — один раз, пара минут…")
        subprocess.run([sys.executable, "-m", "venv", VENV])
    ok = subprocess.run([VENV_PY, "-c", "import numpy, scipy, torch"], capture_output=True).returncode == 0
    if not ok:
        print("Ставлю нейросеть голоса (torch, около 300 МБ)…")
        subprocess.run([VENV_PY, "-m", "pip", "install", "-q", "--upgrade", "pip"])
        subprocess.run([VENV_PY, "-m", "pip", "install", "-q"] + VENV_PKGS)
    JV.download_model()
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def build_app(force=False):
    """Собирает ~/Applications/JARVIS.app с нативным окном. Возвращает путь или None."""
    import tempfile
    src = os.path.join(HERE, "JARVISApp.swift")
    macos = os.path.join(APP, "Contents", "MacOS")
    res = os.path.join(APP, "Contents", "Resources")
    binary = os.path.join(macos, "JARVIS")
    if not os.path.exists(src):
        print("Нет файла JARVISApp.swift рядом с jarvis.py")
        return None
    py = setup_voice()
    if not force and app_is_fresh(binary, src):
        copy_core(res)
        ensure_autostart_once()
        return APP

    print("Собираю приложение JARVIS — это 20–60 секунд, один раз…")
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    with open(src, encoding="utf-8") as f:
        code = f.read().replace("__PYTHON__", esc(py)).replace(
            "__SCRIPT__", esc(os.path.join(APP, "Contents", "Resources", "jarvis.py")))
    tmp = tempfile.mkdtemp(prefix="jarvis-build-")
    main_swift = os.path.join(tmp, "main.swift")
    out = os.path.join(tmp, "JARVIS")
    with open(main_swift, "w", encoding="utf-8") as f:
        f.write(code)
    try:
        r = subprocess.run(["xcrun", "swiftc", "-O", "-o", out, main_swift,
                            "-framework", "Cocoa", "-framework", "WebKit", "-framework", "Carbon",
                            "-framework", "Speech", "-framework", "AVFoundation"],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        print("Компилятор Swift не запустился:", e)
        return None
    if r.returncode != 0 or not os.path.exists(out):
        print("Сборка не удалась:\n" + (r.stderr or "")[-2500:])
        return None

    os.makedirs(macos, exist_ok=True)
    os.makedirs(res, exist_ok=True)
    shutil.copy2(out, binary)
    os.chmod(binary, 0o755)
    copy_core(res)
    has_icon = False
    icon_png = os.path.join(HERE, "icon.png")
    if os.path.exists(icon_png):
        sh(["sips", "-s", "format", "icns", icon_png, "--out", os.path.join(res, "AppIcon.icns")], timeout=30)
        has_icon = os.path.exists(os.path.join(res, "AppIcon.icns"))
    with open(os.path.join(APP, "Contents", "Info.plist"), "w") as f:
        f.write(PLIST % ("<key>CFBundleIconFile</key><string>AppIcon</string>" if has_icon else ""))
    sh(["touch", APP])
    shutil.rmtree(tmp, ignore_errors=True)
    ensure_autostart_once()
    print("Готово: " + APP)
    return APP


def server_running():
    try:
        urllib.request.urlopen(URL + "api/ping", timeout=1)
        return True
    except Exception:
        return False


def run_server(open_ui):
    if server_running():
        if open_ui:
            open_browser()
        return
    if not DEMO:
        write_token()
    if not DEMO:
        SENSE.start(lambda: settings().get("weather_city", ""))
    BRAIN.init(sys.modules[__name__])
    BRAIN.register_builtin_tools()
    PLUG.load_all(sys.modules[__name__])
    MCP.register_all(PLUG.PluginAPI(sys.modules[__name__], "mcp").tool)
    for target in (fast_loop, slow_loop, caches_loop, warm_tts, articles_loop, reminders_loop, timer_loop, brief_loop,
                   events_loop, chatter_loop):
        threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("J.A.R.V.I.S. онлайн → %s  (Ctrl+C — выключить)" % URL)
    if open_ui and not DEMO:
        time.sleep(1.5)
        open_browser()
    try:
        while True:
            time.sleep(5)
            if not DEMO and time.time() - LAST_REQ[0] > IDLE_EXIT:
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()


def main():
    args = sys.argv[1:]
    if "--autostart" in args:
        autostart()
        return
    if "--server" in args or DEMO:
        if "--server" in args:
            print("[%s] ядро стартует, python %s" % (time.ctime(), sys.version.split()[0]), flush=True)
        run_server(open_ui=False)
        return
    if sys.platform == "darwin" and "--browser" not in args:
        app = build_app(force="--rebuild" in args)
        if app:
            subprocess.Popen(["open", app])
            print("JARVIS запущен. Дальше открывай его через Spotlight: Cmd+Пробел → JARVIS.")
            return
        print("Окно приложения собрать не вышло — открываю в браузере.")
    run_server(open_ui=True)


if __name__ == "__main__":
    main()
