"""Entry point for ``python scripts/db-migrate ...``; see db_migrate.py."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_migrate import main  # noqa: E402

sys.exit(main())
