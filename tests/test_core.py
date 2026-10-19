# -*- coding: utf-8 -*-
"""Запуск: python3 -m unittest discover -s tests   (из папки JARVIS)"""
import os
import sys
import tempfile
import unittest

os.environ["JARVIS_DEMO"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jarvis as J  # noqa: E402


class Parsers(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(J.norm("  Ёжик,  ПРИВЕТ!! "), "ежик привет")

    def test_duration(self):
        self.assertEqual(J.parse_duration("полчаса")[0], 1800)
        self.assertEqual(J.parse_duration("час")[0], 3600)
        self.assertEqual(J.parse_duration("20 минут")[0], 1200)
        self.assertIsNone(J.parse_duration("тридцать яблок")[0])

    def test_minutes(self):
        self.assertEqual(J.fmt_minutes(90 * 60), "1 час 30 минут")
        self.assertEqual(J.fmt_minutes(5 * 60), "5 минут")

    def test_plural(self):
        self.assertEqual(J.plural(2, ("файл", "файла", "файлов")), "файла")
        self.assertEqual(J.plural(11, ("файл", "файла", "файлов")), "файлов")


class Brain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        J.SUPPORT = self.tmp
        J.BRAIN.init(J)
        J.BRAIN.register_builtin_tools()

    def test_memory_roundtrip(self):
        J.BRAIN.remember("люблю чай")
        self.assertIn("люблю чай", J.BRAIN.recall("чай"))
        self.assertEqual(J.BRAIN.remember("Люблю чай"), "Это я уже знаю.")
        J.BRAIN.forget("чай")
        self.assertNotIn("люблю чай", J.BRAIN.recall())

    def test_emotion_tag(self):
        emo, text = J.BRAIN._split_emotion("[witty] Готово.")
        self.assertEqual((emo, text), ("witty", "Готово."))
        self.assertEqual(J.BRAIN._split_emotion("[nonsense] Эй")[1], "Эй")

    def test_speech_cleanup(self):
        self.assertEqual(J.BRAIN._clean_for_speech("**Да** https://x.io `ок`"), "Да ок")

    def test_dangerous_blocked(self):
        out = J.PLUG.TOOLS["jarvis_command"]["fn"]("перезагрузи мак")
        self.assertIn("нельзя", out)

    def test_no_key_means_no_llm(self):
        J.BRAIN._key.update(v="", t=9e12)
        self.assertIsNone(J.BRAIN.ask("привет"))


class Plugins(unittest.TestCase):
    def test_all_load(self):
        J.PLUG.load_all(J)
        self.assertEqual(J.PLUG.ERRORS, {})
        self.assertGreaterEqual(len(J.PLUG.LOADED), 8)


if __name__ == "__main__":
    unittest.main()


class PluginCommands(unittest.TestCase):
    def setUp(self):
        J.SUPPORT = tempfile.mkdtemp()
        J.BRAIN.init(J)
        J.PLUG.load_all(J)

    def test_water_count_not_marked_by_question(self):
        J.text_command("выпил воды")
        self.assertIn("1", J.text_command("сколько я выпил воды"))
        self.assertIn("1", J.text_command("сколько я выпил воды"))

    def test_theme(self):
        J.text_command("тема золотая")
        self.assertEqual(J.settings()["theme"], "gold")

    def test_theme_not_article_topic(self):
        r = J.text_command("тема статьи про космос")
        self.assertNotEqual(J.settings()["theme"], "red")
