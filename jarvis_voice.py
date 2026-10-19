# -*- coding: utf-8 -*-
"""
Голос JARVIS: подготовка текста к синтезу, нейросеть Silero (локально), эмоции, эффекты.

Движки по приоритету: Silero v5 (основной) → голос macOS (запасной).
Эмоция фразы меняет темп и высоту голоса. Эффект (fx) — «обработка динамиком».
"""
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import wave

SUPPORT = os.path.expanduser("~/Library/Application Support/JARVIS")
VOICE_DIR = os.path.join(SUPPORT, "voice")
TTS_CACHE = os.path.join(SUPPORT, "tts")
SILERO_FILE = "v5_6_ru.pt"
SILERO_URL = "https://models.silero.ai/models/tts/ru/" + SILERO_FILE
CACHE_LIMIT = 300 * 1024 * 1024
SR = 48000

# ───────────────────────── профили ─────────────────────────

DEFAULT_VOICE = {"engine": "silero", "speaker": "aidar", "fx": "jarvis", "pitch": -10, "rate": 96}
DEFAULT_PET_VOICE = {"engine": "silero", "speaker": "aidar", "fx": "mark", "pitch": 28, "rate": 110}

SPEAKERS = [
    {"id": "aidar", "name": "Айдар", "note": "чистый, ровный баритон"},
    {"id": "eugene", "name": "Евгений", "note": "ниже и плотнее"},
]
FX_LIST = [
    {"id": "jarvis", "name": "Джарвис", "note": "студийный ИИ: тёплый, с лёгким объёмом"},
    {"id": "clean", "name": "Чистый", "note": "без обработки"},
    {"id": "helmet", "name": "Шлем", "note": "радио внутри брони"},
    {"id": "hologram", "name": "Голограмма", "note": "объёмный, с переливом"},
    {"id": "legacy", "name": "Старый", "note": "металлический, как было"},
]

# эмоция → (сдвиг высоты в %, множитель темпа в %)
EMOTIONS = {
    "calm":      (0, 0),
    "happy":     (6, 3),
    "witty":     (3, 2),
    "concerned": (-5, -6),
    "alarmed":   (8, 8),
    "proud":     (2, -3),
    "excited":   (10, 9),
    "tired":     (-7, -10),
    "curious":   (7, 0),
    "warm":      (-2, -5),
}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ───────────────────────── числа словами ─────────────────────────

_ONES_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
           "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
           "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
         "девяносто"]
_HUND = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот",
         "девятьсот"]
_SCALES = [(("тысяча", "тысячи", "тысяч"), "f"), (("миллион", "миллиона", "миллионов"), "m"),
           (("миллиард", "миллиарда", "миллиардов"), "m")]


def plural(n, forms):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _triad(n, gender):
    out = []
    h, r = divmod(n, 100)
    if h:
        out.append(_HUND[h])
    if r >= 20:
        t, r = divmod(r, 10)
        out.append(_TENS[t])
    if r:
        w = _ONES_M[r]
        if r == 1 and gender == "f":
            w = "одна"
        elif r == 2 and gender == "f":
            w = "две"
        elif r == 1 and gender == "n":
            w = "одно"
        out.append(w)
    return out


def num_words(n, gender="m"):
    n = int(n)
    if n == 0:
        return "ноль"
    neg = n < 0
    n = abs(n)
    parts = []
    triads = []
    while n:
        n, r = divmod(n, 1000)
        triads.append(r)
    for i in range(len(triads) - 1, -1, -1):
        t = triads[i]
        if not t:
            continue
        if i == 0:
            parts += _triad(t, gender)
        else:
            forms, g = _SCALES[min(i, 3) - 1]
            parts += _triad(t, g) + [plural(t, forms)]
    return ("минус " if neg else "") + " ".join(parts)


def decimal_words(int_part, frac_str):
    frac_str = frac_str[:2]
    whole = num_words(int(int_part), "f") + " " + plural(int(int_part), ("целая", "целых", "целых"))
    f = int(frac_str)
    if f == 0:
        return num_words(int(int_part))
    unit = ("десятая", "десятых", "десятых") if len(frac_str) == 1 else ("сотая", "сотых", "сотых")
    return whole + " " + num_words(f, "f") + " " + plural(f, unit)


# ───────────────────────── латиница и сокращения ─────────────────────────

SPEAK_AS = {
    "J.A.R.V.I.S.": "Джарвис", "JARVIS": "Джарвис", "Jarvis": "Джарвис",
    "VPN": "ви-пи-эн", "Claude": "Клод", "Safari": "Сафари", "Happ": "Хапп", "Telegram": "Телеграм",
    "Google Chrome": "Гугл Хром", "Chrome": "Хром", "Google": "Гугл", "Steam": "Стим", "Figma": "Фигма",
    "Photoshop": "Фотошоп", "Xcode": "Икс-код", "Finder": "Файндер", "macOS": "мак-о-эс", "MacBook": "Макбук",
    "Mac mini": "Мак мини", "Mac": "Мак", "iPhone": "Айфон", "iPad": "Айпэд", "Wi-Fi": "вай-фай", "WiFi": "вай-фай",
    "Bluetooth": "блютус", "USB": "ю-эс-би", "CPU": "процессор", "GPU": "видеоядро", "RAM": "оперативка",
    "SSD": "эс-эс-дэ", "IP": "ай-пи", "Apple": "Эппл", "Dzen": "Дзен",
    "YouTube": "Ютуб", "Spotlight": "Спотлайт", "WindowServer": "Виндоу-сервер", "kernel_task": "кёрнел таск",
    "Zoom": "Зум", "xray": "икс-рэй", "sing-box": "синг-бокс", "Time Machine": "Тайм машин",
    "Magic Mouse": "Мэджик маус", "Magic Keyboard": "Мэджик кейборд", "Magic Trackpad": "Мэджик трекпад",
    "AirPods": "Эйрподс", "Cmd+V": "команд-вэ", "Cmd+C": "команд-си", "Cmd+Q": "команд-кью",
    "Cmd": "команд", "Enter": "Энтер", "Music": "Музыка", "Terminal": "Терминал", "Notes": "Заметки",
    "Yandex": "Яндекс", "Opera": "Опера", "Firefox": "Файрфокс", "Arc": "Арк", "Discord": "Дискорд",
    "WhatsApp": "Ватсап", "Spotify": "Спотифай", "Notion": "Ношн", "Slack": "Слак", "Trello": "Трелло",
    "Mark": "Марк", "M4": "эм четыре", "M1": "эм один", "M2": "эм два", "M3": "эм три", "Intel": "Интел",
    "FileVault": "Файл-волт", "SIP": "эс-ай-пи", "Gatekeeper": "Гейткипер", "Ethernet": "изернет",
    "Tony Stark": "Тони Старк", "Stark": "Старк", "Iron Man": "Железный человек", "HUD": "худ",
    "OK": "окей", "ok": "окей", "PDF": "пэ-дэ-эф", "AI": "эй-ай",
}
try:                                   # своё произношение: ~/JARVIS/Произношение.json {"слово": "как читать"}
    import json as _json
    with open(os.path.expanduser("~/JARVIS/Произношение.json"), encoding="utf-8") as _f:
        SPEAK_AS.update({str(k): str(v) for k, v in _json.load(_f).items() if not str(k).startswith("_")})
except Exception:
    pass
_SPEAK_RE = None

_TR = [("tion", "шн"), ("sch", "ш"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("th", "т"), ("ph", "ф"),
       ("kh", "х"), ("ck", "к"), ("qu", "кв"), ("ee", "и"), ("oo", "у"), ("ou", "ау"), ("ea", "и"),
       ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ye", "е"), ("ai", "эй"), ("ay", "эй"), ("oy", "ой"),
       ("ey", "ей"), ("ts", "ц"), ("x", "кс"), ("w", "у"), ("j", "дж"), ("q", "к")]
_TR1 = {"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "k": "к",
        "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
        "y": "и", "z": "з"}
_LETTERS = {"a": "эй", "b": "би", "c": "си", "d": "ди", "e": "и", "f": "эф", "g": "джи", "h": "эйч", "i": "ай",
            "j": "джей", "k": "кей", "l": "эл", "m": "эм", "n": "эн", "o": "оу", "p": "пи", "q": "кью",
            "r": "ар", "s": "эс", "t": "ти", "u": "ю", "v": "ви", "w": "дабл-ю", "x": "икс", "y": "уай",
            "z": "зед"}


def translit(word):
    if word.isupper() and len(word) <= 4:                       # аббревиатура: по буквам
        return "-".join(_LETTERS.get(c.lower(), "") for c in word if c.isalpha())
    w = word.lower()
    out = ""
    i = 0
    while i < len(w):
        for a, b in _TR:
            if w.startswith(a, i):
                out += b
                i += len(a)
                break
        else:
            c = w[i]
            if c == "c" and i + 1 < len(w) and w[i + 1] in "eiy":
                out += "с"
            elif c == "e" and i == len(w) - 1 and len(w) > 3:
                pass                                            # немая «e» на конце
            else:
                out += _TR1.get(c, c)
            i += 1
    return out[:1].upper() + out[1:] if word[:1].isupper() else out


# ───────────────────────── нормализация для синтеза ─────────────────────────

UNITS = [  # (регулярка единицы, формы)
    (r"ГБ|Гб|GB", ("гигабайт", "гигабайта", "гигабайт")),
    (r"ТБ|Тб|TB", ("терабайт", "терабайта", "терабайт")),
    (r"МБ/с|Мб/с|MB/s", ("мегабайт в секунду", "мегабайта в секунду", "мегабайт в секунду")),
    (r"КБ/с|Кб/с|KB/s", ("килобайт в секунду", "килобайта в секунду", "килобайт в секунду")),
    (r"МБ|Мб|MB", ("мегабайт", "мегабайта", "мегабайт")),
    (r"КБ|Кб|KB", ("килобайт", "килобайта", "килобайт")),
    (r"°C|°С|°", ("градус", "градуса", "градусов")),
    (r"мс|ms", ("миллисекунда", "миллисекунды", "миллисекунд")),
    (r"ГГц|GHz", ("гигагерц", "гигагерца", "гигагерц")),
]


def normalize(text):
    global _SPEAK_RE
    t = str(text)
    # без обращения «сэр»
    t = re.sub(r",?\s*\bсэр\b\s*,?", " ", t, flags=re.IGNORECASE)
    t = t.replace("ё", "ё")
    if _SPEAK_RE is None:
        keys = sorted(SPEAK_AS, key=len, reverse=True)
        _SPEAK_RE = re.compile(r"(?<![\w])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w])")
    t = _SPEAK_RE.sub(lambda m: SPEAK_AS[m.group(1)], t)
    t = re.sub(r"[«»“”„\"*_#`~<>\[\]{}|]", " ", t)
    t = t.replace("—", ", ").replace("–", ", ").replace("…", "...").replace("→", ", ").replace("·", ", ")
    t = t.replace("№", " номер ").replace("&", " и ").replace("×", " на ").replace("+", " плюс ")

    # время 15:00 / 9:05
    def clock(m):
        h, mi = int(m.group(1)), m.group(2)
        hw = num_words(h)
        if mi == "00":
            return hw + " ноль-ноль"
        if mi.startswith("0"):
            return hw + " ноль " + num_words(int(mi))
        return hw + " " + num_words(int(mi))
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, t)

    # проценты
    def pct(m):
        if m.group(2):
            return decimal_words(m.group(1), m.group(2)) + " процента"
        n = int(m.group(1))
        return num_words(n) + " " + plural(n, ("процент", "процента", "процентов"))
    t = re.sub(r"(\d+)(?:[.,](\d+))?\s?%", pct, t)

    # числа с единицами
    for rx, forms in UNITS:
        def unit(m, forms=forms):
            if m.group(2):
                return decimal_words(m.group(1), m.group(2)) + " " + forms[1]
            n = int(m.group(1))
            return num_words(n) + " " + plural(n, forms)
        t = re.sub(r"(\d+)(?:[.,](\d+))?\s?(?:%s)(?![\wа-яА-Я])" % rx, unit, t)

    # десятичные и простые числа
    t = re.sub(r"(\d+)[.,](\d+)", lambda m: decimal_words(m.group(1), m.group(2)), t)
    t = re.sub(r"(\d+)-(?:й|я|е|го|му|м)\b", lambda m: num_words(int(m.group(1))), t)
    t = re.sub(r"\d+", lambda m: num_words(int(m.group(0))) if len(m.group(0)) < 13 else " ".join(
        num_words(int(c)) for c in m.group(0)), t)

    # оставшаяся латиница
    t = re.sub(r"[A-Za-z][A-Za-z'\-]*", lambda m: translit(m.group(0).replace("'", "")), t)
    t = re.sub(r"[^\w\s.,!?;:()\-ёЁ]", " ", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = re.sub(r"([.,!?;:]){2,}", lambda m: m.group(0)[0] if m.group(0)[0] != "." else ".", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;")
    return t


def split_sentences(t, limit=600):
    parts = re.split(r"(?<=[.!?])\s+", t)
    out, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 <= limit:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                out.append(cur)
            while len(p) > limit:                       # очень длинное предложение режем по запятым
                k = p.rfind(",", 0, limit)
                k = k if k > 50 else limit
                out.append(p[:k + 1])
                p = p[k + 1:].strip()
            cur = p
    if cur:
        out.append(cur)
    return [x for x in out if re.search(r"\w", x)]


# ───────────────────────── эффекты ─────────────────────────

def _peaking(f0, gain_db, q, sr):
    import numpy as np
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / (2 * q)
    b = [1 + al * A, -2 * np.cos(w), 1 - al * A]
    a = [1 + al / A, -2 * np.cos(w), 1 - al / A]
    return np.array(b) / a[0], np.array(a) / a[0]


def _shelf_low(f0, gain_db, sr):
    import numpy as np
    A = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    al = np.sin(w) / 2 * np.sqrt(2)
    c = np.cos(w)
    b = [A * ((A + 1) - (A - 1) * c + 2 * np.sqrt(A) * al), 2 * A * ((A - 1) - (A + 1) * c),
         A * ((A + 1) - (A - 1) * c - 2 * np.sqrt(A) * al)]
    a = [(A + 1) + (A - 1) * c + 2 * np.sqrt(A) * al, -2 * ((A - 1) + (A + 1) * c),
         (A + 1) + (A - 1) * c - 2 * np.sqrt(A) * al]
    return np.array(b) / a[0], np.array(a) / a[0]


def _comb(x, d, g):
    import numpy as np
    from scipy.signal import lfilter
    b = np.zeros(d + 1); b[d] = 1.0
    a = np.zeros(d + 1); a[0] = 1.0; a[d] = -g
    return lfilter(b, a, x)


def _allpass(x, d, g):
    import numpy as np
    from scipy.signal import lfilter
    b = np.zeros(d + 1); b[0] = -g; b[d] = 1.0
    a = np.zeros(d + 1); a[0] = 1.0; a[d] = -g
    return lfilter(b, a, x)


def _reverb(x, sr, size=1.0, fb=0.74, damp=5200):
    import numpy as np
    from scipy.signal import butter, sosfilt
    combs = [29.7, 37.1, 41.1, 43.7]
    y = np.zeros_like(x)
    for ms in combs:
        y += _comb(x, max(1, int(sr * ms * size / 1000)), fb)
    y /= len(combs)
    for ms, g in ((5.0, 0.7), (1.7, 0.7)):
        y = _allpass(y, max(1, int(sr * ms / 1000)), g)
    return sosfilt(butter(2, damp, "low", fs=sr, output="sos"), y)


def _chorus(x, sr, base_ms=9.0, depth_ms=0.3, rate=0.35, phase=0.0):
    import numpy as np
    n = np.arange(len(x))
    d = (base_ms + depth_ms * np.sin(2 * np.pi * rate * n / sr + phase)) * sr / 1000
    return np.interp(n - d, n, x, left=0.0, right=0.0)


def _compress(x, sr, thresh_db=-20, ratio=3.0, att=0.004, rel=0.12):
    import numpy as np
    from scipy.signal import lfilter
    env = np.abs(x)
    a_r = np.exp(-1 / (sr * rel))
    env = lfilter([1 - a_r], [1, -a_r], env)                    # сглаженная огибающая
    th = 10 ** (thresh_db / 20)
    gain = np.ones_like(env)
    over = env > th
    gain[over] = (th / env[over]) ** (1 - 1 / ratio)
    return x * gain


def apply_fx(x, sr, preset):
    """x — float32/64 в диапазоне [-1, 1]. Возвращает обработанный сигнал той же частоты."""
    import numpy as np
    from scipy.signal import butter, lfilter, sosfilt
    x = x.astype(np.float64)
    pad = np.zeros(int(sr * 0.06))
    tail = np.zeros(int(sr * 0.35))
    x = np.concatenate([pad, x, tail])
    if preset == "clean":
        y = sosfilt(butter(2, 60, "high", fs=sr, output="sos"), x)
    elif preset == "helmet":
        y = sosfilt(butter(4, [260, 4600], "band", fs=sr, output="sos"), x)
        y = np.tanh(y * 2.4) / np.tanh(2.4)
        y = y + 0.32 * _comb(y, int(sr * 0.0035), 0.35)
        y = y * 0.85 + _reverb(y, sr, size=0.45, fb=0.6) * 0.18
        y = _compress(y, sr, -22, 4)
    elif preset == "hologram":
        y = sosfilt(butter(2, 90, "high", fs=sr, output="sos"), x)
        c1 = _chorus(y, sr, 11, 0.6, 0.42)
        c2 = _chorus(y, sr, 17, 0.8, 0.31, 1.7)
        n = np.arange(len(y))
        ring = y * np.sin(2 * np.pi * 95 * n / sr)
        y = y * 0.72 + c1 * 0.26 + c2 * 0.2 + ring * 0.06
        y = y + _reverb(y, sr, size=1.25, fb=0.79) * 0.3
        y = _compress(y, sr, -20, 3)
    elif preset == "legacy":
        n = np.arange(len(x))
        y = x * 0.75 + x * np.sin(2 * np.pi * 30 * n / sr) * 0.25
        y = y * 0.65 + 0.35 * _comb(y, max(1, int(sr * 0.004)), 0.45)
        out = y.copy()
        for ms, g in ((23, 0.22), (41, 0.14), (67, 0.08)):
            dd = int(sr * ms / 1000)
            out[dd:] += y[:-dd] * g
        y = out
    elif preset == "mark":                                      # голос Марка-0: маленький робот
        y = sosfilt(butter(2, [220, 7000], "band", fs=sr, output="sos"), x)
        n = np.arange(len(y))
        y = y * 0.86 + y * np.sin(2 * np.pi * 62 * n / sr) * 0.14
        y = y + 0.22 * _comb(y, int(sr * 0.0021), 0.3)
        y = y + _reverb(y, sr, size=0.35, fb=0.55) * 0.1
        y = _compress(y, sr, -20, 3)
    else:                                                       # «jarvis»: студийный ИИ
        y = sosfilt(butter(2, 75, "high", fs=sr, output="sos"), x)
        b, a = _shelf_low(170, 2.2, sr); y = lfilter(b, a, y)          # тепло
        b, a = _peaking(3300, 3.0, 0.9, sr); y = lfilter(b, a, y)      # разборчивость
        b, a = _peaking(9000, 1.5, 0.7, sr); y = lfilter(b, a, y)      # воздух
        y = _compress(y, sr, -21, 2.6)
        ch = _chorus(y, sr, 8.5, 0.22, 0.33)
        y = y * 0.88 + ch * 0.16
        y = y + _reverb(y, sr, size=0.8, fb=0.72, damp=6200) * 0.11
    peak = float(np.max(np.abs(y))) or 1.0
    y = y / peak * 0.9
    f = int(sr * 0.006)
    y[:f] *= np.linspace(0, 1, f)
    y[-f:] *= np.linspace(1, 0, f)
    return y


# ───────────────────────── движки ─────────────────────────

_silero = {"model": None, "error": None, "lock": threading.Lock()}
_synth_lock = threading.Lock()
LAST_ERROR = [None]


def silero_model():
    with _silero["lock"]:
        if _silero["model"] is not None:
            return _silero["model"]
        path = os.path.join(VOICE_DIR, SILERO_FILE)
        if not os.path.exists(path):
            _silero["error"] = "нет модели голоса Silero (%s)" % SILERO_FILE
            return None
        try:
            import torch
            torch.set_num_threads(4)
            imp = torch.package.PackageImporter(path)
            _silero["model"] = imp.load_pickle("tts_models", "model")
            _silero["error"] = None
            print("голос Silero загружен", flush=True)
        except Exception as e:
            _silero["error"] = "Silero не загрузился: %s" % e
            print(_silero["error"], file=sys.stderr, flush=True)
        return _silero["model"]


def silero_available():
    try:
        import torch  # noqa
        import numpy  # noqa
        import scipy  # noqa
    except Exception:
        return False
    return os.path.exists(os.path.join(VOICE_DIR, SILERO_FILE))


def _ssml_escape(s):
    return s.replace("&", " и ").replace("<", " ").replace(">", " ")


def synth_silero(text, prof, emotion):
    import numpy as np
    model = silero_model()
    if model is None:
        return None
    dp, dr = EMOTIONS.get(emotion or "calm", (0, 0))
    pitch = clamp(int(prof.get("pitch", 0)) + dp, -40, 40)
    rate = clamp(int(prof.get("rate", 100)) + dr, 60, 150)
    spk = prof.get("speaker") or "aidar"
    chunks = []
    for part in split_sentences(text):
        ssml = '<speak><prosody rate="%d%%" pitch="%+d%%">%s</prosody></speak>' % (rate, pitch, _ssml_escape(part))
        try:
            a = model.apply_tts(ssml_text=ssml, speaker=spk, sample_rate=SR).numpy()
        except Exception as e:                                      # SSML не прошёл — пробуем просто текст
            try:
                a = model.apply_tts(text=part, speaker=spk, sample_rate=SR).numpy()
            except Exception as e2:
                LAST_ERROR[0] = "silero: %s / %s" % (e, e2)
                continue
        chunks.append(a)
        chunks.append(np.zeros(int(SR * 0.09), dtype=a.dtype))
    if not chunks:
        return None
    return np.concatenate(chunks)


_say_voices = {"list": None}


def macos_voices():
    if _say_voices["list"] is None:
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
            _say_voices["list"] = [l.split()[0] for l in out.splitlines() if re.search(r"\bru[_-]RU\b", l)]
        except Exception:
            _say_voices["list"] = []
    return _say_voices["list"]


def synth_macos(text, prof, emotion):
    import numpy as np
    voices = macos_voices()
    if not voices:
        return None
    v = prof.get("speaker") if prof.get("speaker") in voices else voices[0]
    rate = int(175 * clamp(int(prof.get("rate", 100)) + EMOTIONS.get(emotion or "calm", (0, 0))[1], 60, 150) / 100)
    tmp = os.path.join(TTS_CACHE, "say-%d.wav" % threading.get_ident())
    try:
        subprocess.run(["say", "-v", v, "-r", str(rate), "-o", tmp, "--data-format=LEI16@%d" % SR, text],
                       timeout=60, capture_output=True)
        with wave.open(tmp, "rb") as wf:
            data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        os.remove(tmp)
        return data
    except Exception as e:
        LAST_ERROR[0] = "say: %s" % e
        return None


# ───────────────────────── главный вход ─────────────────────────

def _trim_cache():
    try:
        files = [os.path.join(TTS_CACHE, f) for f in os.listdir(TTS_CACHE) if f.endswith(".wav")]
        sizes = [(os.path.getatime(p), os.path.getsize(p), p) for p in files]
        total = sum(s for _, s, _ in sizes)
        if total <= CACHE_LIMIT:
            return
        for _, s, p in sorted(sizes):
            os.remove(p)
            total -= s
            if total < CACHE_LIMIT * 0.8:
                break
    except Exception:
        pass


_trim_at = [0]


def synthesize(text, prof=None, emotion="calm"):
    """Возвращает путь к WAV (кэшируется) или None."""
    prof = dict(DEFAULT_VOICE, **(prof or {}))
    clean = normalize(text)
    if not clean:
        return None
    os.makedirs(TTS_CACHE, exist_ok=True)
    key = hashlib.md5(("v8|%s|%s|%s|%s|%s|%s|%s" % (prof["engine"], prof["speaker"], prof["fx"], prof["pitch"],
                                                     prof["rate"], emotion, clean)).encode("utf-8")).hexdigest()
    path = os.path.join(TTS_CACHE, key + ".wav")
    if os.path.exists(path):
        try:
            os.utime(path)
        except Exception:
            pass
        return path
    with _synth_lock:
        if os.path.exists(path):
            return path
        audio = None
        try:
            if prof["engine"] == "silero":
                audio = synth_silero(clean, prof, emotion)
            if audio is None:
                audio = synth_macos(clean, prof, emotion)
        except Exception as e:
            LAST_ERROR[0] = "синтез: %s" % e
            print(LAST_ERROR[0], file=sys.stderr, flush=True)
        if audio is None:
            return None
        try:
            import numpy as np
            y = apply_fx(audio, SR, prof.get("fx", "jarvis"))
            pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16)
        except Exception as e:
            LAST_ERROR[0] = "эффект: %s" % e
            import numpy as np
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        tmp = path + ".tmp"
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm.tobytes())
        os.replace(tmp, path)
    if time.time() - _trim_at[0] > 600:
        _trim_at[0] = time.time()
        threading.Thread(target=_trim_cache, daemon=True).start()
    return path


def wav_duration(path):
    try:
        with wave.open(path, "rb") as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


def last_error():
    return _silero["error"] or LAST_ERROR[0]


def download_model(log=print):
    """Скачивает модель Silero (≈150 МБ), если её нет."""
    path = os.path.join(VOICE_DIR, SILERO_FILE)
    if os.path.exists(path) and os.path.getsize(path) > 50 * 1024 * 1024:
        return True
    os.makedirs(VOICE_DIR, exist_ok=True)
    log("Скачиваю нейросетевой голос Silero (около 150 МБ)…")
    r = subprocess.run(["curl", "-L", "-f", "-s", "-o", path + ".part", SILERO_URL], timeout=1800)
    if r.returncode == 0 and os.path.getsize(path + ".part") > 50 * 1024 * 1024:
        os.replace(path + ".part", path)
        return True
    log("Голос скачать не удалось — JARVIS будет говорить запасным голосом macOS.")
    return False


if __name__ == "__main__":                                   # проверка: python3 jarvis_voice.py "текст"
    txt = " ".join(sys.argv[1:]) or "Добрый вечер. Индекс здоровья 87%, свободно 51,6 ГБ. Сейчас 15:05."
    print(normalize(txt))
    t0 = time.time()
    print(synthesize(txt), round(time.time() - t0, 2), last_error())
