import sys

if sys.argv[1:2] == ["doctor"]:
    # As in omarchy-game-mode.py. Only this package's own __init__ and this file, which
    # `python -m` loads before any line here runs, can have been cached already.
    sys.dont_write_bytecode = True

from . import doctor  # noqa: E402

if doctor.requested(sys.argv):
    # Diagnostics have to work even when GTK or PyGObject cannot be loaded.
    sys.exit(doctor.main(sys.argv[2:]))

from .application import main  # noqa: E402

sys.exit(main(sys.argv))
