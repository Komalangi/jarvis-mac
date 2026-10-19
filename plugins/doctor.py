# -*- coding: utf-8 -*-
"""Самодиагностика: «почему ты молчишь», «как ты себя чувствуешь», «что сломалось»."""
import time


def register(api):
    core = api.core

    def why_quiet(raw="", t="", m=None):
        st = core.settings()
        now = time.time()
        reasons = []
        left = st.get("quiet_until", 0) - now
        if left > 0:
            reasons.append("включён режим тишины ещё на %s" % core.fmt_minutes(left))
        if st.get("talk") == "quiet":
            reasons.append("выбран режим «молчун»")
        h = time.localtime().tm_hour
        if h < 7 or h >= 23:
            reasons.append("сейчас ночь, в такое время я говорю только о важном")
        try:
            if core.JV.last_error():
                reasons.append("голос сообщал об ошибке: %s" % str(core.JV.last_error())[:80])
        except Exception:
            pass
        if not reasons:
            return "Я не молчу: режим «%s», %s." % (
                core.talk_mode(), ("последний раз говорил %s назад" % core.fmt_minutes(max(60, now - core._brain["last_talk"])))
                if core._brain["last_talk"] else "с запуска ещё ничего не говорил")
        return "Причина: " + "; ".join(reasons) + "."

    def health(raw="", t="", m=None):
        import jarvis_brain as B
        import jarvis_plugins as P
        parts = ["Плагинов загружено %d" % len(P.LOADED)]
        if P.ERRORS:
            parts.append("с ошибками: %s" % ", ".join(list(P.ERRORS)[:3]))
        parts.append("мозг %s" % ("подключён" if B.configured() else "не подключён, нужен ключ"))
        if B.last_error[0]:
            parts.append("последняя ошибка мозга: %s" % B.last_error[0][:80])
        parts.append("работаю уже %s" % core.fmt_minutes(max(60, time.time() - core._brain["started"])))
        text = ". ".join(parts)
        return text[0].upper() + text[1:] + "."

    api.tool("jarvis_health", "Самодиагностика JARVIS: плагины, мозг, ошибки, аптайм.", None, health)
    api.tool("why_quiet", "Почему JARVIS сейчас молчит или не озвучивает реплики.", None, why_quiet)
    api.command(r"почему (ты )?(молчишь|не говоришь|не отвечаешь)", why_quiet)
    api.command(r"самодиагностика|как ты себя чувствуешь|что (у тебя )?сломалось|твои ошибки", health)
