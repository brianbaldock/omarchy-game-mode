import sys

from . import doctor

if doctor.requested(sys.argv):
    # Diagnostics have to work even when GTK or PyGObject cannot be loaded.
    sys.exit(doctor.main(sys.argv[2:]))

from .application import main  # noqa: E402

sys.exit(main(sys.argv))
