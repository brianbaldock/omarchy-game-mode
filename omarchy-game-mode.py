"""Entry point that works from a checkout and from the installed data dir."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gaminghome.application import main  # noqa: E402

sys.exit(main())
