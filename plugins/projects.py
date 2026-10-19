# -*- coding: utf-8 -*-
"""Статус git-проектов: ветка, незакоммиченное, не отправленное."""
import os
import subprocess

DEFAULT_ROOTS = ["~/Desktop", "~/Developer", "~/Projects", "~/Code", "~/Documents/GitHub", "~/Documents/Projects"]


def register(api):
    core = api.core

    def roots():
        cfg = api.data("projects.json", {}) or {}
        return [os.path.expanduser(r) for r in cfg.get("roots", DEFAULT_ROOTS)]

    def find_repos():
        out = []
        for root in roots():
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name)
                if name.startswith(".") or not os.path.isdir(p):
                    continue
                if os.path.isdir(os.path.join(p, ".git")):
                    out.append(p)
                else:
                    try:
                        for sub in sorted(os.listdir(p))[:60]:
                            q = os.path.join(p, sub)
                            if os.path.isdir(os.path.join(q, ".git")):
                                out.append(q)
                    except OSError:
                        pass
        return out[:40]

    def git(p, *args):
        try:
            return subprocess.run(["git", "-C", p] + list(args), capture_output=True, text=True, timeout=8).stdout
        except Exception:
            return ""

    def info(p):
        st = git(p, "status", "--porcelain", "-b").splitlines()
        head = st[0][3:] if st else ""
        branch = head.split("...")[0]
        dirty = len([l for l in st[1:] if l.strip()])
        ahead = 0
        if "[ahead " in head:
            try:
                ahead = int(head.split("[ahead ")[1].split("]")[0].split(",")[0])
            except ValueError:
                pass
        return {"name": os.path.basename(p), "path": p, "branch": branch, "dirty": dirty, "ahead": ahead,
                "last": git(p, "log", "-1", "--format=%cr").strip()}

    def report():
        rows = [info(p) for p in find_repos()]
        if not rows:
            return "Git-проектов не нашёл. Укажи папки в файле projects.json."
        return "\n".join("%s: ветка %s, изменений %d, не отправлено %d, последний коммит %s" % (
            r["name"], r["branch"] or "?", r["dirty"], r["ahead"], r["last"] or "—") for r in rows)

    def voice(raw, t, m):
        rows = [info(p) for p in find_repos()]
        if not rows:
            return "Git-проектов не нашёл."
        dirty = [r for r in rows if r["dirty"] or r["ahead"]]
        if not dirty:
            return "Проектов под git: %d. Всё закоммичено и отправлено." % len(rows)
        parts = ["%s — %d изменений%s" % (r["name"], r["dirty"], (", не отправлено %d" % r["ahead"]) if r["ahead"] else "")
                 for r in dirty[:4]]
        return "Проектов %d, с хвостами %d. %s." % (len(rows), len(dirty), "; ".join(parts))

    def open_project(name):
        import difflib
        repos = find_repos()
        by = {os.path.basename(p).lower(): p for p in repos}
        n = name.strip().lower()
        hit = by.get(n) or next((p for k, p in by.items() if n in k), None)
        if not hit:
            c = difflib.get_close_matches(n, list(by), n=1, cutoff=0.5)
            hit = by[c[0]] if c else None
        if not hit:
            return "Не нашёл проект «%s»." % name
        cfg = api.data("projects.json", {}) or {}
        editors = [cfg["editor"]] if cfg.get("editor") else ["Cursor", "Visual Studio Code", "Zed", "Sublime Text"]
        if any(f.endswith((".xcodeproj", ".xcworkspace")) for f in os.listdir(hit)) and not cfg.get("editor"):
            editors.insert(0, "Xcode")
        for ed in editors:
            if subprocess.run(["open", "-a", ed, hit], capture_output=True).returncode == 0:
                return "Открываю %s в %s." % (os.path.basename(hit), ed)
        subprocess.run(["open", hit])
        return "Открыл папку проекта %s." % os.path.basename(hit)

    api.command(r"открой проект (.+)", lambda raw, t, m: open_project(m.group(1)))
    api.tool("open_project", "Открыть git-проект пользователя в редакторе по названию.",
             {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, open_project)
    api.tool("project_status", "Статус git-репозиториев пользователя: ветки, незакоммиченные изменения, неотправленные коммиты.",
             None, report)
    api.command(r"статус проектов|что с проектами|незакоммич|git статус", voice)
    api.route("GET", "/api/ext/projects", lambda q: {"projects": [info(p) for p in find_repos()]})
