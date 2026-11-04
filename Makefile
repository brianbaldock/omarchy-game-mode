PREFIX ?= /usr
DESTDIR ?=
BINDIR = $(PREFIX)/bin
PKGDATADIR = $(PREFIX)/share/omarchy-game-mode
DATADIR = $(PREFIX)/share
VERSION = 0.1.0
APP_ID = io.github.brianbaldock.OmarchyGameMode
LEGACY_APP_ID = io.github.omarchy.GameMode
PYTHON ?= python3

.PHONY: all test check install uninstall dist clean

all:

test check:
	$(PYTHON) -m py_compile omarchy-game-mode.py gaminghome/*.py
	$(PYTHON) -m unittest discover -s tests

install:
	install -d "$(DESTDIR)$(BINDIR)" "$(DESTDIR)$(PKGDATADIR)/gaminghome" "$(DESTDIR)$(PKGDATADIR)/data/locales" "$(DESTDIR)$(PKGDATADIR)/data/icons"
	sed -e 's|@BINDIR@|$(BINDIR)|g' -e 's|@PKGDATADIR@|$(PKGDATADIR)|g' bin/omarchy-game-mode.in > "$(DESTDIR)$(BINDIR)/omarchy-game-mode"
	chmod 755 "$(DESTDIR)$(BINDIR)/omarchy-game-mode"
	install -m 644 omarchy-game-mode.py "$(DESTDIR)$(PKGDATADIR)/"
	install -m 644 gaminghome/*.py "$(DESTDIR)$(PKGDATADIR)/gaminghome/"
	install -m 644 data/catalog.json "$(DESTDIR)$(PKGDATADIR)/data/"
	install -m 644 data/locales/*.json "$(DESTDIR)$(PKGDATADIR)/data/locales/"
	install -m 644 data/icons/*.svg "$(DESTDIR)$(PKGDATADIR)/data/icons/"
	install -Dm 644 data/$(APP_ID).desktop "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop"
	install -Dm 644 data/icons/$(APP_ID).svg "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg"
	rm -f "$(DESTDIR)$(DATADIR)/applications/$(LEGACY_APP_ID).desktop" "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(LEGACY_APP_ID).svg" "$(DESTDIR)$(PKGDATADIR)/data/icons/$(LEGACY_APP_ID).svg"
	install -Dm 644 LICENSE "$(DESTDIR)$(DATADIR)/licenses/omarchy-game-mode/LICENSE"

uninstall:
	rm -f "$(DESTDIR)$(BINDIR)/omarchy-game-mode"
	rm -rf "$(DESTDIR)$(PKGDATADIR)"
	rm -f "$(DESTDIR)$(DATADIR)/applications/$(APP_ID).desktop" "$(DESTDIR)$(DATADIR)/applications/$(LEGACY_APP_ID).desktop"
	rm -f "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(APP_ID).svg" "$(DESTDIR)$(DATADIR)/icons/hicolor/scalable/apps/$(LEGACY_APP_ID).svg"
	rm -rf "$(DESTDIR)$(DATADIR)/licenses/omarchy-game-mode"

dist:
	archive=$$(mktemp /tmp/omarchy-game-mode.XXXXXX); trap 'rm -f -- "$$archive"' EXIT; \
	tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
	  --exclude-vcs --exclude=__pycache__ --exclude='*.tar.gz' --exclude='*.pkg.tar.*' --exclude=site --exclude=pkg --exclude=src --exclude=tests/scratch \
	  --transform 's|^\.|omarchy-game-mode-$(VERSION)|' -czf "$$archive" . && \
	mv -- "$$archive" omarchy-game-mode-$(VERSION).tar.gz

clean:
	rm -rf -- __pycache__ gaminghome/__pycache__ tests/__pycache__
	rm -f -- omarchy-game-mode-$(VERSION).tar.gz
