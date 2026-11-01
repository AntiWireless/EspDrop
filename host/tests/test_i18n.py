# EspDrop tests - the app in German and English (i18n.py): every text the app
# shows goes through _() and has an English version; German and English both
# render the main window.
#
#   ~/opendrop-venv/bin/python -m unittest discover -s host/tests
#
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026  EspDrop contributors

import ast
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from espdrop import i18n  # noqa: E402

PKG = os.path.join(HERE, "..", "espdrop")
# The modules whose texts reach the user (the command line is English).
MODULES = ["gui.py", "ui.py", "flash.py", "integration.py"]
GERMAN = re.compile(r"[äöüÄÖÜß„“]|\b(der|die|das|und|nicht|ein|eine|mit|für|ist|wird|"
                    r"Datei|Dateien|Gerät|Geräte|Senden|Empfangen|Suchen|Abbrechen|Fertig|"
                    r"Kein|Keine|Beenden|Allgemein|Installieren|Aktualisieren|Erneut|Warten|"
                    r"Gesendet|Abgelehnt|Funk|Hell|Dunkel|Automatisch|Mehr|Protokoll|Aus|"
                    r"Minuten|Anschluss|Sprache)\b")
# German-looking strings that are not shown (file names, test data, keys).
NOT_SHOWN = {"prüfung-ä.txt"}
# Words that are the same in both languages (keys of EN for completeness).
SAME_IN_BOTH = {"AirDrop", "Name", "ESP {mac}"}


def strings(path):
    """(text, line, how) for every string constant: how is "_", "N_" or None."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    marked = {}
    docs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_", "N_") and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Constant):      # _(TABLE[key]): the key is not a text
                marked[id(arg)] = node.func.id
        if (isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)) and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            docs.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.value, node.lineno, marked.get(id(node))


class TranslationTest(unittest.TestCase):
    def test_every_shown_text_is_translated(self):
        missing, untranslated = [], []
        for name in MODULES:
            for text, line, how in strings(os.path.join(PKG, name)):
                if how:
                    if text not in i18n.EN:
                        missing.append(f"{name}:{line}: {text!r}")
                elif (GERMAN.search(text) or text in i18n.EN) and text not in NOT_SHOWN \
                        and text not in SAME_IN_BOTH:
                    untranslated.append(f"{name}:{line}: {text!r}")
        self.assertEqual(untranslated, [], "German text shown without _():\n" + "\n".join(untranslated))
        self.assertEqual(missing, [], "_() text without an English version")

    def test_placeholders_match(self):
        for de, en in i18n.EN.items():
            self.assertEqual(sorted(re.findall(r"\{(\w+)\}", de)),
                             sorted(re.findall(r"\{(\w+)\}", en)), de)

    def test_language_choice(self):
        old = i18n.LANG
        try:
            self.assertEqual(i18n.set_language("en"), "en")
            self.assertEqual(i18n._("Abbrechen"), "Cancel")
            self.assertEqual(i18n._("{n} Fotos").format(n=3), "3 photos")
            self.assertEqual(i18n.set_language("de"), "de")
            self.assertEqual(i18n._("Abbrechen"), "Abbrechen")
            from unittest import mock
            with mock.patch.dict(os.environ, {"ESPDROP_LANG": "en"}):
                self.assertEqual(i18n.set_language("auto"), "en")
            with mock.patch.dict(os.environ, {"ESPDROP_LANG": "de"}):
                self.assertEqual(i18n.set_language("auto"), "de")
        finally:
            i18n.LANG = old


if __name__ == "__main__":
    unittest.main()
