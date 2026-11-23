"""`omarchy-game-mode doctor`: read-only streaming, controller and system diagnostics.

Ownership and safety rules for everything in this package:

- Python standard library only, and never PyGObject/GTK: the doctor has to work
  over SSH, in CI and when the graphical stack itself is what is broken.
- Read-only. It never asks for root, never installs or configures anything, never
  signs in and never opens browser profiles, cookies, keyrings or token stores.
- Every subprocess is an argv list with a timeout and a bounded output; every
  file read is bounded; every socket has a timeout.
- Output is English only and redacted by default (see `redact.py`).

Importing this package is free: nothing is loaded until `cli.main` runs.
"""
