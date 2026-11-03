"""Starts Telecomando: python -m telecomando, or python path/to/telecomando/__main__.py"""

import sys
from pathlib import Path

if not __package__:
    # Started as a plain script (start at login, packaged program): make the package importable.
    here = Path(__file__).resolve().parent
    if sys.path and Path(sys.path[0]).resolve() == here:
        sys.path[0] = str(here.parent)
    else:
        sys.path.insert(0, str(here.parent))

from telecomando.app import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
