"""What the documentation promises about Guide and InputPlumber has to stay true."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def text(relative) -> str:
    """A document with every run of whitespace collapsed, so line breaks do not matter."""
    return " ".join((ROOT / relative).read_text(encoding="utf-8").split())


README, CONTRIBUTING, SITE = text("README.md"), text("CONTRIBUTING.md"), text("site/index.html")
REPORT_FORM = text(".github/ISSUE_TEMPLATE/hardware-validation.yml")


class DocsTest(unittest.TestCase):
    def test_no_working_notes_in_user_facing_text(self):
        notes = re.compile(r"\b(workstreams?|orchestrator|rubber[ -]duck|session[- ]state|critique|WS[0-9])\b", re.I)
        for name, document in (("README.md", README), ("CONTRIBUTING.md", CONTRIBUTING), ("site/index.html", SITE)):
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
        self.assertIn("never changes it", SITE)
        self.assertIsNone(re.search(r"InputPlumber (improves|fixes|restores)", README + CONTRIBUTING + SITE))

    def test_readme_separates_verified_from_pending(self):
        tiers = ("**Verified by automated tests:**", "without physical input:**",
                 "**Pending physical validation, and known limits:**")
        positions = [README.index(tier) for tier in tiers]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("No button was pressed.", README)

    def test_guide_promise_is_kept_everywhere(self):
        self.assertIn("Game Mode never acts on the Xbox Guide button", SITE)
        self.assertIn("It never acts on the Guide button", SITE)
        self.assertIn("Guide/Xbox button is deliberately left alone", README)
        self.assertIn("capture Guide", CONTRIBUTING)


if __name__ == "__main__":
    unittest.main()
