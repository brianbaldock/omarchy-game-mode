"""What the documentation promises about Guide, InputPlumber and the Ally layout has to stay true."""

import re
import unittest
from pathlib import Path

from gaminghome import quirks

ROOT = Path(__file__).resolve().parent.parent


def text(relative) -> str:
    """A document with every run of whitespace collapsed, so line breaks do not matter."""
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


def comments(relative) -> str:
    """The comment lines of a configuration file, collapsed the same way."""
    lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()
    return " ".join(" ".join(line.lstrip("#").split()) for line in lines if line.startswith("#"))


README, CONTRIBUTING = text("README.md"), text("CONTRIBUTING.md")
# The source archive leaves the website out, so its wording is only checked in a full checkout.
SITE = text("site/index.html") if (ROOT / "site" / "index.html").is_file() else ""
needs_site = unittest.skipUnless(SITE, "the source archive has no website")
HWDB = comments("contrib/udev/61-rog-xbox-ally-gamepad.hwdb")
REPORT_FORM = text(".github/ISSUE_TEMPLATE/hardware-validation.yml")


class DocsTest(unittest.TestCase):
    def test_no_working_notes_in_user_facing_text(self):
        notes = re.compile(r"\b(workstreams?|orchestrator|rubber[ -]duck|session[- ]state|critique|WS[0-9])\b", re.I)
        documents = [("README.md", README), ("CONTRIBUTING.md", CONTRIBUTING)]
        documents += [("site/index.html", SITE)] if SITE else []
        for name, document in documents:
            self.assertEqual(notes.findall(document), [], name)

    def test_claims_that_reading_inputplumber_made_false_are_gone(self):
        self.assertNotIn("not even HHD or InputPlumber", README)
        self.assertNotIn("Virtual controllers are currently skipped", README)
        self.assertNotIn("HHD/InputPlumber compatibility is not established", CONTRIBUTING)
        self.assertNotIn("HHD/InputPlumber are not yet validated", SITE)
        self.assertNotIn("not components it uses", SITE)
        self.assertNotIn("HHD/InputPlumber compatibility is unverified", REPORT_FORM)

    def test_inputplumber_is_described_as_read_only_and_not_as_an_upgrade(self):
        self.assertIn("it loads no profile, sets no property and sends no event", README)
        self.assertIn("it is not a mapping upgrade by itself", README)
        self.assertIn("never changes it", CONTRIBUTING)
        self.assertIsNone(re.search(r"InputPlumber (improves|fixes|restores)", README + CONTRIBUTING + SITE))

    @needs_site
    def test_website_says_inputplumber_is_never_changed(self):
        self.assertIn("never changes it", SITE)

    def test_readme_separates_verified_from_pending(self):
        tiers = ("**Verified by automated tests:**", "without physical input:**",
                 "**Pending physical validation, and known limits:**")
        positions = [README.index(tier) for tier in tiers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("No button was pressed.", README)

    def test_r3_and_xbox_button_statements_say_what_was_read(self):
        # The virtual pad has the button; InputPlumber's sources do not report one to feed it.
        self.assertNotIn("the virtual pad reports no right stick click", README)
        self.assertIn("does advertise a right stick click (`BTN_THUMBR`), but nothing feeds it", README)
        # Of the two maps, only HHD's has an entry for the Xbox button.
        self.assertNotIn("R3 as `BTN_TR2` and the Xbox button as `BTN_SELECT`", README)
        self.assertIn("Only HHD's map also lists the Xbox button", README)
        self.assertIn("A guided press test on an RC73YA has since confirmed the", README)
        self.assertNotIn("no guided physical test on an RC73YA has confirmed", README)

    def test_ally_layout_section_says_what_the_press_test_confirmed_and_what_is_pending(self):
        self.assertIn(f"`{quirks.DISABLE_ENV}=1`", README)
        shipped = re.findall(r"/usr/share/omarchy-game-mode/(contrib/\S+\.hwdb)", README)
        self.assertTrue(shipped)
        for relative in shipped:
            self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertIn("confirmed by a guided press test on one ROG Xbox Ally (RC73YA, Linux 7.2)", README)
        self.assertNotIn("not yet confirmed by a physical press test", README)
        # What the press test did not cover stays listed as pending.
        pending = README[README.index("- **Pending physical validation:**"):]
        pending = pending[:pending.index("## Controller modes")]
        for item in ("presses of the three buttons of unknown meaning", "how Steam, SDL games and browsers",
                     "anything on the Xbox Ally X"):
            self.assertIn(item, pending)

    def test_ally_layout_section_names_the_small_buttons_as_measured(self):
        section = README[README.index("### ASUS ROG Xbox Ally controller layout"):README.index("## Controller modes")]
        for fact in ("`KEY_F16`", "`KEY_F15`", "`KEY_F22`", 'o.bind("code:200"', "second press test"):
            self.assertIn(fact, section)
        self.assertNotIn("a press test with the entry applied;", section)

    def test_hwdb_entry_is_applied_with_a_reboot_and_applying_it_live_carries_its_warning(self):
        # Applied to a running pad, it can leave a button held meanwhile down under its old code.
        raw = (ROOT / "README.md").read_text(encoding="utf-8")
        blocks = [block for block in re.findall(r"```bash\n(.*?)```", raw, re.S) if "hwdb" in block]
        self.assertEqual(len(blocks), 2)                       # apply and undo
        for block in blocks:
            self.assertIn("systemd-hwdb update", block)
            self.assertNotIn("udevadm trigger", block)
        for document in (README, HWDB):
            self.assertIn("reboot", document)
            self.assertIn("with every button released", document)
        self.assertIn("a restart is the cleanest", README)

    def test_guide_promise_is_kept_everywhere(self):
        self.assertIn("Guide/Xbox button is deliberately left alone", README)
        self.assertIn("capture Guide", CONTRIBUTING)

    @needs_site
    def test_website_keeps_the_guide_promise(self):
        self.assertIn("Game Mode never acts on the Xbox Guide button", SITE)
        self.assertIn("It never acts on the Guide button", SITE)


if __name__ == "__main__":
    unittest.main()
