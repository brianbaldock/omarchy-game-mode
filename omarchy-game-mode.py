"""Entry point that works from a checkout and from the installed data dir."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gaminghome import doctor  # noqa: E402

if doctor.requested(sys.argv):
    # Diagnostics have to work even when GTK or PyGObject cannot be loaded.
    sys.exit(doctor.main(sys.argv[2:]))

from gaminghome.application import main  # noqa: E402

sys.exit(main())
