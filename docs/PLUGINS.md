# Плагины

Плагин — это один файл `*.py` в папке `plugins/` рядом с `jarvis.py`. Свои плагины можно класть и
в `~/Library/Application Support/JARVIS/plugins`: так они переживут обновление репозитория. Файлы, имя которых
начинается с `_`, не загружаются.

Каждый плагин экспортирует функцию `register(api)`. Если в плагине ошибка, JARVIS продолжает работать,
а ошибка видна по команде «самодиагностика» и в `GET /api/ext/plugins`.

## API

| Вызов | Что делает |
| --- | --- |
| `api.command(regex, fn)` | Голосовая или текстовая команда. `fn(raw, t, match)` получает исходную фразу, нормализованную (нижний регистр, ё → е, без пунктуации) и совпадение. Возвращает строку, словарь `{"say": …, "emotion": …}` или `None`, чтобы пропустить ход дальше |
| `api.tool(name, description, schema, fn)` | Инструмент для мозга. `schema` — JSON Schema аргументов или `None`. `fn(**args)` возвращает строку |
| `api.loop(fn, interval, first_delay=30)` | Фоновая задача раз в `interval` секунд |
| `api.route(method, path, fn)` | HTTP-маршрут `/api/ext/...`. `fn(body)` для POST или `fn(query)` для GET возвращает словарь, который уйдёт в JSON |
| `api.say(text, prio=2, trick=None, voice=True)` | Сказать фразу голосом, если сейчас уместно, иначе только показать текстом |
| `api.data(name, default)`, `api.save(name, value)` | Хранить JSON в `~/Library/Application Support/JARVIS/<name>` |
| `api.core` | Модуль ядра: `settings()`, `save_settings()`, `sh()`, `notify()`, `frontmost_app()`, `idle_seconds()`, `talk_mode()`, `day_start()`, `fmt_minutes()`, `plural()` и другие |

Приоритеты `prio`: 0 — болтовня, 1 — обычное событие, 2 — важное, 3 — тревога (звучит даже в режиме тишины).

Команды плагинов проверяются раньше встроенных, сразу после твоих команд из `Мои команды.json`. Поэтому
регулярное выражение должно быть узким и не перехватывать чужие фразы.

## Пример

```python
# plugins/coffee.py
import time


def register(api):
    def log_cup(raw, t, m):
        d = api.data("coffee.json", {"cups": []})
        d["cups"].append(int(time.time()))
        api.save("coffee.json", d)
        today = [c for c in d["cups"] if c >= api.core.day_start(0)]
        return "Чашка кофе номер %d за сегодня." % len(today)

    def remind():
        if time.localtime().tm_hour == 15 and api.core.idle_seconds() < 60:
            api.say("Три часа дня. Самое время для кофе.", prio=1)

    api.command(r"выпил кофе|налил кофе", log_cup)
    api.tool("coffee_today", "Сколько чашек кофе выпито сегодня.", None,
             lambda: str(len([c for c in api.data("coffee.json", {"cups": []})["cups"]
                              if c >= api.core.day_start(0)])))
    api.loop(remind, 3600, first_delay=60)
```

После добавления файла перезапусти JARVIS (в собранное приложение папка `plugins/` попадает при `--rebuild`).

## Отключение

«Отключи плагин <имя>» или вручную: список имён в `~/Library/Application Support/JARVIS/plugins_off.json`,
например `["music", "habits"]`. Вступает в силу после перезапуска.

## Тесты

```bash
python3 -m unittest discover -s tests
```

Тесты запускают ядро в демо-режиме (`JARVIS_DEMO=1`) и ничего не меняют в системе.
