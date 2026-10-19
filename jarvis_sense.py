# -*- coding: utf-8 -*-
"""
Датчики JARVIS для полного дашборда: ядра процессора, видеоядро, диск, сеть, периферия,
защита, термо-режим, громкость, железо. Только чтение — ничего в системе не меняет.
"""
import ctypes
import ctypes.util
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque

ENV = dict(os.environ, LC_ALL="C", LANG="C")


def sh(cmd, timeout=6):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV).stdout
    except Exception:
        return ""


# ───────────────────────── процессор по ядрам (mach host_processor_info) ─────────────────────────

_libc = None
_prev_ticks = [None]


def _mach():
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        lib.host_processor_info.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                                            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint)),
                                            ctypes.POINTER(ctypes.c_uint)]
        lib.host_processor_info.restype = ctypes.c_int
        lib.vm_deallocate.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t]
        _libc = lib
    return _libc


def read_core_ticks():
    lib = _mach()
    count = ctypes.c_uint()
    info = ctypes.POINTER(ctypes.c_uint)()
    n = ctypes.c_uint()
    if lib.host_processor_info(lib.mach_host_self(), 2, ctypes.byref(count), ctypes.byref(info), ctypes.byref(n)) != 0:
        return None
    ticks = [[info[i * 4 + j] for j in range(4)] for i in range(count.value)]   # user, system, idle, nice
    try:
        task = ctypes.c_uint.in_dll(lib, "mach_task_self_").value
        lib.vm_deallocate(task, ctypes.cast(info, ctypes.c_void_p).value, n.value * 4)
    except Exception:
        pass
    return ticks


def read_cpu_cores():
    """Возвращает (общая {"user","sys","idle"}, [загрузка ядра в %]) по разнице с прошлым замером."""
    try:
        now = read_core_ticks()
    except Exception:
        return None, []
    if not now:
        return None, []
    prev = _prev_ticks[0]
    _prev_ticks[0] = now
    if not prev or len(prev) != len(now):
        return None, []
    cores, tu, ts, ti = [], 0, 0, 0
    for a, b in zip(prev, now):
        du, dsy, di, dn = [(b[k] - a[k]) % (1 << 32) for k in range(4)]
        tot = du + dsy + di + dn
        cores.append(round(100.0 * (tot - di) / tot, 1) if tot else 0.0)
        tu += du + dn
        ts += dsy
        ti += di
    tot = tu + ts + ti
    if not tot:
        return None, cores
    return {"user": 100.0 * tu / tot, "sys": 100.0 * ts / tot, "idle": 100.0 * ti / tot}, cores


# ───────────────────────── термо-режим (NSProcessInfo.thermalState) ─────────────────────────

_objc = {}


def thermal_state():
    """0 — норма, 1 — тепло, 2 — горячо, 3 — критично. None — не удалось узнать."""
    try:
        if not _objc:
            objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            addr = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
            _objc["id"] = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(addr)
            _objc["long"] = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(addr)
            _objc["pi"] = _objc["id"](objc.objc_getClass(b"NSProcessInfo"), objc.sel_registerName(b"processInfo"))
            _objc["sel"] = objc.sel_registerName(b"thermalState")
        return int(_objc["long"](_objc["pi"], _objc["sel"]))
    except Exception:
        return None


# ───────────────────────── видеоядро ─────────────────────────

def read_gpu():
    out = sh(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=4)
    m = re.search(r'"Device Utilization %"\s*=\s*(\d+)', out)
    r = re.search(r'"Renderer Utilization %"\s*=\s*(\d+)', out)
    mem = re.search(r'"In use system memory"\s*=\s*(\d+)', out)
    if not m:
        return None
    return {"util": int(m.group(1)), "renderer": int(r.group(1)) if r else None,
            "mem": int(mem.group(1)) if mem else None}


# ───────────────────────── диск: чтение/запись ─────────────────────────

_disk_prev = [None, 0.0]


def read_disk_io():
    out = sh(["ioreg", "-c", "IOBlockStorageDriver", "-r", "-w", "0"], timeout=4)
    rd = sum(int(x) for x in re.findall(r'"Bytes \(Read\)"=(\d+)', out))
    wr = sum(int(x) for x in re.findall(r'"Bytes \(Write\)"=(\d+)', out))
    now = time.time()
    res = None
    if _disk_prev[0] is not None:
        dt = max(now - _disk_prev[1], 0.5)
        res = {"read": max(rd - _disk_prev[0][0], 0) / dt, "write": max(wr - _disk_prev[0][1], 0) / dt}
    _disk_prev[0], _disk_prev[1] = (rd, wr), now
    return res


def read_volumes():
    vols = []
    try:
        for name in sorted(os.listdir("/Volumes")):
            p = os.path.join("/Volumes", name)
            if os.path.islink(p) or name.startswith(".") or name in ("Recovery", "Preboot", "VM", "Update"):
                continue
            try:
                u = shutil.disk_usage(p)
            except Exception:
                continue
            if u.total < 64 * 1024 * 1024:
                continue
            vols.append({"name": name, "total": u.total, "free": u.free})
    except Exception:
        pass
    return vols


# ───────────────────────── сеть ─────────────────────────

def default_iface():
    m = re.search(r"interface:\s*(\S+)", sh(["route", "-n", "get", "default"], timeout=3))
    return m.group(1) if m else None


_ports = {"t": 0, "map": {}}


def port_name(iface):
    if time.time() - _ports["t"] > 600:
        _ports["t"] = time.time()
        out = sh(["networksetup", "-listallhardwareports"], timeout=6)
        mp = {}
        for blk in out.split("\n\n"):
            a = re.search(r"Hardware Port:\s*(.+)", blk)
            b = re.search(r"Device:\s*(\S+)", blk)
            if a and b:
                mp[b.group(1)] = a.group(1).strip()
        _ports["map"] = mp
    return _ports["map"].get(iface or "", iface or "—")


def read_net_info():
    iface = default_iface()
    ip = sh(["ipconfig", "getifaddr", iface], timeout=3).strip() if iface else ""
    kind = port_name(iface) if iface else "нет сети"
    if iface and iface.startswith("utun"):
        kind = "VPN-туннель"
    return {"iface": iface, "ip": ip, "kind": kind}


def ping(host="1.1.1.1"):
    out = sh(["/sbin/ping", "-c", "1", "-t", "2", host], timeout=4)
    m = re.search(r"time[=<]([\d.]+) ms", out)
    return float(m.group(1)) if m else None


def online():
    for host in (("1.1.1.1", 53), ("8.8.8.8", 53), ("77.88.8.8", 53)):
        try:
            c = socket.create_connection(host, timeout=1.5)
            c.close()
            return True
        except Exception:
            continue
    return False


# ───────────────────────── периферия ─────────────────────────

def read_peripherals():
    out = sh(["ioreg", "-r", "-l", "-w", "0", "-k", "BatteryPercent"], timeout=6)
    devs = []
    for blk in re.split(r"\n(?=\+-o )", out):
        p = re.search(r'"Product"\s*=\s*"([^"]+)"', blk)
        b = re.search(r'"BatteryPercent"\s*=\s*(\d+)', blk)
        if p and b:
            name = p.group(1)
            if not any(d["name"] == name for d in devs):
                devs.append({"name": name, "pct": int(b.group(1))})
    return devs


# ───────────────────────── защита ─────────────────────────

def read_security():
    fv = sh(["fdesetup", "isactive"], timeout=5).strip()
    fw = sh(["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], timeout=5)
    sip = sh(["csrutil", "status"], timeout=5)
    gk = sh(["spctl", "--status"], timeout=5) + sh(["spctl", "--status"], timeout=5)
    return {
        "filevault": True if fv == "true" else False if fv == "false" else None,
        "firewall": (True if re.search(r"enabled|State = [12]", fw) else False) if fw else None,
        "sip": (True if "enabled" in sip else False) if sip else None,
        "gatekeeper": (True if "enabled" in gk else False) if gk.strip() else None,
    }


# ───────────────────────── звук ─────────────────────────

def read_audio():
    out = sh(["osascript", "-e", "set v to get volume settings",
              "-e", "return (output volume of v as text) & \",\" & (output muted of v as text)"], timeout=4).strip()
    m = re.match(r"(\d+|missing value),(true|false)", out)
    if not m:
        return None
    vol = None if m.group(1) == "missing value" else int(m.group(1))
    return {"volume": vol, "muted": m.group(2) == "true"}


def set_volume(pct=None, muted=None):
    if pct is not None:
        sh(["osascript", "-e", "set volume output volume %d" % max(0, min(100, int(pct)))], timeout=4)
    if muted is not None:
        sh(["osascript", "-e", "set volume output muted %s" % ("true" if muted else "false")], timeout=4)


# ───────────────────────── сон: кто не даёт ─────────────────────────

def read_assertions():
    out = sh(["pmset", "-g", "assertions"], timeout=5)
    who = []
    for m in re.finditer(r"pid \d+\(([^)]+)\):.*?(PreventUserIdleSystemSleep|PreventUserIdleDisplaySleep|PreventSystemSleep)", out):
        if m.group(1) not in who:
            who.append(m.group(1))
    return who[:6]


# ───────────────────────── железо (один раз) ─────────────────────────

def read_hardware():
    out = sh(["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "-json"], timeout=25)
    hw = {}
    try:
        d = json.loads(out)
        h = (d.get("SPHardwareDataType") or [{}])[0]
        hw["name"] = h.get("machine_name")
        hw["chip"] = h.get("chip_type") or h.get("cpu_type")
        hw["cores_txt"] = h.get("number_processors")
        hw["memory"] = h.get("physical_memory")
        hw["serial_tail"] = (h.get("serial_number") or "")[-4:]
        g = (d.get("SPDisplaysDataType") or [{}])[0]
        hw["gpu_cores"] = g.get("sppci_cores")
        disp = []
        for x in g.get("spdisplays_ndrvs", []) or []:
            disp.append({"name": x.get("_name"), "res": x.get("_spdisplays_resolution") or x.get("_spdisplays_pixels")})
        hw["displays"] = disp
    except Exception:
        pass

    def sc(name):
        try:
            return int(sh(["sysctl", "-n", name]).strip())
        except Exception:
            return None
    hw["p_cores"] = sc("hw.perflevel0.logicalcpu")
    hw["e_cores"] = sc("hw.perflevel1.logicalcpu")
    return hw


# ───────────────────────── погода (wttr.in) ─────────────────────────

WEATHER_RU = {
    "Sunny": "солнечно", "Clear": "ясно", "Partly cloudy": "переменная облачность", "Cloudy": "облачно",
    "Overcast": "пасмурно", "Mist": "дымка", "Fog": "туман", "Light rain": "небольшой дождь",
    "Patchy rain possible": "местами дождь", "Patchy rain nearby": "местами дождь", "Moderate rain": "дождь",
    "Heavy rain": "сильный дождь", "Light snow": "небольшой снег", "Moderate snow": "снег",
    "Heavy snow": "сильный снег", "Light drizzle": "морось", "Thundery outbreaks possible": "возможна гроза",
    "Light rain shower": "небольшой ливень", "Patchy light rain": "местами небольшой дождь",
    "Light sleet": "мокрый снег", "Patchy snow possible": "местами снег", "Freezing fog": "морозный туман",
}


def tz_city():
    try:
        p = os.path.realpath("/etc/localtime")
        return p.split("zoneinfo/")[-1].split("/")[-1].replace("_", " ")
    except Exception:
        return ""


def fetch_weather(city=""):
    import urllib.parse
    city = city or tz_city()
    url = "https://wttr.in/%s?format=j1&lang=ru" % urllib.parse.quote(city)
    out = subprocess.run(["curl", "-s", "-f", "-m", "10", url], capture_output=True, text=True, timeout=12).stdout
    d = json.loads(out)
    c = d["current_condition"][0]
    desc_en = (c.get("weatherDesc") or [{}])[0].get("value", "")
    desc = ((c.get("lang_ru") or [{}])[0].get("value") or WEATHER_RU.get(desc_en, desc_en)).lower()
    area = ""
    try:
        area = d["nearest_area"][0]["areaName"][0]["value"]
    except Exception:
        pass
    day = (d.get("weather") or [{}])[0]
    return {"city": city, "area": area, "temp": int(c["temp_C"]), "feels": int(c["FeelsLikeC"]),
            "desc": desc, "desc_en": desc_en, "wind": round(int(c.get("windspeedKmph", 0)) / 3.6),
            "humidity": int(c.get("humidity", 0)), "max": int(day.get("maxtempC", c["temp_C"])),
            "min": int(day.get("mintempC", c["temp_C"])), "t": time.time()}


# ───────────────────────── фоновый сбор ─────────────────────────

X = {
    "cores": [], "gpu": None, "gpu_hist": deque(maxlen=90), "disk_io": None, "disk_hist": deque(maxlen=90),
    "volumes": [], "netinfo": {}, "ping": None, "ping_hist": deque(maxlen=60), "online": True,
    "peripherals": [], "security": {}, "audio": None, "assertions": [], "thermal": None,
    "hw": {}, "weather": None, "load": None,
}
XLOCK = threading.Lock()


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def loop_fast():
    while True:
        g, io = _safe(read_gpu), _safe(read_disk_io)
        with XLOCK:
            X["gpu"] = g
            X["gpu_hist"].append(g["util"] if g else 0)
            if io:
                X["disk_io"] = io
                X["disk_hist"].append([io["read"], io["write"]])
            try:
                X["load"] = [round(v, 2) for v in os.getloadavg()]
            except Exception:
                pass
        time.sleep(2)


def loop_medium():
    n = 0
    while True:
        p, on = _safe(ping), _safe(online)
        th = _safe(thermal_state)
        with XLOCK:
            X["ping"] = p
            X["ping_hist"].append(p or 0)
            X["online"] = bool(on) if on is not None else X["online"]
            X["thermal"] = th
        if n % 2 == 0:
            ni, au, vols = _safe(read_net_info), _safe(read_audio), _safe(read_volumes)
            with XLOCK:
                X["netinfo"] = ni or {}
                X["audio"] = au
                X["volumes"] = vols or []
        if n % 6 == 0:
            per, ass = _safe(read_peripherals), _safe(read_assertions)
            with XLOCK:
                X["peripherals"] = per or []
                X["assertions"] = ass or []
        n += 1
        time.sleep(10)


def loop_slow(weather_city=lambda: ""):
    hw = _safe(read_hardware) or {}
    with XLOCK:
        X["hw"] = hw
    while True:
        sec = _safe(read_security)
        with XLOCK:
            X["security"] = sec or {}
        w = X.get("weather")
        if not w or time.time() - w.get("t", 0) > 1800:
            nw = _safe(fetch_weather, weather_city())
            if nw:
                with XLOCK:
                    X["weather"] = nw
        time.sleep(600)


def start(weather_city=lambda: ""):
    for fn, args in ((loop_fast, ()), (loop_medium, ()), (loop_slow, (weather_city,))):
        threading.Thread(target=fn, args=args, daemon=True).start()


def snapshot():
    with XLOCK:
        return {
            "gpu": X["gpu"], "gpu_hist": list(X["gpu_hist"]), "disk_io": X["disk_io"],
            "disk_hist": list(X["disk_hist"]), "volumes": X["volumes"], "netinfo": X["netinfo"],
            "ping": X["ping"], "ping_hist": list(X["ping_hist"]), "online": X["online"],
            "peripherals": X["peripherals"], "security": X["security"], "audio": X["audio"],
            "assertions": X["assertions"], "thermal": X["thermal"], "hw": X["hw"], "weather": X["weather"],
            "load": X["load"],
        }


if __name__ == "__main__":
    read_cpu_cores(); time.sleep(1)
    print("cpu", read_cpu_cores())
    print("thermal", thermal_state())
    print("gpu", read_gpu())
    read_disk_io(); time.sleep(1); print("disk io", read_disk_io())
    print("vols", read_volumes())
    print("net", read_net_info(), "ping", ping(), "online", online())
    print("periph", read_peripherals())
    print("sec", read_security())
    print("audio", read_audio())
    print("assert", read_assertions())
    print("hw", read_hardware())
    print("tz", tz_city())
    try:
        print("weather", fetch_weather())
    except Exception as e:
        print("weather err", e)
