"""The version, the changelog and everything generated from them stay in step.

The tool is tested on small changelogs and on scratch copies of the repository; the last classes check the real
files, so a change to CHANGELOG.md or to the version that leaves the website or the README behind fails here.
"""

import contextlib
import datetime
import html.parser
import io
import shutil
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import release  # noqa: E402

SCRATCH = ROOT / "tests" / "scratch" / "release"
HAS_SITE = (ROOT / "site" / "index.html").is_file()
BASE = "https://github.com/brianbaldock/omarchy-game-mode"

GOOD = f"""# Changelog

Introduction, which nothing reads.

## [Unreleased]

### Added

- Something that is coming (#9).

## [1.1.0] - 2026-02-01

### Added

- **Big thing** that names `a command`
  and wraps onto a second line (#3).

### Fixed

- A bug.

## [1.0.0] - 2026-01-01

### Added

- The first release.

[Unreleased]: {BASE}/compare/v1.1.0...HEAD
[1.1.0]: {BASE}/compare/v1.0.0...v1.1.0
[1.0.0]: {BASE}/releases/tag/v1.0.0
"""


class ParseTest(unittest.TestCase):
    def test_reads_releases_groups_entries_and_wrapped_entries(self):
        log = release.parse(GOOD)
        self.assertEqual([(r.version, r.date) for r in log.releases],
                         [("Unreleased", ""), ("1.1.0", "2026-02-01"), ("1.0.0", "2026-01-01")])
        self.assertEqual(log.latest.version, "1.1.0")
        self.assertEqual(log.unreleased.count, 1)
        added, fixed = log.get("1.1.0").groups
        self.assertEqual(added, ("Added", ("**Big thing** that names `a command` and wraps onto a second line (#3).",)))
        self.assertEqual(fixed, ("Fixed", ("A bug.",)))

    def test_an_empty_unreleased_section_is_fine(self):
        log = release.parse(GOOD.replace("### Added\n\n- Something that is coming (#9).\n\n", "", 1))
        self.assertEqual(log.unreleased.count, 0)

    def test_every_malformed_changelog_is_refused_with_a_reason(self):
        swapped = GOOD.replace("### Added\n\n- **Big thing** that names `a command`\n  and wraps onto a second line "
                               "(#3).\n\n### Fixed\n\n- A bug.",
                               "### Fixed\n\n- A bug.\n\n### Added\n\n- **Big thing** that names `a command`\n  and "
                               "wraps onto a second line (#3).", 1)
        no_unreleased = (GOOD.replace("## [Unreleased]\n\n### Added\n\n- Something that is coming (#9).\n\n", "")
                         .replace(f"[Unreleased]: {BASE}/compare/v1.1.0...HEAD\n", ""))
        cases = {
            "no title": (GOOD.replace("# Changelog", "Changes", 1), "has to start"),
            "heading": (GOOD.replace("## [1.1.0] - 2026-02-01", "## 1.1.0", 1), "a release is"),
            "impossible date": (GOOD.replace("2026-02-01", "2026-02-30", 1), "is not a date"),
            "dated unreleased": (GOOD.replace("## [Unreleased]", "## [Unreleased] - 2026-03-01", 1), "has a date"),
            "undated release": (GOOD.replace("## [1.1.0] - 2026-02-01", "## [1.1.0]", 1), "has a date"),
            "unknown group": (GOOD.replace("### Fixed", "### Misc", 1), "a group is one of"),
            "repeated group": (GOOD.replace("### Fixed", "### Added", 1), "appears twice"),
            "entry without group": (GOOD.replace("### Added\n\n- **Big", "- stray\n\n### Added\n\n- **Big", 1),
                                    "belongs under"),
            "versions out of order": (GOOD.replace("## [1.1.0]", "## [0.9.0]", 1), "newest first"),
            "dates out of order": (GOOD.replace("2026-02-01", "2025-12-31", 1), "dated before"),
            "empty release": (GOOD.replace("### Added\n\n- The first release.\n\n", "", 1), "describes nothing"),
            "empty group": (GOOD.replace("- A bug.\n\n", "", 1), "has no entries"),
            "groups out of order": (swapped, "out of order"),
            "wrong link": (GOOD.replace("compare/v1.0.0...v1.1.0", "compare/v0.5.0...v1.1.0", 1), "should end with"),
            "link for nothing": (GOOD + f"[9.9.9]: {BASE}/releases/tag/v9.9.9\n", "for nothing"),
            "text after the links": (GOOD + "More text.\n", "nothing may follow"),
            "no unreleased": (no_unreleased, "first"),
            "no release": (f"# Changelog\n\n## [Unreleased]\n\n[Unreleased]: {BASE}/commits\n", "lists no release"),
            "stray line": (GOOD.replace("- A bug.", "A bug.", 1), "expected an entry"),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(name), self.assertRaises(release.ReleaseError) as caught:
                release.parse(text)
            self.assertIn(reason, str(caught.exception), name)


class InlineTest(unittest.TestCase):
    def test_markup_in_an_entry_is_escaped_and_only_a_few_forms_mean_anything(self):
        self.assertEqual(release.inline("plain <script>alert(1)</script> & more"),
                         "plain &lt;script&gt;alert(1)&lt;/script&gt; &amp; more")
        self.assertEqual(release.inline("`<b>` and `**x**`"), "<code>&lt;b&gt;</code> and <code>**x**</code>")
        self.assertEqual(release.inline("[a](http://insecure.example) and [b](javascript:alert(1))"),
                         "[a](http://insecure.example) and [b](javascript:alert(1))")
        self.assertEqual(release.inline("[x](https://example.org/?a=1&b=2)"),
                         '<a href="https://example.org/?a=1&amp;b=2">x</a>')

    def test_code_can_sit_inside_bold_and_pull_requests_become_links(self):
        self.assertEqual(release.inline("**`doctor`** (#14)"),
                         f'<strong><code>doctor</code></strong> (<a href="{BASE}/pull/14">#14</a>)')


class RegionTest(unittest.TestCase):
    def test_a_region_is_replaced_in_place_and_indented_like_its_marker(self):
        start, end = release.markers("x")
        text = f"a\n    {start}\n    old\n    {end}\nb\n"
        self.assertEqual(release.replace_region(text, "x", "one\n  two\n", "f"),
                         f"a\n    {start}\n    one\n      two\n    {end}\nb\n")

    def test_a_missing_repeated_or_misordered_marker_is_an_error(self):
        start, end = release.markers("x")
        broken = {"none": "nothing", "no end": start, "twice": f"{start}{end}{start}{end}",
                  "backwards": f"{end}{start}", "inline": f"text {start}\n{end}"}
        for name, text in broken.items():
            with self.subTest(name), self.assertRaises(release.ReleaseError):
                release.replace_region(text, "x", "body", "f")


class ScratchCase(unittest.TestCase):
    """A scratch copy of the files the tool reads, so a release can be rehearsed without touching the real ones."""

    def setUp(self):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        self.tree = SCRATCH / "tree"
        self.addCleanup(shutil.rmtree, SCRATCH, True)

    def copy(self, site=None, unreleased="### Added\n\n- A new thing (#99).\n\n"):
        site = HAS_SITE if site is None else site
        files = ["CHANGELOG.md", "README.md", "Makefile", "PKGBUILD", "gaminghome/__init__.py"]
        files += ["site/index.html"] if site else []
        for rel in files:
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, self.tree / rel)
        changelog = self.tree / "CHANGELOG.md"
        text = changelog.read_text(encoding="utf-8")
        if unreleased and not release.parse(text).unreleased.count:
            changelog.write_text(text.replace("## [Unreleased]\n\n", f"## [Unreleased]\n\n{unreleased}", 1),
                                 encoding="utf-8")
        return release.Tree(self.tree)

    def snapshot(self):
        return {p.relative_to(self.tree).as_posix(): p.read_bytes() for p in sorted(self.tree.rglob("*")) if p.is_file()}

    def newest(self):
        return release.load(release.Tree(self.tree)).latest

    def next_release(self):
        latest = self.newest()
        major, minor, patch = release.key(latest.version)
        day = datetime.date.fromisoformat(latest.date) + datetime.timedelta(days=1)
        return f"{major}.{minor}.{patch + 1}", day.isoformat()


class BumpTest(ScratchCase):
    def test_a_release_moves_unreleased_into_a_version_and_updates_everything_that_mentions_it(self):
        self.copy()
        before = self.newest()
        version, date = self.next_release()
        changed = release.bump(self.tree, version, date)
        self.assertIn("CHANGELOG.md", changed)
        self.assertEqual(release.check(self.tree), [])
        tree = release.Tree(self.tree)
        self.assertEqual(set(release.versions_in(tree).values()), {version})
        self.assertIn("pkgrel=1", tree.read("PKGBUILD"))
        log = release.load(tree)
        self.assertEqual((log.latest.version, log.latest.date), (version, date))
        self.assertEqual(log.unreleased.count, 0)
        entries = [entry for _group, group_entries in log.latest.groups for entry in group_entries]
        self.assertEqual(entries[-1], "A new thing (#99).")
        self.assertEqual(log.links["Unreleased"], f"{BASE}/compare/v{version}...HEAD")
        self.assertEqual(log.links[version], f"{BASE}/compare/v{before.version}...v{version}")
        self.assertIn(version, tree.read("README.md"))
        if HAS_SITE:
            self.assertIn(f"v{version}", tree.read("site/index.html"))
            self.assertIn(f'id="{release.anchor(version)}"', tree.read("site/changelog.html"))
            self.assertIn(f"<lastmod>{date}</lastmod>", tree.read("site/sitemap.xml"))

    def test_a_release_can_be_made_from_a_source_archive_without_a_website(self):
        self.copy(site=False)
        version, date = self.next_release()
        changed = release.bump(self.tree, version, date)
        self.assertNotIn("site/changelog.html", changed)
        self.assertFalse((self.tree / "site").exists())
        self.assertEqual(release.check(self.tree), [])

    def test_a_refused_release_leaves_every_file_as_it_was(self):
        self.copy()
        version, date = self.next_release()
        latest = self.newest()
        day_before = (datetime.date.fromisoformat(latest.date) - datetime.timedelta(days=1)).isoformat()
        refused = {"not a version": (version.replace(".", "-"), date), "not newer": (latest.version, date),
                   "older": ("0.0.1", date), "not a date": (version, "tomorrow"),
                   "before the last release": (version, day_before)}
        original = self.snapshot()
        for name, (wanted, when) in refused.items():
            with self.subTest(name), self.assertRaises(release.ReleaseError):
                release.bump(self.tree, wanted, when)
            self.assertEqual(self.snapshot(), original, name)

    def test_nothing_to_release_is_refused(self):
        self.copy(unreleased="")
        version, date = self.next_release()
        original = self.snapshot()
        with self.assertRaises(release.ReleaseError) as caught:
            release.bump(self.tree, version, date)
        self.assertIn("nothing under", str(caught.exception))
        self.assertEqual(self.snapshot(), original)


class SyncTest(ScratchCase):
    def test_sync_is_idempotent_and_check_notices_a_stale_file_a_wrong_version_and_a_missing_page(self):
        self.copy()
        release.sync(self.tree)
        self.assertEqual(release.check(self.tree), [])
        self.assertEqual(release.sync(self.tree), [])
        readme = self.tree / "README.md"
        readme.write_text(readme.read_text().replace("Latest release", "Latest"))
        self.assertTrue(any("README.md is out of date" in p for p in release.check(self.tree)))
        release.sync(self.tree)
        makefile, newest = self.tree / "Makefile", self.newest().version
        makefile.write_text(makefile.read_text().replace(f"VERSION = {newest}", "VERSION = 0.0.1"))
        self.assertTrue(any("Makefile says 0.0.1" in p for p in release.check(self.tree)))
        makefile.write_text(makefile.read_text().replace("VERSION = 0.0.1", f"VERSION = {newest}"))
        if HAS_SITE:
            (self.tree / "site" / "changelog.html").unlink()
            self.assertTrue(any("changelog.html is missing" in p for p in release.check(self.tree)))

    def test_unreleased_changes_are_not_published_on_the_website(self):
        self.copy(unreleased="### Added\n\n- A secret plan (#98).\n\n")
        release.sync(self.tree)
        files = ("README.md",) + (("site/index.html", "site/changelog.html") if HAS_SITE else ())
        for rel in files:
            self.assertNotIn("secret plan", (self.tree / rel).read_text(), rel)

    def test_a_file_that_lacks_its_markers_is_reported_by_name(self):
        self.copy()
        for rel, name in (("README.md", "line"), ("site/index.html", "hero")):
            if rel.startswith("site") and not HAS_SITE:
                continue
            with self.subTest(rel):
                path = self.tree / rel
                original = path.read_text()
                path.write_text(original.replace(release.markers(name)[0], ""))
                problems = release.check(self.tree)
                path.write_text(original)
                self.assertEqual(len(problems), 1)
                self.assertIn(f"{rel} needs exactly one", problems[0])


class NotesTest(ScratchCase):
    def test_release_notes_hold_the_entries_the_install_steps_and_the_comparison(self):
        tree = self.copy()
        log = release.load(tree)
        latest = log.latest
        text = release.notes(self.tree)
        self.assertIn("### " + latest.groups[0][0], text)
        self.assertIn(latest.groups[0][1][0], text)
        self.assertIn(f"sudo pacman -U omarchy-game-mode-{latest.version}-1-any.pkg.tar.zst", text)
        self.assertIn(f"omarchy-game-mode-{latest.version}.tar.gz", text)
        self.assertTrue(text.rstrip().endswith(f"Full changelog: {log.links[latest.version]}"))
        self.assertEqual(release.notes(self.tree, latest.version), text)
        with self.assertRaises(release.ReleaseError):
            release.notes(self.tree, "0.0.1")


class CommandLineTest(ScratchCase):
    def run_tool(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = release.main(["--root", str(self.tree), *args])
        return status, out.getvalue(), err.getvalue()

    def test_check_says_ok_or_what_is_wrong_and_a_refused_bump_says_why(self):
        self.copy()
        release.sync(self.tree)
        status, out, _err = self.run_tool("check")
        self.assertEqual((status, out.startswith("ok:")), (0, True))
        (self.tree / "README.md").write_text("no markers\n")
        status, _out, err = self.run_tool("check")
        self.assertEqual(status, 1)
        self.assertIn("README.md needs exactly one", err)
        status, _out, err = self.run_tool("bump", "0.0.1")
        self.assertEqual(status, 1)
        self.assertIn("not newer", err)


class Page(html.parser.HTMLParser):
    """What the tests need to know about a page of the website."""

    def __init__(self, text):
        super().__init__()
        self.ids, self.hrefs, self.nav, self.current, self.problems = [], [], [], [], []
        self.title = self.csp = self.canonical = ""
        self.has_feed = False
        self._in_nav = self._in_link = self._in_title = self._is_current = False
        self._label = ""
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in ("script", "style") or "style" in attrs or any(name.startswith("on") for name in attrs):
            self.problems.append(f"<{tag}> is not allowed by the content security policy")
        if tag == "nav" and attrs.get("class") == "site-nav":
            self._in_nav = True
        if tag == "a" and "href" in attrs:
            self.hrefs.append(attrs["href"])
            if self._in_nav:
                self._in_link, self._label = True, ""
                self._is_current = attrs.get("aria-current") == "page"
        elif tag == "title":
            self._in_title = True
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href", "")
        elif tag == "link" and attrs.get("rel") == "alternate" and attrs.get("type") == "application/atom+xml":
            self.has_feed = True
        elif tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
            self.csp = attrs.get("content", "")

    def handle_endtag(self, tag):
        if tag == "a" and self._in_link:
            self.nav.append(self._label.strip())
            if self._is_current:
                self.current.append(self._label.strip())
            self._in_link = self._is_current = False
        elif tag == "nav":
            self._in_nav = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_link:
            self._label += data
        if self._in_title:
            self.title += data


class RepositoryTest(unittest.TestCase):
    def test_the_version_the_changelog_and_everything_generated_from_them_are_in_step(self):
        self.assertEqual(release.check(ROOT), [])

    def test_the_guides_ask_for_a_changelog_entry_and_describe_the_release_command(self):
        guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("## Changelog and releases", guide)
        self.assertIn("scripts/release.py bump", guide)
        template = ROOT / ".github" / "pull_request_template.md"
        if template.is_file():
            self.assertIn("CHANGELOG.md", template.read_text(encoding="utf-8"))


@unittest.skipUnless(HAS_SITE, "the source archive has no website")
class SiteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = ROOT / "site"
        cls.index_text = (site / "index.html").read_text(encoding="utf-8")
        cls.changelog_text = (site / "changelog.html").read_text(encoding="utf-8")
        cls.pages = {"index.html": Page(cls.index_text), "changelog.html": Page(cls.changelog_text)}
        cls.log = release.load(release.Tree(ROOT))

    def test_neither_page_does_anything_its_content_security_policy_forbids(self):
        for name, page in self.pages.items():
            self.assertEqual(page.problems, [], name)
        self.assertEqual(self.pages["changelog.html"].csp, self.pages["index.html"].csp)
        self.assertIn("default-src 'none'", self.pages["index.html"].csp)

    def test_ids_are_unique_and_every_link_inside_the_site_goes_somewhere(self):
        for name, page in self.pages.items():
            self.assertEqual(len(page.ids), len(set(page.ids)), name)
            for href in page.hrefs:
                if "://" in href:
                    continue
                target, _, fragment = href.partition("#")
                if target:
                    self.assertTrue((ROOT / "site" / target).is_file(), f"{name}: {href}")
                    ids = self.pages[target].ids if target in self.pages else None
                else:
                    ids = page.ids
                if fragment and fragment != "top" and ids is not None:
                    self.assertIn(fragment, ids, f"{name}: {href}")

    def test_the_menu_is_the_same_on_both_pages_and_marks_the_changelog_as_current(self):
        index, changelog = self.pages["index.html"], self.pages["changelog.html"]
        self.assertEqual(changelog.nav, index.nav)
        self.assertIn("What’s new", index.nav)
        self.assertEqual((index.current, changelog.current), ([], ["What’s new"]))

    def test_the_changelog_page_lists_every_release_newest_first_with_its_entries(self):
        text = self.changelog_text
        positions = [text.index(f'id="{release.anchor(r.version)}"') for r in self.log.published]
        self.assertEqual(positions, sorted(positions))
        for published in self.log.published:
            for _group, entries in published.groups:
                for entry in entries:
                    self.assertIn(release.inline(entry), text)
        self.assertNotIn("`", text)
        self.assertNotIn("**", text)

    def test_the_front_page_and_the_sitemap_name_the_latest_release(self):
        latest = self.log.latest
        self.assertIn(f'<span class="release-tag">v{latest.version}</span>', self.index_text)
        self.assertIn(f"<h3>{latest.version}</h3>", self.index_text)
        self.assertIn(f"{latest.version}-1-any.pkg.tar.zst", self.index_text)
        urls = {url.find("{*}loc").text: url.find("{*}lastmod") for url in
                ET.parse(ROOT / "site" / "sitemap.xml").getroot().findall("{*}url")}
        self.assertEqual(set(urls), {release.SITE_URL, release.SITE_URL + "changelog.html"})
        self.assertEqual(urls[release.SITE_URL + "changelog.html"].text, latest.date)

    def test_the_changelog_page_can_be_found_and_followed(self):
        page = self.pages["changelog.html"]
        self.assertEqual(page.canonical, release.SITE_URL + "changelog.html")
        self.assertTrue(page.has_feed)
        self.assertIn("What’s new", page.title)


if __name__ == "__main__":
    unittest.main()
