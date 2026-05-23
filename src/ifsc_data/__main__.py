"""Entrypoint so `python -m ifsc_data ...` works."""
from .cli import main

raise SystemExit(main())
