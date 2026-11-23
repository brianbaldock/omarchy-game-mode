import json
import shutil
import time
import unittest
from pathlib import Path

from gaminghome import APP_ID, core, settings
from gaminghome.catalog import CatalogError, load_catalog, parse_catalog, parse_entry
from gaminghome.core import (ACTIVATE, BTN_EAST, BTN_SELECT, BTN_SOUTH, BTN_START, DESKTOP, DOWN, EV_ABS, EV_KEY,
                             EV_SYN, GAME, HOME, MENU, SYN_DROPPED, SYN_REPORT, Window)
from gaminghome.i18n import Translator

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SCRATCH = Path(__file__).resolve().parent / "scratch" / "unit"


def W(address, cls, title="", **kw):
    return Window(address, cls, title, cls, **kw)


STEAM = W("0x10", "steam", "Steam Big Picture Mode")
STEAM_CLIENT = W("0x11", "steam", "Steam")
GREENLIGHT = W("0x20", "Greenlight", "Greenlight - Streaming Xbox")
TERM = W("0x30", "Alacritty", "shell")
HOME_WIN = W("0x40", APP_ID, "Omarchy Game Mode")
HEROIC = W("0x50", "heroic", "Heroic Games Launcher", pid=500)


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(DATA / "catalog.json")
        self.model = core.HomeModel(core.Classifier(self.catalog))
        self.model.ipc_restored()

    def test_classification_priorities(self):
        c = core.Classifier(self.catalog)
        self.assertEqual(c.service(W("0x1", "steam_app_battlenet", "Battle.net")), "battlenet")
        self.assertEqual(c.service(W("0x1", "steam_app_1234", "Some Game")), "steam")
        self.assertIsNone(c.service(W("0x1", "chromium", "Moonlight docs")))
        self.assertEqual(c.kind(W("0x1", "gamescope", "x")), core.KIND_GAME)
        self.assertEqual(c.kind(W("0x1", "foo", "x", content_type="game")), core.KIND_GAME)

    def test_manual_mode_persists_across_layers_and_resets_on_switch(self):
        m = self.model
        m.update_windows([STEAM, TERM], STEAM)
        self.assertEqual(m.desired_mode(), GAME)
        self.assertEqual(m.set_manual_mode(DESKTOP), [("mapper", DESKTOP)])
        self.assertEqual(m.update_windows([STEAM, TERM], None), [])  # menu/layer took focus
        self.assertEqual(m.target_mode_choice(), DESKTOP)
        m.update_windows([STEAM, TERM], STEAM)
        self.assertEqual(m.desired_mode(), DESKTOP)
        m.update_windows([STEAM, TERM], TERM)
        m.update_windows([STEAM, TERM], STEAM)
        self.assertEqual(m.desired_mode(), GAME)

    def test_home_focus_is_game_and_override_targets_underlying_window(self):
        m = self.model
        m.update_windows([TERM], TERM)
        m.update_windows([TERM, HOME_WIN], HOME_WIN)
        m.set_home_state(True, True)
        self.assertEqual(m.desired_mode(), GAME)
        m.set_manual_mode(GAME)
        self.assertEqual(m.override, (TERM.address, GAME))
        m.set_home_state(False, False)
        m.update_windows([TERM], TERM)
        self.assertEqual(m.desired_mode(), GAME)

    def test_launch_waits_for_window_not_wrapper_exit(self):
        m = self.model
        m.update_windows([TERM], TERM)
        m.set_home_state(True, True)
        effects = m.begin_launch("steam")
        self.assertEqual(effects[0][0], "spawn")
        token = effects[0][2]
        self.assertEqual(m.launch_exited(token, True), [])
        self.assertIsNotNone(m.pending)
        self.assertEqual(m.update_windows([TERM, STEAM_CLIENT], TERM), [])  # desktop client is not the handoff
        effects = m.update_windows([TERM, STEAM_CLIENT, STEAM], TERM)
        self.assertIn(("hide",), effects)
        self.assertIn(("focus", STEAM.address), effects)
        self.assertIsNone(m.pending)

    def test_launch_failure_timeout_and_desktop_cancels_handoff(self):
        m = self.model
        token = m.begin_launch("steam")[0][2]
        self.assertIn(("show",), m.launch_exited(token, False, 1))
        token = m.begin_launch("steam")[0][2]
        self.assertEqual(m.message[1], "status.starting")
        self.assertIn(("show",), m.launch_timed_out(token))
        m.begin_launch("steam")
        m.desktop()
        effects = m.update_windows([STEAM], None)
        self.assertNotIn(("hide",), effects)
        self.assertEqual(m.game_address, STEAM.address)

    def test_existing_session_is_focused_not_relaunched(self):
        m = self.model
        m.update_windows([GREENLIGHT], GREENLIGHT)
        effects = m.begin_launch("remote")
        self.assertEqual(effects, [("cancel_launch_timeout",), ("hide",), ("focus", GREENLIGHT.address)])

    def test_session_end_returns_home_only_when_user_was_in_it(self):
        m = self.model
        m.update_windows([STEAM, TERM], STEAM)
        self.assertIn(("show",), m.update_windows([TERM], TERM))
        m.update_windows([STEAM, TERM], STEAM)
        m.update_windows([STEAM, TERM], TERM)  # user went browsing
        self.assertNotIn(("show",), m.update_windows([TERM], TERM))
        self.assertFalse(m.can_resume())

    def test_resume_focuses_exact_window(self):
        m = self.model
        m.update_windows([STEAM, TERM], STEAM)
        m.update_windows([STEAM, TERM], TERM)
        self.assertEqual(m.resume(), [("hide",), ("focus", STEAM.address)])

    def test_ipc_loss_restores_desktop_and_shows_home(self):
        m = self.model
        m.update_windows([STEAM], STEAM)
        m.begin_launch("cloud")
        effects = m.ipc_lost()
        self.assertIn(("reset_input",), effects)
        self.assertIn(("mapper", DESKTOP), effects)
        self.assertIn(("show",), effects)
        self.assertIsNone(m.pending)
        self.assertEqual(m.desired_mode(), DESKTOP)


class RecognitionTest(unittest.TestCase):
    """Fallbacks for games the catalog cannot name (review issue 1)."""

    PARENTS = {900: 800, 800: 500, 500: 1, 700: 1, 950: 1}

    def setUp(self):
        self.catalog = load_catalog(DATA / "catalog.json")
        self.parents = dict(self.PARENTS)
        self.starts = {pid: 1000 + pid for pid in (500, 700, 800, 900, 950)}
        self.model = core.HomeModel(core.Classifier(self.catalog, self.parents.get, self.starts.get))
        self.model.ipc_restored()

    def mode_for(self, window, others=()):
        self.model.update_windows([*others, window], window)
        return self.model.desired_mode()

    def test_unknown_fullscreen_window_is_a_game(self):
        self.assertEqual(self.mode_for(W("0x60", "eldenring.exe", "ELDEN RING", fullscreen=True)), GAME)

    def test_fullscreen_desktop_apps_stay_desktop(self):
        for cls in ("mpv", "chromium", "chrome-youtube.com__-Default", "Alacritty", "org.gnome.Nautilus"):
            self.assertEqual(self.mode_for(W("0x61", cls, "x", fullscreen=True)), DESKTOP, cls)

    def test_windowed_child_of_supported_launcher_is_a_game(self):
        child = W("0x62", "wine-something", "Game", pid=900)  # 900 -> 800 -> heroic(500)
        self.assertEqual(self.mode_for(child, [HEROIC]), GAME)
        self.assertEqual(self.model.kind(HEROIC), core.KIND_GAME)

    def test_unrelated_windowed_app_stays_desktop(self):
        self.assertEqual(self.mode_for(W("0x63", "gimp", "GIMP", pid=700), [HEROIC]), DESKTOP)
        self.assertEqual(self.mode_for(W("0x64", "Alacritty", "shell", pid=900), [HEROIC]), DESKTOP)

    def test_game_stays_game_when_launcher_window_closes(self):
        child = W("0x62", "wine-something", "Game", pid=900)
        self.assertEqual(self.mode_for(child, [HEROIC]), GAME)
        self.model.update_windows([child], child)  # Heroic window closed, process alive
        self.assertEqual(self.model.desired_mode(), GAME)
        self.assertEqual(self.mode_for(W("0x63", "gimp", "GIMP", pid=700)), DESKTOP)

    def test_game_stays_game_after_launcher_exits_and_child_reparents(self):
        child = W("0x62", "wine-something", "Game", pid=900)
        self.assertEqual(self.mode_for(child, [HEROIC]), GAME)
        del self.starts[500]
        self.parents[800] = 1  # launcher exited; the game chain reparented to init
        self.model.update_windows([child], child)
        self.assertEqual(self.model.desired_mode(), GAME)
        self.assertEqual(self.model.classifier.launcher_pids, {})
        # A new window that only descended from the dead launcher is not a game.
        self.assertEqual(self.mode_for(W("0x65", "wine-other", "Tool", pid=800)), DESKTOP)

    def test_child_association_ends_with_process_or_pid_reuse(self):
        child = W("0x62", "wine-something", "Game", pid=900)
        self.assertEqual(self.mode_for(child, [HEROIC]), GAME)
        self.model.update_windows([HEROIC], HEROIC)  # game window closed
        del self.starts[500]
        self.starts[900] = 9999  # pid 900 reused by an unrelated process
        self.parents[900] = 1
        self.assertEqual(self.mode_for(W("0x66", "editor", "Notes", pid=900)), DESKTOP)
        self.assertEqual(self.model.classifier.child_pids, {})

    def test_fullscreen_int_mode_parsing(self):
        base = {"address": "0x70", "class": "x"}
        self.assertTrue(Window.from_hypr({**base, "fullscreen": 2}).fullscreen)
        self.assertFalse(Window.from_hypr({**base, "fullscreen": 1}).fullscreen)  # maximized
        self.assertTrue(Window.from_hypr({**base, "fullscreen": True}).fullscreen)

    def test_kind_rules(self):
        c = self.model.classifier
        self.assertEqual(c.kind(HOME_WIN), core.KIND_HOME)
        self.assertEqual(c.kind(STEAM_CLIENT), core.KIND_GAME)
        self.assertEqual(c.kind(W("0x1", "greenlight-desktop", "Greenlight")), core.KIND_LAUNCHER)
        self.assertEqual(c.kind(GREENLIGHT), core.KIND_GAME)
        self.assertEqual(c.kind(W("0x1", "net.lutris.Lutris", "Lutris")), core.KIND_LAUNCHER)
        self.assertEqual(c.kind(TERM), core.KIND_DESKTOP)


class SteamHandoffTest(unittest.TestCase):
    def setUp(self):
        self.model = core.HomeModel(core.Classifier(load_catalog(DATA / "catalog.json")))
        self.model.ipc_restored()
        self.model.update_windows([TERM, STEAM_CLIENT], TERM)
        self.model.set_home_state(True, True)
        self.token = self.model.begin_launch("steam")[0][2]

    def test_existing_client_retitled_to_big_picture(self):
        retitled = W(STEAM_CLIENT.address, "steam", "Steam Big Picture Mode")
        effects = self.model.update_windows([TERM, retitled], TERM)
        self.assertIn(("focus", retitled.address), effects)
        self.assertIsNone(self.model.pending)

    def test_new_big_picture_window_preferred(self):
        old = W(STEAM_CLIENT.address, "steam", "Steam Big Picture Mode", focus_history=0)
        effects = self.model.update_windows([TERM, old, STEAM], TERM)
        self.assertIn(("focus", STEAM.address), effects)

    def test_wrapper_exit_with_only_regular_client_keeps_waiting(self):
        self.assertEqual(self.model.launch_exited(self.token, True), [])
        self.assertEqual(self.model.update_windows([TERM, STEAM_CLIENT], TERM), [])
        self.assertIsNotNone(self.model.pending)

    def test_timeout_without_big_picture_fails_visibly(self):
        effects = self.model.launch_timed_out(self.token)
        self.assertIn(("show",), effects)
        self.assertEqual(self.model.message[1], "status.launchTimeout")


class FakeClock:
    def __init__(self):
        self.timers, self.next = {}, 0

    def schedule(self, ms, callback):
        self.next += 1
        self.timers[self.next] = callback
        return self.next

    def cancel(self, token):
        self.timers.pop(token, None)

    def fire_all(self):
        timers, self.timers = self.timers, {}
        for callback in timers.values():
            callback()


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.actions = []
        self.resyncs = []
        self.ci = core.ControllerInterpreter(self.clock.schedule, self.clock.cancel, self.actions.append,
                                             on_resync=self.resyncs.append)
        self.ci.set_nav_enabled(True)

    def key(self, code, value, dev="d"):
        self.ci.feed(dev, EV_KEY, code, value)
        self.ci.feed(dev, EV_SYN, SYN_REPORT, 0)

    def test_chord_requires_hold(self):
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.key(BTN_START, 0)  # released before 0.8s
        self.clock.fire_all()
        self.assertNotIn(HOME, self.actions)
        self.assertNotIn(MENU, self.actions)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions.count(HOME), 1)
        self.clock.fire_all()
        self.assertEqual(self.actions.count(HOME), 1)

    def test_chord_works_when_nav_disabled_but_menu_does_not(self):
        self.ci.set_nav_enabled(False)
        self.key(BTN_START, 1)
        self.key(BTN_START, 0)
        self.assertEqual(self.actions, [])
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [HOME])

    def test_syn_dropped_cancels_chord_and_resyncs(self):
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.ci.feed("d", EV_SYN, SYN_DROPPED, 0)
        self.assertEqual(self.clock.timers, {})
        self.ci.feed("d", EV_KEY, BTN_SOUTH, 1)  # ignored until SYN_REPORT
        self.ci.feed("d", EV_SYN, SYN_REPORT, 0)
        self.assertEqual(self.resyncs, ["d"])
        self.ci.sync_state("d", {BTN_SELECT, BTN_START}, {})
        self.clock.fire_all()
        self.assertEqual(self.actions, [])

    def test_buttons_and_repeat_only_while_held(self):
        self.key(BTN_SOUTH, 1)
        self.key(BTN_EAST, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 1)
        self.assertEqual(self.actions, [ACTIVATE, "back", DOWN])
        self.clock.fire_all()
        self.assertEqual(self.actions[-1], DOWN)
        self.assertEqual(len(self.clock.timers), 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 0)
        self.assertEqual(self.clock.timers, {})

    def test_unsigned_stick_range_uses_kernel_center(self):
        self.ci.set_range("d", core.ABS_Y, 0, 65535, 4000)
        self.ci.feed("d", EV_ABS, core.ABS_Y, 32768)
        self.assertEqual(self.actions, [])
        self.ci.feed("d", EV_ABS, core.ABS_Y, 65535)
        self.assertEqual(self.actions, [DOWN])

    def test_guide_never_acts(self):
        self.key(core.BTN_MODE, 1)
        self.clock.fire_all()
        self.key(core.BTN_MODE, 0)
        self.assertEqual(self.actions, [])

    def test_ordinary_pad_still_acts_on_what_is_pressed_while_guide_is_held(self):
        # Only Guide itself is ignored; what Steam makes of a Guide chord is its own business.
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SOUTH, 1)
        self.key(BTN_START, 1)
        self.key(BTN_START, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.assertEqual(self.actions, [ACTIVATE, MENU, HOME, core.RIGHT])

    def test_buttons_pressed_while_guide_is_held_never_act_on_a_pad_that_sends_guide_chords(self):
        # InputPlumber's Xbox target sends a handheld's vendor button as Guide + A.
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SOUTH, 1)
        self.key(BTN_SOUTH, 0)
        self.key(BTN_START, 1)
        self.key(BTN_START, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.key(BTN_START, 0)
        self.key(BTN_SELECT, 0)
        self.key(core.BTN_MODE, 0)
        self.assertEqual(self.actions, [])
        self.key(BTN_SOUTH, 1)
        self.assertEqual(self.actions, [ACTIVATE])

    def test_guide_pressed_during_a_hold_cancels_menu_and_home_on_such_a_pad(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(BTN_START, 1)
        self.key(core.BTN_MODE, 1)
        self.key(BTN_START, 0)
        self.key(core.BTN_MODE, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.key(core.BTN_MODE, 1)
        self.key(core.BTN_MODE, 0)             # a vendor button tapped during the hold
        self.clock.fire_all()
        self.key(BTN_START, 0)
        self.assertEqual(self.actions, [])
        self.key(BTN_SELECT, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [HOME])

    def test_button_of_a_guide_chord_stays_ignored_until_it_is_released(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SELECT, 1)
        self.key(core.BTN_MODE, 0)
        self.key(BTN_START, 1)                 # View is still down, but it was pressed for the Guide chord
        self.assertEqual(self.clock.timers, {})
        self.key(BTN_START, 0)
        self.assertEqual(self.actions, [MENU])
        self.key(BTN_SELECT, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [MENU, HOME])

    def test_direction_taken_while_guide_is_held_is_ignored_until_it_is_released(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.ci.feed("d", EV_ABS, core.ABS_Y, 32767)
        self.key(core.BTN_MODE, 0)
        self.assertEqual((self.actions, self.clock.timers), ([], {}))
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 1)   # a direction pushed after Guide counts, as a button does
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 0)
        self.assertEqual((self.actions, self.clock.timers), ([DOWN], {}))
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)   # let go and pushed again; the stick is still held
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.assertEqual((self.actions, self.clock.timers), ([DOWN, core.RIGHT], {}))
        self.ci.feed("d", EV_ABS, core.ABS_Y, 0)
        self.ci.feed("d", EV_ABS, core.ABS_Y, 32767)
        self.assertEqual(self.actions, [DOWN, core.RIGHT, DOWN])

    def test_chord_direction_stays_ignored_when_the_direction_held_before_guide_on_its_axis_is_let_go(self):
        self.ci.add_device("d", guide_chords=True)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, -1)  # held before Guide
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)   # the other way on the stick, hidden by the D-pad
        self.key(core.BTN_MODE, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.assertEqual((self.actions, self.clock.timers), ([core.LEFT], {}))
        self.ci.feed("d", EV_ABS, core.ABS_X, 0)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.assertEqual(self.actions, [core.LEFT, core.RIGHT])

    def test_chord_direction_stays_ignored_when_the_other_d_pad_axis_is_let_go(self):
        self.ci.add_device("d", guide_chords=True)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 1)   # held before Guide
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)   # down-right: down wins, right is the chord's
        self.key(core.BTN_MODE, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 0)
        self.assertEqual((self.actions, self.clock.timers), ([DOWN], {}))

    def test_ordinary_pad_follows_any_direction_held_with_guide(self):
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, -1)
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.key(core.BTN_MODE, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 1)
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.key(core.BTN_MODE, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0Y, 0)
        self.assertEqual(self.actions, [core.LEFT, core.RIGHT, DOWN, core.RIGHT])

    def test_direction_held_before_guide_goes_on_and_a_new_one_does_not(self):
        self.ci.add_device("d", guide_chords=True)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.key(core.BTN_MODE, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [core.RIGHT, core.RIGHT])
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, -1)
        self.assertEqual((self.actions, self.clock.timers), ([core.RIGHT, core.RIGHT], {}))

    def test_what_is_held_together_with_guide_when_state_is_adopted_counts_as_its_chord(self):
        self.ci.add_device("d", guide_chords=True)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {core.BTN_MODE, BTN_SELECT}, {core.ABS_HAT0X: 1})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)          # Home is shown while Guide, View and a direction are down
        self.assertEqual(self.clock.timers, {})
        self.key(core.BTN_MODE, 0)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [])

    def test_what_belongs_to_a_guide_chord_stays_ignored_when_the_state_is_read_again(self):
        # Home reads the state again when it is focused and after dropped events; only a release ends the chord.
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SELECT, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.key(core.BTN_MODE, 0)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {BTN_SELECT}, {core.ABS_HAT0X: 1})
        self.ci.feed("d", EV_SYN, SYN_DROPPED, 0)
        self.ci.feed("d", EV_SYN, SYN_REPORT, 0)
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)          # focused again while View and right are still held
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.key(BTN_START, 0)
        self.assertEqual(self.actions, [MENU])
        self.key(BTN_SELECT, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.assertEqual(self.actions, [MENU, HOME, core.RIGHT])

    def test_guide_chord_ends_when_the_state_read_again_shows_it_released(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SELECT, 1)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.key(core.BTN_MODE, 0)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, set(), {core.ABS_X: 0})
        self.ci.feed("d", EV_SYN, SYN_DROPPED, 0)  # both were let go among the dropped events
        self.ci.feed("d", EV_SYN, SYN_REPORT, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.assertEqual(self.actions, [HOME, core.RIGHT])

    def test_ordinary_pad_keeps_a_button_held_with_guide_when_the_state_is_read_again(self):
        self.key(core.BTN_MODE, 1)
        self.key(BTN_SELECT, 1)
        self.key(core.BTN_MODE, 0)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {BTN_SELECT}, {})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [HOME])

    def test_what_a_resync_finds_held_without_seeing_it_pressed_is_ignored_until_released(self):
        # Reading the keys drops the key events still queued: Guide down, View down and Guide up, or the
        # Guide around a D-pad press, may never be seen. Only what is held afterwards is.
        self.ci.add_device("d", guide_chords=True)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {BTN_SELECT}, {core.ABS_HAT0X: 1})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.key(BTN_START, 0)
        self.assertEqual(self.actions, [MENU])
        self.key(BTN_SELECT, 0)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.key(BTN_SELECT, 1)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.assertEqual(self.actions, [MENU, HOME, core.RIGHT])

    def test_hold_seen_without_guide_still_counts_after_a_resync(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(BTN_SELECT, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {BTN_SELECT}, {core.ABS_HAT0X: 1})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)          # focus went away and came back while View and right stayed down
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.assertEqual(self.actions, [core.RIGHT, core.RIGHT, HOME])

    def test_chord_stick_direction_eased_into_the_hysteresis_band_is_not_released_by_a_resync(self):
        self.ci.add_device("d", guide_chords=True)
        self.key(core.BTN_MODE, 1)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.key(core.BTN_MODE, 0)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, set(), {core.ABS_X: 15000})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)   # pushed further, never let go
        self.assertEqual((self.actions, self.clock.timers), ([], {}))
        self.ci.feed("d", EV_ABS, core.ABS_X, 0)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.assertEqual(self.actions, [core.RIGHT])

    def test_ordinary_pad_adopts_what_a_resync_finds_held_as_before(self):
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, {BTN_SELECT}, {core.ABS_X: 15000, core.ABS_HAT0X: 1})
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)
        self.key(BTN_START, 1)
        self.clock.fire_all()
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 0)
        self.ci.feed("d", EV_ABS, core.ABS_X, 32767)   # the band released the stick: this is a new push
        self.assertEqual(self.actions, [core.RIGHT, core.RIGHT, HOME, core.RIGHT])

    def test_reenabled_nav_resumes_held_direction_without_stale_press(self):
        held = {"d": ({BTN_SOUTH}, {core.ABS_HAT0X: 1})}
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, *held[dev])
        self.key(BTN_SOUTH, 1)
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.ci.set_nav_enabled(False)
        self.assertEqual(self.clock.timers, {})
        del self.actions[:]
        self.ci.set_nav_enabled(True)
        self.assertEqual(self.actions, [])  # no stale A, no instant jump
        self.clock.fire_all()
        self.assertEqual(self.actions, [core.RIGHT])
        self.key(BTN_SOUTH, 0)
        self.key(BTN_SOUTH, 1)
        self.assertEqual(self.actions[-1], ACTIVATE)

    def test_reenable_after_release_while_disabled_does_not_repeat(self):
        self.ci.on_resync = lambda dev: self.ci.sync_state(dev, set(), {})
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.ci.set_nav_enabled(False)
        self.ci.set_nav_enabled(True)
        self.assertEqual(self.clock.timers, {})

    def test_unplug_clears_held_state(self):
        self.ci.feed("d", EV_ABS, core.ABS_HAT0X, 1)
        self.key(BTN_SELECT, 1)
        self.ci.remove_device("d")
        self.assertEqual(self.clock.timers, {})
        self.assertFalse(self.ci.held())


class CatalogTest(unittest.TestCase):
    BASE = {"id": "x", "name": "X", "launch": ["x"], "availability": {"command": "x"}}

    def test_install_vectors_are_fixed_omarchy_commands(self):
        parse_entry({**self.BASE, "install": ["omarchy", "install", "gaming", "x"]})
        for bad in (["sudo", "pacman", "-S", "x"], ["omarchy", "install", "x;rm"], ["omarchy", "install"]):
            with self.assertRaises(CatalogError):
                parse_entry({**self.BASE, "install": bad})

    def test_shipped_catalog_is_valid(self):
        catalog = load_catalog(DATA / "catalog.json")
        self.assertEqual(catalog.errors, [])
        self.assertEqual(catalog.source, str(DATA / "catalog.json"))

    def test_xbox_cloud_web_app_is_a_game_in_any_locale_and_other_xbox_pages_are_not(self):
        # While a stream runs, the window title is the game's name: only the class tells what it is.
        model = core.HomeModel(core.Classifier(load_catalog(DATA / "catalog.json")))
        model.ipc_restored()
        for cls in ("chrome-www.xbox.com__en-US_play-Default", "chrome-www.xbox.com__fr-FR_play-Default",
                    "msedge-www.xbox.com__de-DE_play-Default", "chromium-www.xbox.com__ja-JP_play-Profile_1",
                    "chrome-www.xbox.com__zh-Hans-CN_play-Default", "chrome-www.xbox.com__play-Default",
                    "msedge-www.xbox.com__play-Profile_1", "CHROMIUM-WWW.XBOX.COM__ES-419_PLAY-DEFAULT"):
            window = W("0x71", cls, "Forza Horizon 5")
            self.assertEqual(model.service(window), "cloud", cls)
            model.update_windows([window], window)
            self.assertEqual(model.desired_mode(), GAME, cls)
        for cls in ("chrome-support.xbox.com__en-US_help_play-Default", "chrome-support.xbox.com__en-US_play-Default",
                    "msedge-support.xbox.com__play-Default", "chrome-www.xbox.com__en-US_playground-Default",
                    "chrome-www.xbox.com__en-US_remoteplay-Default",
                    "chrome-www.xbox.com__en-US_consoles_remote-play-Default",      # a product page
                    "chrome-www.xbox.com__en-US_games_xbox-play-anywhere-Default",
                    "chrome-www.xbox.com__en-US_xbox-game-pass-Default", "chrome-www.xbox.com__-Default",
                    "chrome-www.xbox.com__en-US_playlists-Default", "chrome-www.xbox.com_play-Default",
                    "notchrome-www.xbox.com__en-US_play-Default", "brave-www.xbox.com__en-US_play-Default",
                    "chrome-www.xbox.com.example.org__en-US_play-Default",
                    "chrome-www.example.com__en-US_play-Default",
                    "chrome-evil.example__www.xbox.com__en-US_play-Default"):
            window = W("0x72", cls, "Forza Horizon 5")
            self.assertIsNone(model.service(window), cls)
            model.update_windows([window], window)
            self.assertEqual(model.desired_mode(), DESKTOP, cls)

    def test_class_glob_finds_its_literal_parts_in_order_in_any_case(self):
        entry = parse_entry({**self.BASE, "match": {"globs": ["example-app_*_play-*", "example-tool*-end",
                                                               "Straße-App*", "EXAMPLE-\u0130-APP-*"]}})
        for cls in ("example-app__play-", "EXAMPLE-APP_en_play-Profile_1", "example-app_a_play_b_play-c",
                    "example-tool-end", "Example-Tool-x-END", "STRASSE-APP-1", "example-\u0130-app-2"):
            self.assertTrue(entry.matches(cls, "", ""), cls)
            self.assertTrue(entry.matches("", cls, ""), cls)
        for cls in ("my-example-app__play-", "example-app_play-", "example-app__playground-x",
                    "example-tool-end-2", "example-tool", "example-i-app-3", "example_app__play-"):
            self.assertFalse(entry.matches(cls, "", ""), cls)

    def test_invalid_class_glob_is_ignored_with_one_warning_and_the_entry_kept(self):
        for glob in (7, ["list"], "", "short*", "*example-app", "example-app" + "*x" * 5,
                     "example-app-" + "x" * 120):
            with self.subTest(glob=glob):
                catalog = parse_catalog({"schemaVersion": 1, "apps": [
                    {**self.BASE, "match": {"classes": ["x-app"], "globs": [glob, "example-app-*"]}}]})
                self.assertEqual(catalog.ids, ["x"])
                self.assertEqual(len(catalog.errors), 1, catalog.errors)
                self.assertTrue(catalog.errors[0].startswith("x: globs[0] ignored: "), catalog.errors)
                for cls in ("x-app", "EXAMPLE-APP-7"):
                    self.assertIs(catalog.match_window(cls, "", ""), catalog.get("x"), cls)
        for globs, warning in (("example-app-*", "x: globs ignored: "), (["example-app-*"] * 9, "x: globs[8:] ignored: ")):
            catalog = parse_catalog({"schemaVersion": 1, "apps": [{**self.BASE, "match": {"globs": globs}}]})
            self.assertEqual((catalog.ids, len(catalog.errors)), (["x"], 1))
            self.assertTrue(catalog.errors[0].startswith(warning), catalog.errors)
        catalog = parse_catalog({"schemaVersion": 1, "apps": [
            {**self.BASE, "match": {"globs": ["abcdefgh*", "abcdefgh*a*b**", "a" * 120]}}]})
        self.assertEqual(catalog.errors, [])

    def test_class_globs_match_long_classes_in_linear_time(self):
        entry = parse_entry({**self.BASE, "match": {"globs": ["chrome-www.xbox.com_*_play-*",
                                                               "example-app_*_a_*_b_*_c_*-end"]}})
        started = time.perf_counter()
        for cls in ("chrome-www.xbox.com_" + "_" * 100_000, "chrome-www.xbox.com_" + "_pla" * 25_000 + "y",
                    "example-app_" + "_a_b_c" * 17_000 + "-en", "example-app_" + "_a_" * 33_000 + "-end"):
            self.assertFalse(entry.matches(cls, "", ""))
        self.assertLess(time.perf_counter() - started, 0.25)


class FakeBus:
    """Synchronous stand-in for Gio.DBusConnection used by the mapper."""

    def __init__(self, sets):
        self.sets = dict(sets)
        self.calls = []

    def _reply(self, path, method, args):
        from gi.repository import GLib
        self.calls.append((path, method, args))
        if method == "Introspect":
            nodes = "".join(f'<node name="{p.rsplit("/", 1)[1]}"/>' for p in self.sets)
            return GLib.Variant("(s)", (f"<node>{nodes}</node>",))
        if method == "setActiveSetNumber":
            self.sets[path] = args.unpack()[0]
            return None
        return GLib.Variant("(i)", (self.sets[path],))

    def call(self, _name, path, _iface, method, args, _reply, _flags, _timeout, _cancel, callback, data):
        callback(self, self._reply(path, method, args), data)

    def call_finish(self, result):
        return result

    def call_sync(self, _name, path, _iface, method, args, *_rest):
        return self._reply(path, method, args)


class MapperTest(unittest.TestCase):
    def mapper(self, managed):
        from gaminghome.mapper import AntimicroxMapper
        mapper = AntimicroxMapper(lambda: None, managed=managed)
        bus = FakeBus({"/InputDevice/a": 3})
        mapper._appeared(bus, None, None)
        return mapper, bus

    def test_unmanaged_mapper_is_never_touched_and_blocks_raw_nav(self):
        mapper, bus = self.mapper(False)
        mapper.request(GAME)
        mapper.stop()
        self.assertEqual(bus.calls, [])
        self.assertTrue(mapper.external)
        self.assertFalse(mapper.raw_navigation_allowed())

    def test_managed_mapper_snapshots_once_and_restores_original_set(self):
        mapper, bus = self.mapper(True)
        mapper.request(GAME)
        self.assertEqual(mapper.confirmed, GAME)
        self.assertTrue(mapper.raw_navigation_allowed())
        mapper.request(DESKTOP)
        self.assertEqual(bus.sets["/InputDevice/a"], 0)
        self.assertEqual(mapper.initial, {"/InputDevice/a": 3})
        mapper.stop()
        self.assertEqual(bus.sets["/InputDevice/a"], 3)
        mapper.stop()  # idempotent

    def test_opting_out_restores_and_stops_switching(self):
        mapper, bus = self.mapper(True)
        mapper.request(GAME)
        mapper.set_managed(False)
        self.assertEqual(bus.sets["/InputDevice/a"], 3)
        count = len(bus.calls)
        mapper.request(DESKTOP)
        self.assertEqual(len(bus.calls), count)


class ApplicationTest(unittest.TestCase):
    def test_show_claims_hyprland_focus_once_when_home_maps_unfocused(self):
        # Live regression: autostarted Home mapped with no focused window, so
        # active_window stayed null and the mapper was never requested.
        import os
        from types import SimpleNamespace
        from gaminghome.application import Runtime
        model = core.HomeModel(core.Classifier(load_catalog(DATA / "catalog.json")))
        model.ipc_restored()
        focused = []
        rt = SimpleNamespace(model=model, _claim_focus=True,
                             window=SimpleNamespace(get_visible=lambda: True),
                             hypr=SimpleNamespace(focus=focused.append))
        Runtime._focus_home_if_needed(rt)  # not mapped yet: keep waiting
        self.assertEqual(focused, [])
        own = W("0x41", APP_ID, "Omarchy Game Mode", pid=os.getpid())
        model.update_windows([GREENLIGHT, HOME_WIN, own], None)
        Runtime._focus_home_if_needed(rt)
        Runtime._focus_home_if_needed(rt)
        self.assertEqual(focused, ["0x41"])
        model.update_windows([GREENLIGHT, own], own)
        model.set_home_state(True, True)
        self.assertEqual(model.desired_mode(), GAME)

    class StubRuntime:
        def __init__(self):
            self.shown, self.stops = 0, 0

        def show(self, page=None):
            self.shown += 1

        def stop(self):
            self.stops += 1

    def test_autostart_shows_background_stays_hidden_and_shutdown_is_idempotent(self):
        from gaminghome.application import GameModeApplication
        app = GameModeApplication()
        stub = app.runtime = self.StubRuntime()
        app.hold()
        self.assertEqual(app.dispatch(["--autostart"]), (0, ""))
        self.assertEqual(app.dispatch(["background"]), (0, ""))
        self.assertEqual(stub.shown, 1)
        app.shutdown_runtime()
        app.shutdown_runtime()
        self.assertEqual(stub.stops, 1)


class I18nTest(unittest.TestCase):
    def test_every_ui_key_is_in_the_locale_files(self):
        from gaminghome.i18n import DEFAULTS
        english = json.loads((DATA / "locales/en.json").read_text())
        self.assertEqual(set(DEFAULTS), set(english))

    def test_locale_key_and_placeholder_parity(self):
        english = json.loads((DATA / "locales/en.json").read_text())
        placeholders = lambda text: sorted(set(__import__("re").findall(r"\{(\w+)\}", text)))  # noqa: E731
        for path in (DATA / "locales").glob("*.json"):
            data = json.loads(path.read_text())
            self.assertEqual(set(data), set(english), path.name)
            for key, text in data.items():
                self.assertEqual(placeholders(text), placeholders(english[key]), f"{path.name}:{key}")

    def test_language_resolution_and_rtl(self):
        missing = Path("/nonexistent/language")
        t = Translator(DATA / "locales", "system", {"LANG": "pt_BR.UTF-8"}, missing)
        self.assertEqual(t.language, "pt-BR")
        t = Translator(DATA / "locales", "ar", {"LANG": "de_DE.UTF-8"}, missing)
        self.assertTrue(t.rtl)
        t = Translator(DATA / "locales", "system", {"LANG": "C"}, missing)
        self.assertEqual(t.language, "en")
        self.assertEqual(t("missing.key"), "missing.key")
        self.assertEqual(t("status.starting", name="Steam"), "Starting Steam...")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.env = {"XDG_CONFIG_HOME": str(SCRATCH), "HOME": str(SCRATCH), "OMARCHY_GAME_MODE_EXEC": "/bin/true"}

    def tearDown(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)

    def test_autostart_round_trip_and_ownership(self):
        ok, _ = settings.set_autostart(True, self.env)
        self.assertTrue(ok and settings.autostart_enabled(self.env))
        entry = settings.autostart_path(self.env).read_text()
        self.assertIn("Exec=/bin/true --autostart", entry)
        self.assertIn("Comment=Show Omarchy Game Mode at login", entry)
        self.assertTrue(settings.set_autostart(False, self.env)[0])
        path = settings.autostart_path(self.env)
        path.write_text("[Desktop Entry]\nExec=other\n")
        self.assertFalse(settings.set_autostart(False, self.env)[0])
        self.assertTrue(path.exists())

    def test_autostart_migrates_owned_legacy_entry_and_removes_both(self):
        legacy = settings.legacy_autostart_paths(self.env)[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(settings.autostart_entry("/bin/true"))
        self.assertTrue(settings.autostart_enabled(self.env))
        self.assertTrue(settings.set_autostart(True, self.env)[0])
        self.assertFalse(legacy.exists())
        self.assertTrue(settings.autostart_path(self.env).exists())
        legacy.write_text(settings.autostart_entry("/bin/true"))
        self.assertTrue(settings.set_autostart(False, self.env)[0])
        self.assertFalse(legacy.exists())
        self.assertFalse(settings.autostart_path(self.env).exists())
        self.assertFalse(settings.autostart_enabled(self.env))

    def test_autostart_preserves_unowned_legacy_entry(self):
        legacy = settings.legacy_autostart_paths(self.env)[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        foreign = "[Desktop Entry]\nExec=other\n"
        legacy.write_text(foreign)
        self.assertFalse(settings.autostart_enabled(self.env))
        self.assertTrue(settings.set_autostart(True, self.env)[0])
        self.assertTrue(settings.set_autostart(False, self.env)[0])
        self.assertEqual(legacy.read_text(), foreign)

    def test_pins_persist_and_keep_unknown_ids(self):
        catalog = load_catalog(DATA / "catalog.json")
        s = settings.Settings(SCRATCH / "settings.json")
        s.library = ["future-app"]
        s.set_pinned(catalog, "moonlight", True)
        again = settings.Settings(SCRATCH / "settings.json")
        self.assertEqual(again.pinned(catalog), ["moonlight"])
        self.assertIn("future-app", again.library)
        self.assertFalse(again.mapper_managed)
        again.mapper_managed = True
        again.save()
        self.assertTrue(settings.Settings(SCRATCH / "settings.json").mapper_managed)


if __name__ == "__main__":
    unittest.main()
