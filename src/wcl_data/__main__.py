"""Entrypoint so `python -m wcl_data ...` works."""
from .cli import main

raise SystemExit(main())
