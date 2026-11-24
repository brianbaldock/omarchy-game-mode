"""How Omarchy's bar counts the hours of its clock: the Qt format, the shell.json that holds it and which one wins.

No display and no Omarchy: the files are scratch ones laid out like the packaged default, and the environment is
a dictionary. Nothing here reads the user's own configuration.
"""

import ast
import json
import os
import shutil
import threading
import unittest
from pathlib import Path
from unittest import mock

from fakes.omarchy import shell
from gaminghome import barclock
from gaminghome.barclock import MAX_BYTES, bar_twelve_hour, clock_format, config_paths, twelve_hour_format

SCRATCH = Path(__file__).resolve().parent / "scratch" / "barclock"


class QtFormatTest(unittest.TestCase):
    def test_the_default_of_the_bar_counts_to_24(self):
        self.assertIs(twelve_hour_format("dddd HH:mm"), False)

    def test_an_am_pm_designator_counts_to_12(self):
        for text in ("h:mm AP", "hh:mm ap", "h:mm a", "h:mm A", "hh:mm:ss AP", "AP h:mm", "dddd h:mm AP", "H:mm AP"):
            self.assertIs(twelve_hour_format(text), True, text)

    def test_without_a_designator_every_hour_letter_counts_to_24(self):
        for text in ("H:mm", "HH:mm", "h:mm", "hh:mm", "HH:mm:ss", "dddd HH:mm", "HH'h'mm", "h", "d MMMM HH"):
            self.assertIs(twelve_hour_format(text), False, text)

    def test_text_in_single_quotes_is_literal(self):
        for text in ("HH:mm 'AP'", "HH:mm 'a'", "dddd 'at' HH:mm", "HH:mm 'o''clock'", "HH:mm 'AP", "HH:mm ''"):
            self.assertIs(twelve_hour_format(text), False, text)
        self.assertIs(twelve_hour_format("h:mm 'at' AP"), True)         # the quoted word is text, the AP is not
        for text in ("'HH':mm", "'h' AP", "'HH:mm", "dddd 'H'"):
            self.assertIsNone(twelve_hour_format(text), text)            # a quoted hour is no hour

    def test_a_format_with_no_hour_says_nothing(self):
        for text in ("", " ", "dddd", "d MMMM yyyy", "mm:ss", "AP", "a", "yyyy-MM-dd"):
            self.assertIsNone(twelve_hour_format(text), repr(text))

    def test_anything_that_is_not_text_says_nothing(self):
        for junk in (None, 5, 5.5, True, [], {}, b"HH:mm", ("HH",)):
            self.assertIsNone(twelve_hour_format(junk), repr(junk))


class FindTest(unittest.TestCase):
    def test_the_clock_is_found_where_the_packaged_default_has_it(self):
        self.assertEqual(clock_format(shell()), "dddd HH:mm")
        self.assertEqual(clock_format(shell("h:mm AP")), "h:mm AP")

    def test_it_is_found_wherever_it_sits(self):
        entry = {"id": "omarchy.clock", "format": "HH:mm"}
        for document in (entry, [entry], {"plugins": [entry]}, {"a": {"b": [[{"c": [entry]}]]}},
                         {"bar": {"plugins": [{"id": "omarchy.tray"}, 5, None, entry]}}):
            self.assertEqual(clock_format(document), "HH:mm", document)

    def test_a_string_that_merely_names_the_clock_is_not_an_entry(self):
        # The packaged default has "centerAnchor": "omarchy.clock" next to its layout.
        self.assertIsNone(clock_format({"bar": {"centerAnchor": "omarchy.clock", "layout": {"center": ["omarchy.clock"]}}}))
        self.assertIsNone(clock_format({"name": "omarchy.clock", "format": "HH:mm"}))      # not under "id"
        self.assertIsNone(clock_format({"id": "omarchy.clock.extra", "format": "HH:mm"}))

    def test_other_entries_with_a_format_are_not_the_clock(self):
        self.assertIsNone(clock_format({"layout": [{"id": "omarchy.weather", "format": "h:mm AP"}]}))

    def test_the_first_clock_of_the_file_is_the_one(self):
        document = {"left": [{"id": "omarchy.clock", "format": "h:mm AP"}],
                    "right": [{"id": "omarchy.clock", "format": "HH:mm"}]}
        self.assertEqual(clock_format(document), "h:mm AP")
        nested = [{"id": "x", "inner": [{"id": "omarchy.clock", "format": "h:mm AP"}]},
                  {"id": "omarchy.clock", "format": "HH:mm"}]
        self.assertEqual(clock_format(nested), "h:mm AP")

    def test_an_entry_without_a_text_format_is_passed_over(self):
        for bad in (None, 24, True, ["HH:mm"], {"format": "HH:mm"}):
            self.assertIsNone(clock_format(shell(format=bad)), repr(bad))
        self.assertIsNone(clock_format({"id": "omarchy.clock"}))
        later = {"left": [{"id": "omarchy.clock", "format": 24}], "right": [{"id": "omarchy.clock", "format": "HH:mm"}]}
        self.assertEqual(clock_format(later), "HH:mm")

    def test_the_other_formats_of_the_entry_are_never_the_answer(self):
        document = {"id": "omarchy.clock", "formatAlt": "h:mm AP", "verticalFormat": "h AP"}
        self.assertIsNone(clock_format(document))
        self.assertEqual(clock_format(shell("HH:mm", formatAlt="h:mm AP", verticalFormat="h AP")), "HH:mm")

    def test_what_is_not_a_document_has_no_clock(self):
        for junk in (None, 3, "omarchy.clock", [], {}, [[]], {"id": None}, [None, 1, "x"], {"id": ["omarchy.clock"]}):
            self.assertIsNone(clock_format(junk), repr(junk))

    def test_a_very_deep_document_cannot_overflow_the_stack(self):
        document = [{"id": "omarchy.clock", "format": "HH:mm"}]
        for _ in range(20_000):
            document = [document]
        self.assertEqual(clock_format(document), "HH:mm")


class PathsTest(unittest.TestCase):
    def test_the_users_file_comes_first_and_the_packaged_default_behind_it(self):
        self.assertEqual(config_paths({"HOME": "/home/user"}),
                         [Path("/home/user/.config/omarchy/shell.json"),
                          Path("/usr/share/omarchy/config/omarchy/shell.json")])

    def test_the_environment_moves_either_of_them(self):
        env = {"HOME": "/home/user", "XDG_CONFIG_HOME": "/xdg", "OMARCHY_PATH": "/opt/omarchy"}
        self.assertEqual(config_paths(env), [Path("/xdg/omarchy/shell.json"),
                                             Path("/opt/omarchy/config/omarchy/shell.json")])
        self.assertEqual(config_paths({"HOME": "/home/user", "OMARCHY_PATH": ""})[1],
                         Path("/usr/share/omarchy/config/omarchy/shell.json"))

    def test_the_environment_is_read_when_asked_and_not_before(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/first", "OMARCHY_PATH": "/one"}):
            self.assertEqual(config_paths(), [Path("/first/omarchy/shell.json"),
                                              Path("/one/config/omarchy/shell.json")])
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/second", "OMARCHY_PATH": "/two"}):
            self.assertEqual(config_paths(), [Path("/second/omarchy/shell.json"),
                                              Path("/two/config/omarchy/shell.json")])


class ReadTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        SCRATCH.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, SCRATCH, True)
        self.user, self.default = SCRATCH / "user.json", SCRATCH / "default.json"

    def write(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def counted(self):
        return bar_twelve_hour([self.user, self.default])

    def test_the_packaged_default_alone_counts_to_24(self):
        self.write(self.default, shell())
        self.assertIs(self.counted(), False)

    def test_the_users_format_wins_over_the_default_either_way(self):
        self.write(self.default, shell("HH:mm"))
        self.write(self.user, shell("h:mm AP"))
        self.assertIs(self.counted(), True)
        self.write(self.default, shell("h:mm AP"))
        self.write(self.user, shell("HH:mm"))
        self.assertIs(self.counted(), False)

    def test_the_users_file_is_enough_without_a_default(self):
        self.write(self.user, shell("h:mm AP"))
        self.assertIs(self.counted(), True)

    def test_nothing_to_read_says_nothing(self):
        self.assertIsNone(self.counted())
        self.assertIsNone(bar_twelve_hour([]))

    def test_only_the_format_is_read_never_the_alternative_one(self):
        self.write(self.user, shell("HH:mm", formatAlt="h:mm AP", verticalFormat="h:mm AP"))
        self.assertIs(self.counted(), False)
        self.write(self.user, shell("dddd", formatAlt="h:mm AP", verticalFormat="h:mm AP"))   # no hour in "format"
        self.assertIsNone(self.counted())

    def test_a_users_file_that_does_not_say_leaves_it_to_the_default(self):
        self.write(self.default, shell("h:mm AP"))
        center_without_a_clock = {"bar": {"layout": {"center": [{"id": "omarchy.indicators"}]}}}
        for name, content in (("empty", ""), ("blank", "  \n"), ("not json", '{"bar": '), ("null", "null"),
                              ("a number", "5"), ("a string", '"HH:mm"'), ("a list", "[]"), ("an object", "{}"),
                              ("no clock", json.dumps(center_without_a_clock)),
                              ("a clock without a format", json.dumps({"id": "omarchy.clock"})),
                              ("a number as format", json.dumps(shell(24))),
                              ("null as format", json.dumps(shell(None))),
                              ("a list as format", json.dumps(shell(["HH:mm"]))),
                              ("no hour in the format", json.dumps(shell("dddd"))),
                              ("only a quoted hour", json.dumps(shell("'HH' dddd")))):
            with self.subTest(name):
                self.user.write_text(content, encoding="utf-8")
                self.assertIs(self.counted(), True)

    def test_a_file_that_is_not_text_or_not_a_file_says_nothing(self):
        self.write(self.default, shell("h:mm AP"))
        for name, content in (("invalid utf-8", b"\xc3\x28 {}"), ("utf-16 noise", b"\xff\xfe\x00\x01garbage"),
                              ("binary", bytes(range(256))), ("a nul byte", b'{"bar": 1}\x00')):
            with self.subTest(name):
                self.user.write_bytes(content)
                self.assertIs(self.counted(), True)
        self.user.unlink()
        self.user.mkdir()                                              # a directory where the file should be
        self.assertIs(self.counted(), True)

    def test_a_missing_file_is_not_an_error(self):
        self.write(self.default, shell("h:mm AP"))
        self.assertIs(self.counted(), True)
        self.assertIs(bar_twelve_hour([SCRATCH / "nowhere" / "shell.json", self.default]), True)

    def test_a_file_that_cannot_be_opened_says_nothing(self):
        self.write(self.user, shell("h:mm AP"))
        for error in (PermissionError(13, "denied"), OSError(5, "input/output error"), FileNotFoundError(2, "gone")):
            with mock.patch("builtins.open", side_effect=error):
                self.assertIsNone(self.counted(), repr(error))

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_a_file_that_may_not_be_read_says_nothing(self):
        self.write(self.default, shell("HH:mm"))
        self.write(self.user, shell("h:mm AP"))
        self.user.chmod(0)
        self.addCleanup(self.user.chmod, 0o600)
        self.assertIs(self.counted(), False)

    def test_a_symbolic_link_to_a_file_is_followed(self):
        self.write(SCRATCH / "real.json", shell("h:mm AP"))
        self.user.symlink_to(SCRATCH / "real.json")
        self.assertIs(self.counted(), True)

    def test_a_very_large_file_is_not_read(self):
        padding = " " * MAX_BYTES
        self.write(self.default, shell("HH:mm"))
        self.user.write_text(json.dumps(shell("h:mm AP")) + padding, encoding="utf-8")   # valid, but over the limit
        self.assertIs(self.counted(), False)
        document = json.dumps(shell("h:mm AP"))
        self.user.write_text(document + " " * (MAX_BYTES - len(document)), encoding="utf-8")   # exactly at it
        self.assertEqual(self.user.stat().st_size, MAX_BYTES)
        self.assertIs(self.counted(), True)

    def test_a_document_nested_too_deeply_for_json_says_nothing(self):
        self.write(self.default, shell("h:mm AP"))
        self.user.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
        self.assertIs(self.counted(), True)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_pipe_in_place_of_the_file_does_not_make_anyone_wait(self):
        os.mkfifo(self.user)
        self.write(self.default, shell("h:mm AP"))
        answers = []
        worker = threading.Thread(target=lambda: answers.append(self.counted()), daemon=True)
        worker.start()
        worker.join(timeout=3)
        if worker.is_alive():
            os.close(os.open(self.user, os.O_RDWR))                    # let a stuck reader go before the cleanup
            worker.join(timeout=3)
            self.fail("reading a pipe blocked")
        self.assertEqual(answers, [True])

    def test_without_paths_the_files_come_from_the_environment_at_each_call(self):
        config, omarchy = SCRATCH / "config", SCRATCH / "omarchy"
        user, default = config / "omarchy" / "shell.json", omarchy / "config" / "omarchy" / "shell.json"
        user.parent.mkdir(parents=True)
        default.parent.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config), "OMARCHY_PATH": str(omarchy)}):
            self.assertIsNone(bar_twelve_hour())                       # nothing there yet
            self.write(default, shell("HH:mm"))
            self.assertIs(bar_twelve_hour(), False)
            self.write(user, shell("h:mm AP"))
            self.assertIs(bar_twelve_hour(), True)                     # nothing is remembered between calls
            user.unlink()
            self.assertIs(bar_twelve_hour(), False)


class ModuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(Path(barclock.__file__).read_text(encoding="utf-8"))

    def test_the_module_needs_neither_gtk_nor_glib(self):
        modules = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add((node.module or "").split(".")[0])
        self.assertFalse(modules & {"gi", "Gtk", "GLib", "Gio"}, modules)

    def test_importing_the_module_runs_nothing_and_reads_nothing(self):
        for node in self.tree.body:
            self.assertIsInstance(node, (ast.Expr, ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef))
            if isinstance(node, ast.Assign):
                self.assertEqual([call for call in ast.walk(node.value) if isinstance(call, ast.Call)], [])

    def test_nothing_in_it_writes_or_watches(self):
        used = ({node.id for node in ast.walk(self.tree) if isinstance(node, ast.Name)}
                | {node.attr for node in ast.walk(self.tree) if isinstance(node, ast.Attribute)})
        forbidden = {"write", "write_text", "write_bytes", "chmod", "mkdir", "unlink", "rename", "replace", "remove",
                     "O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "timeout_add", "timeout_add_seconds",
                     "idle_add", "FileMonitor", "monitor_file", "inotify_init", "Thread", "Timer"}
        self.assertEqual(used & forbidden, set())
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
                self.assertEqual(node.args[1].value, "rb")              # every file is opened to be read, as bytes


if __name__ == "__main__":
    unittest.main()
