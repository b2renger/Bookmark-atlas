"""Entry point of the bundled backend.

The desktop app runs this with the bundled Python in isolated mode
(``python -I -u run_atlas.py ...``), so nothing from the user's own Python
setup can interfere. Isolated mode leaves this folder off the import path,
which is why the path to the ``atlas`` package is added here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from atlas.cli import main  # noqa: E402

raise SystemExit(main())
