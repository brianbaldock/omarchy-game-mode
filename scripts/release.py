#!/usr/bin/env python3
"""Keep the version, the changelog and everything derived from them in step.

CHANGELOG.md is the one place where a release is described, and gaminghome/__init__.py is the one place that
says which version this is. Everything else that mentions either is generated from them or checked against them:

* the version in Makefile and PKGBUILD,
* the release line and the install steps in README.md,
* the website: the release line, the "What's new" section and the install card on site/index.html, the whole of
  site/changelog.html, and site/sitemap.xml.

Commands (standard library only; run them from anywhere):

    release.py check             report anything that is out of step; exit status 1 when something is
    release.py sync              regenerate the derived files from CHANGELOG.md and the version
    release.py bump X.Y.Z        turn "Unreleased" into release X.Y.Z, set the version everywhere and sync
    release.py notes [X.Y.Z]     print the GitHub release notes of a version (the newest by default)

``check`` runs as part of ``make test``, so a change to the changelog or the version that leaves the website or
the README behind fails the tests. In a source archive, which has no website, only what is there is checked.
"""

from __future__ import annotations

import argparse
import datetime
import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/brianbaldock/omarchy-game-mode"
SITE_URL = "https://brianbaldock.github.io/omarchy-game-mode/"
PACKAGE = "omarchy-game-mode"
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")   # Keep a Changelog's order
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNRELEASED = "Unreleased"

# Where the version is written, how to find it, and how to write it.
VERSION_FILES = (
    ("gaminghome/__init__.py", re.compile(r'^VERSION = "([^"\n]+)"$', re.M), 'VERSION = "{}"'),
    ("Makefile", re.compile(r"^VERSION = (\S+)$", re.M), "VERSION = {}"),
    ("PKGBUILD", re.compile(r"^pkgver=(\S+)$", re.M), "pkgver={}"),
)
PKGREL = re.compile(r"^pkgrel=(\d+)$", re.M)
SEMVER = re.compile(r"\d+\.\d+\.\d+")


class ReleaseError(Exception):
    """Something the person running the tool has to fix; the message says what."""


# ---- the changelog ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Release:
    version: str                                        # UNRELEASED or "1.2.3"
    date: str                                           # "" for UNRELEASED
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()    # (category, entries), in the file's order

    @property
    def released(self) -> bool:
        return self.version != UNRELEASED

    @property
    def count(self) -> int:
        return sum(len(entries) for _category, entries in self.groups)


@dataclass(frozen=True)
class Changelog:
    releases: tuple[Release, ...]                       # UNRELEASED first, then newest to oldest
    links: dict[str, str]

    @property
    def unreleased(self) -> Release:
        return self.releases[0]

    @property
    def published(self) -> tuple[Release, ...]:
        return self.releases[1:]

    @property
    def latest(self) -> Release:
        return self.releases[1]

    def get(self, version: str) -> Release:
        for release in self.published:
            if release.version == version:
                return release
        raise ReleaseError(f"CHANGELOG.md has no release {version}")


def key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def expected_links(versions: list[str]) -> dict[str, str]:
    """The link references CHANGELOG.md has to end with, for the released versions newest first."""
    links = {UNRELEASED: f"{REPO}/compare/v{versions[0]}...HEAD"}
    for version, previous in zip(versions, versions[1:]):
        links[version] = f"{REPO}/compare/v{previous}...v{version}"
    links[versions[-1]] = f"{REPO}/releases/tag/v{versions[-1]}"
    return links


HEADING = re.compile(r"^## \[(Unreleased|\d+\.\d+\.\d+)\](?: - (\d{4}-\d{2}-\d{2}))?$")
GROUP = re.compile(r"^### (\S+)$")
LINK = re.compile(r"^\[([^\]]+)\]: (https://\S+)$")


def parse(text: str) -> Changelog:
    """Read CHANGELOG.md. The grammar is small on purpose, so that what the website shows is what the file says."""
    lines = text.splitlines()
    where = "CHANGELOG.md line {}: {}".format
    first = next((n for n, line in enumerate(lines) if line.strip()), None)
    if first is None or lines[first] != "# Changelog":
        raise ReleaseError("CHANGELOG.md has to start with the line '# Changelog'")
    releases: list[list] = []                           # [version, date, {category: [entries]}]
    links: dict[str, str] = {}
    entry: list[str] | None = None
    category: str | None = None

    def close():
        nonlocal entry
        if entry is not None:
            releases[-1][2][category].append(" ".join(entry))
            entry = None

    for number, line in enumerate(lines[first + 1:], first + 2):
        if not line.strip():
            close()
            continue
        if links and not LINK.match(line):
            raise ReleaseError(where(number, "nothing may follow the link references at the end"))
        if line.startswith("## "):
            close()
            match = HEADING.match(line)
            if not match:
                raise ReleaseError(where(number, "a release is '## [1.2.3] - YYYY-MM-DD', or '## [Unreleased]'"))
            version, date = match.groups()
            if (version == UNRELEASED) != (date is None):
                raise ReleaseError(where(number, "a released version has a date and Unreleased has none"))
            if date:
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    raise ReleaseError(where(number, f"{date} is not a date")) from None
            releases.append([version, date or "", {}])
            category = None
        elif line.startswith("### "):
            close()
            match = GROUP.match(line)
            if not releases or not match or match.group(1) not in CATEGORIES:
                raise ReleaseError(where(number, "a group is one of " + ", ".join(f"'### {c}'" for c in CATEGORIES)))
            category = match.group(1)
            if category in releases[-1][2]:
                raise ReleaseError(where(number, f"'### {category}' appears twice in {releases[-1][0]}"))
            releases[-1][2][category] = []
        elif line.startswith("- "):
            close()
            if category is None:
                raise ReleaseError(where(number, "an entry belongs under a '### Added' style group"))
            entry = [line[2:].strip()]
        elif line.startswith("  ") and entry is not None:
            entry.append(line.strip())
        elif LINK.match(line):
            close()
            label, url = LINK.match(line).groups()
            if label in links:
                raise ReleaseError(where(number, f"[{label}] is defined twice"))
            links[label] = url
        elif not releases:
            continue                                    # the introduction
        else:
            raise ReleaseError(where(number, "expected an entry ('- text'), a group, a release or a link reference"))
    close()
    return validate(Changelog(tuple(Release(version, date, tuple((c, tuple(e)) for c, e in groups.items()))
                                    for version, date, groups in releases), links))


def validate(log: Changelog) -> Changelog:
    if not log.releases or log.releases[0].version != UNRELEASED:
        raise ReleaseError("CHANGELOG.md has to list '## [Unreleased]' first, even when nothing is in it")
    if not log.published:
        raise ReleaseError("CHANGELOG.md lists no release")
    versions = [release.version for release in log.published]
    if versions != sorted(set(versions), key=key, reverse=True):
        raise ReleaseError("CHANGELOG.md has to list each release once, newest first")
    dates = [release.date for release in log.published]
    if dates != sorted(dates, reverse=True):
        raise ReleaseError("a release cannot be dated before an older one")
    for release in log.releases:
        order = [CATEGORIES.index(category) for category, _entries in release.groups]
        if order != sorted(order):
            raise ReleaseError(f"the groups of {release.version} are out of order; use " + ", ".join(CATEGORIES))
        for category, entries in release.groups:
            if not entries:
                raise ReleaseError(f"'### {category}' of {release.version} has no entries")
    for release in log.published:
        if not release.count:
            raise ReleaseError(f"release {release.version} describes nothing; every release lists what changed")
    expected = expected_links(versions)
    for label, url in expected.items():
        if log.links.get(label) != url:
            raise ReleaseError(f"CHANGELOG.md should end with the link reference '[{label}]: {url}'")
    extra = sorted(set(log.links) - set(expected))
    if extra:
        raise ReleaseError("CHANGELOG.md has link references for nothing: " + ", ".join(extra))
    return log


# ---- the files -------------------------------------------------------------------------------------------------

class Tree:
    """The project's files. Changes are held in memory until ``write``, so nothing is half done."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.pending: dict[str, str] = {}

    def exists(self, rel: str) -> bool:
        return rel in self.pending or (self.root / rel).is_file()

    def read(self, rel: str) -> str:
        if rel in self.pending:
            return self.pending[rel]
        try:
            return (self.root / rel).read_text(encoding="utf-8")
        except OSError as error:
            raise ReleaseError(f"cannot read {rel}: {error.strerror or error}") from None

    def put(self, rel: str, text: str) -> None:
        self.pending[rel] = text

    def changed(self) -> list[str]:
        return [rel for rel, text in sorted(self.pending.items())
                if not (self.root / rel).is_file() or (self.root / rel).read_text(encoding="utf-8") != text]

    def write(self) -> list[str]:
        changed = self.changed()
        for rel in changed:
            (self.root / rel).write_text(self.pending[rel], encoding="utf-8")
        return changed


def versions_in(tree: Tree) -> dict[str, str | None]:
    found = {}
    for rel, pattern, _template in VERSION_FILES:
        match = pattern.search(tree.read(rel))
        found[rel] = match.group(1) if match else None
    return found


def package_release(tree: Tree) -> str:
    match = PKGREL.search(tree.read("PKGBUILD"))
    return match.group(1) if match else "1"


def package_file(tree: Tree, version: str) -> str:
    return f"{PACKAGE}-{version}-{package_release(tree)}-any.pkg.tar.zst"


# ---- words and markup ------------------------------------------------------------------------------------------

INLINE = re.compile(r"`(?P<code>[^`\n]+)`|\*\*(?P<bold>[^*\n]+)\*\*|\[(?P<label>[^\]\n]+)\]\((?P<url>https://[^)\s]+)\)"
                    r"|\(#(?P<pull>\d+)\)")
NESTED = re.compile(r"`(?P<code>[^`\n]+)`")       # inside bold only code means anything
E = html.escape


def inline(text: str, nested: bool = False) -> str:
    """An entry as HTML. Only code, bold, https links and pull request numbers mean anything; the rest is text.
    Code may sit inside bold, which is how an entry names a command."""
    out, position = [], 0
    for match in (NESTED if nested else INLINE).finditer(text):
        out.append(E(text[position:match.start()]))
        found = match.groupdict()
        if found.get("code") is not None:
            out.append(f"<code>{E(found['code'])}</code>")
        elif found.get("bold") is not None:
            out.append(f"<strong>{inline(found['bold'], nested=True)}</strong>")
        elif found.get("url") is not None:
            out.append(f'<a href="{E(found["url"])}">{E(found["label"])}</a>')
        else:
            out.append(f'(<a href="{REPO}/pull/{found["pull"]}">#{found["pull"]}</a>)')
        position = match.end()
    out.append(E(text[position:]))
    return "".join(out)


def long_date(date: str) -> str:
    year, month, day = (int(part) for part in date.split("-"))
    return f"{MONTHS[month - 1]} {day}, {year}"


def anchor(version: str) -> str:
    return "v" + version.replace(".", "-")


def tag_url(version: str) -> str:
    return f"{REPO}/releases/tag/v{version}"


def arrow(name: str) -> str:
    return f'<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-{name}"/></svg>'


def groups_html(release: Release, heading: str) -> str:
    sections = []
    for category, entries in release.groups:
        items = "\n".join(f"  <li>{inline(entry)}</li>" for entry in entries)
        sections.append(f'<section class="change-group">\n  <{heading}>{category}</{heading}>\n'
                        f'  <ul class="change-list">\n{indent(items, 2)}\n  </ul>\n</section>')
    return "\n".join(sections)


def indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line else line for line in text.splitlines())


def markers(name: str) -> tuple[str, str]:
    return f"<!-- release:{name}:start -->", f"<!-- release:{name}:end -->"


def replace_region(text: str, name: str, body: str, where: str) -> str:
    """Put ``body`` between the two marker comments of ``name``, indented like the start marker."""
    start, end = markers(name)
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise ReleaseError(f"{where} needs exactly one '{start}' followed by '{end}'")
    first, last = text.index(start), text.index(end)
    pad = text[text.rfind("\n", 0, first) + 1:first]
    if pad.strip():
        raise ReleaseError(f"the marker '{start}' in {where} has to be on a line of its own")
    return text[:first] + start + "\n" + indent(body.strip("\n"), len(pad)) + "\n" + pad + text[last:]


# ---- what is generated -----------------------------------------------------------------------------------------

def readme_release_line(log: Changelog) -> str:
    latest = log.latest
    return (f"**Latest release: [{latest.version}]({tag_url(latest.version)})** ({latest.date}) · "
            f"[What changed](CHANGELOG.md) · [Install it](#install)")


def readme_install(tree: Tree, log: Changelog) -> str:
    version, package = log.latest.version, package_file(tree, log.latest.version)
    return (f"Download `{package}` and `SHA256SUMS` from the\n[{version} release]({tag_url(version)}), then:\n\n"
            f"```bash\nsha256sum -c --ignore-missing SHA256SUMS\nsudo pacman -U {package}\n"
            f"omarchy-game-mode quit      # so the new version starts\n```\n\n"
            f"See the [changelog](CHANGELOG.md) for what changed in each version.")


def site_hero(log: Changelog) -> str:
    latest = log.latest
    return (f'<p class="hero-release"><span class="release-tag">v{latest.version}</span>'
            f'<time datetime="{latest.date}">{long_date(latest.date)}</time>'
            f'<a href="changelog.html">What changed</a>'
            f'<a href="{tag_url(latest.version)}">Release notes{arrow("external")}</a></p>')


def site_whats_new(log: Changelog) -> str:
    latest = log.latest
    return f"""<section id="whats-new" class="section" aria-labelledby="whats-new-title">
  <div class="container">
    <div class="section-head">
      <div>
        <h2 id="whats-new-title">What’s new.</h2>
        <p class="lede">Version {latest.version} is the latest release. The changelog lists every version, newest first: what was added, changed and fixed.</p>
      </div>
      <a class="head-link" href="changelog.html">Full changelog{arrow("arrow")}</a>
    </div>
    <div class="card release-card">
      <div class="release-card-head">
        <h3>{latest.version}</h3>
        <time datetime="{latest.date}">{long_date(latest.date)}</time>
        <span class="badge is-done">Latest</span>
      </div>
{indent(groups_html(latest, "h4"), 6)}
      <p class="note"><a href="{tag_url(latest.version)}">Release notes and downloads on GitHub</a></p>
    </div>
  </div>
</section>"""


def site_install(tree: Tree, log: Changelog) -> str:
    version, package = log.latest.version, package_file(tree, log.latest.version)
    download = f"{REPO}/releases/download/v{version}"
    return f"""<div class="card">
  <h3>Install the latest release</h3>
  <pre class="code" tabindex="0"><code><span class="line">curl -LO {download}/{package}</span><span class="line">curl -LO {download}/SHA256SUMS</span><span class="line">sha256sum -c --ignore-missing SHA256SUMS</span><span class="line">sudo pacman -U {package}</span></code></pre>
  <p class="note">Releases are Arch packages built from a tagged source archive, with checksums. Run <code>omarchy-game-mode quit</code> afterwards so the new version starts, and read <a href="changelog.html">what changed</a> first.</p>
</div>"""


def changelog_page(index: str, log: Changelog) -> str:
    """site/changelog.html. Its head, header and footer come from index.html, so the two pages cannot drift."""
    latest = log.latest
    csp = re.search(r'<meta http-equiv="Content-Security-Policy" content="[^"]*">', index)
    referrer = re.search(r'<meta name="referrer" content="[^"]*">', index)
    if not (csp and referrer and "<body>" in index and '<main id="main"' in index and "<footer" in index):
        raise ReleaseError("site/index.html does not have the head, body and footer the changelog page copies")
    title = "What’s new · Omarchy Game Mode"
    description = (f"Every release of Omarchy Game Mode, newest first: what was added, changed and fixed. "
                   f"The latest version is {latest.version}.")
    url = SITE_URL + "changelog.html"
    top = index[index.index("<body>") + len("<body>"):index.index('<main id="main"')]
    nav_start, nav_end = top.index('<nav class="site-nav"'), top.index("</nav>", top.index('<nav class="site-nav"'))
    nav = top[nav_start:nav_end].replace('href="#', 'href="index.html#')
    link = '<a href="changelog.html">What’s new</a>'
    if nav.count(link) != 1:
        raise ReleaseError("the header of site/index.html has to link to changelog.html as 'What’s new', once")
    nav = nav.replace(link, '<a href="changelog.html" aria-current="page">What’s new</a>')
    top = top[:nav_start] + nav + top[nav_end:]
    top = top.replace('<a class="brand" href="#top">', '<a class="brand" href="index.html">')
    footer = index[index.index("<footer"):index.index("</footer>") + len("</footer>")]
    menu = "\n".join(f'          <li><a href="#{anchor(r.version)}">{r.version}'
                     + (' <span class="badge is-done">Latest</span>' if r is latest else "") + "</a></li>"
                     for r in log.published)
    articles = "\n".join(release_article(log, release, release is latest) for release in log.published)
    return f"""<!doctype html>
<html lang="en" dir="ltr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {csp.group(0)}
  {referrer.group(0)}
  <title>{title}</title>
  <meta name="description" content="{E(description)}">
  <meta name="color-scheme" content="dark">
  <meta name="theme-color" content="#1a1b26">
  <link rel="canonical" href="{url}">
  <link rel="icon" href="assets/mark.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="assets/apple-touch-icon.png">
  <link rel="stylesheet" href="styles.css">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="Omarchy Game Mode">
  <meta property="og:locale" content="en_US">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{E(description)}">
  <meta property="og:url" content="{url}">
  <meta property="og:image" content="{SITE_URL}assets/og-image.png">
  <meta property="og:image:type" content="image/png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="Pixel-art handheld above the line “Make the handheld feel at home.” Omarchy Game Mode, an independent community project.">
  <meta name="twitter:card" content="summary_large_image">
  <link rel="alternate" type="application/atom+xml" title="Omarchy Game Mode releases" href="{REPO}/releases.atom">
</head>
<body>{top}<main id="main" tabindex="-1">
    <section class="section changelog" aria-labelledby="changelog-title">
      <div class="container">
        <div class="section-head">
          <div>
            <h1 id="changelog-title" class="page-title">What’s new.</h1>
            <p class="lede">Every release of Omarchy Game Mode, newest first: what was added, changed and fixed. The latest version is {latest.version}.</p>
          </div>
          <a class="head-link" href="{REPO}/releases">All releases on GitHub{arrow("external")}</a>
        </div>

        <nav class="release-menu" aria-label="Releases">
          <ul>
{menu}
          </ul>
        </nav>

        <div class="releases">
{articles}
        </div>
      </div>
    </section>
  </main>

  {footer}
</body>
</html>
"""


def release_article(log: Changelog, release: Release, latest: bool) -> str:
    version = release.version
    older = log.links[version]
    links = [f'<a href="{tag_url(version)}">Release notes and downloads{arrow("external")}</a>']
    if "/compare/" in older:
        links.append(f'<a href="{older}">All changes{arrow("external")}</a>')
    badge = '\n            <span class="badge is-done">Latest</span>' if latest else ""
    return f"""          <article class="release" id="{anchor(version)}" aria-labelledby="{anchor(version)}-title">
            <header class="release-head">
              <h2 id="{anchor(version)}-title">{version}</h2>{badge}
            </header>
            <p class="release-meta"><time datetime="{release.date}">{long_date(release.date)}</time>{"".join(links)}</p>
{indent(groups_html(release, "h3"), 12)}
          </article>"""


def sitemap(log: Changelog) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{SITE_URL}</loc>
  </url>
  <url>
    <loc>{SITE_URL}changelog.html</loc>
    <lastmod>{log.latest.date}</lastmod>
  </url>
</urlset>
"""


def derived(tree: Tree, log: Changelog) -> dict[str, str]:
    """What every generated file has to say, as whole files. Files that are not there (a source archive has no
    website) are left out."""
    out = {}
    readme = tree.read("README.md")
    readme = replace_region(readme, "line", readme_release_line(log), "README.md")
    readme = replace_region(readme, "install", readme_install(tree, log), "README.md")
    out["README.md"] = readme
    if tree.exists("site/index.html"):
        index = tree.read("site/index.html")
        for name, body in (("hero", site_hero(log)), ("whatsnew", site_whats_new(log)),
                           ("install", site_install(tree, log))):
            index = replace_region(index, name, body, "site/index.html")
        out["site/index.html"] = index
        out["site/changelog.html"] = changelog_page(index, log)
        out["site/sitemap.xml"] = sitemap(log)
    return out


# ---- the commands ----------------------------------------------------------------------------------------------

def load(tree: Tree) -> Changelog:
    return parse(tree.read("CHANGELOG.md"))


def check(root: Path = ROOT) -> list[str]:
    """Everything that is out of step, as sentences. Empty when all is well."""
    tree, problems = Tree(root), []
    try:
        log = load(tree)
        found = versions_in(tree)
        newest = log.latest.version
        for rel, version in found.items():
            if version is None:
                problems.append(f"{rel} does not say which version this is")
            elif version != newest:
                problems.append(f"{rel} says {version}, but the newest release in CHANGELOG.md is {newest}")
        for rel, text in derived(tree, log).items():
            if not tree.exists(rel):
                problems.append(f"{rel} is missing: run python3 scripts/release.py sync")
            elif tree.read(rel) != text:
                problems.append(f"{rel} is out of date: run python3 scripts/release.py sync")
    except ReleaseError as error:
        problems.append(str(error))
    return problems


def sync(root: Path = ROOT) -> list[str]:
    tree = Tree(root)
    for rel, text in derived(tree, load(tree)).items():
        tree.put(rel, text)
    return tree.write()


def bump(root: Path, version: str, date: str) -> list[str]:
    """Release ``version``: Unreleased becomes it, the version is set everywhere and the derived files follow."""
    if not SEMVER.fullmatch(version):
        raise ReleaseError(f"{version} is not a version like 1.2.3")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ReleaseError(f"{date} is not a date like 2026-12-31") from None
    tree = Tree(root)
    log = load(tree)
    newest = log.latest
    if key(version) <= key(newest.version):
        raise ReleaseError(f"{version} is not newer than {newest.version}, the latest release")
    if date < newest.date:
        raise ReleaseError(f"{date} is before {newest.date}, the date of {newest.version}")
    if not log.unreleased.count:
        raise ReleaseError("there is nothing under '## [Unreleased]' in CHANGELOG.md to release")
    text = tree.read("CHANGELOG.md")
    heading = f"## [{UNRELEASED}]"
    old_unreleased = f"[{UNRELEASED}]: {log.links[UNRELEASED]}"
    if text.count(heading + "\n") != 1 or text.count(old_unreleased) != 1:
        raise ReleaseError("CHANGELOG.md does not have the Unreleased heading and link in the expected form")
    text = text.replace(heading + "\n", f"{heading}\n\n## [{version}] - {date}\n", 1)
    text = text.replace(old_unreleased, f"[{UNRELEASED}]: {REPO}/compare/v{version}...HEAD\n"
                        f"[{version}]: {REPO}/compare/v{newest.version}...v{version}", 1)
    tree.put("CHANGELOG.md", text)
    for rel, pattern, template in VERSION_FILES:
        original = tree.read(rel)
        if len(pattern.findall(original)) != 1:
            raise ReleaseError(f"cannot find the version in {rel}")
        tree.put(rel, pattern.sub(lambda _m: template.format(version), original, count=1))
    pkgbuild = tree.read("PKGBUILD")
    tree.put("PKGBUILD", PKGREL.sub("pkgrel=1", pkgbuild, count=1))
    for rel, derived_text in derived(tree, load(tree)).items():
        tree.put(rel, derived_text)
    return tree.write()


def notes(root: Path, version: str | None = None) -> str:
    """The GitHub release notes of a version: its changelog entries, how to install it and where to compare."""
    tree = Tree(root)
    log = load(tree)
    release = log.get(version) if version else log.latest
    version = release.version
    package = package_file(tree, version)
    lines = []
    for category, entries in release.groups:
        lines += [f"### {category}", "", *(f"- {entry}" for entry in entries), ""]
    lines += [
        "## Install", "",
        f"Arch and Omarchy: download `{package}` and run `sudo pacman -U {package}`. "
        "Then run `omarchy-game-mode quit` so the new version starts.", "",
        f"From source: extract `{PACKAGE}-{version}.tar.gz` and run `sudo make install PREFIX=/usr` "
        "(Python 3.11 or newer, python-gobject, gtk4).", "",
        "Check the downloads with `sha256sum -c SHA256SUMS`. `make dist` builds the tarball with sorted entries and "
        "fixed timestamps and ownership, so it can be rebuilt from the tag and compared.", "",
        f"Full changelog: {log.links[version]}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], prog="release.py")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="report what is out of step")
    commands.add_parser("sync", help="regenerate the derived files")
    bumping = commands.add_parser("bump", help="release the Unreleased changes as a new version")
    bumping.add_argument("version")
    bumping.add_argument("--date", default=datetime.date.today().isoformat(), help="release date (default: today)")
    printing = commands.add_parser("notes", help="print the GitHub release notes")
    printing.add_argument("version", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            problems = check(args.root)
            for problem in problems:
                print(problem, file=sys.stderr)
            if not problems:
                print("ok: the version, the changelog and what is generated from them are in step")
            return 1 if problems else 0
        if args.command == "sync":
            changed = sync(args.root)
            print("updated: " + ", ".join(changed) if changed else "nothing to update")
        elif args.command == "bump":
            changed = bump(args.root, args.version, args.date)
            print("updated: " + ", ".join(changed))
            print(f"next: review the diff, open a pull request titled 'Release {args.version}', then after it is "
                  f"merged tag v{args.version}, build the artifacts and publish them (see CONTRIBUTING.md).")
        else:
            print(notes(args.root, args.version), end="")
    except ReleaseError as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
