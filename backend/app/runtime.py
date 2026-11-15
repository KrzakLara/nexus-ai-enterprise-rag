"""Request-time access to the active Settings.

The API module (`backend.app.main`) owns a module-level `settings` object that tests and the
evaluation harness replace with `dataclasses.replace(...)` (e.g. a temporary storage directory).
`get_settings()` returns that object when the API is loaded, otherwise the process configuration,
so every request-time code path sees one consistent configuration without import cycles.
"""
from __future__ import annotations

import sys

from .config import Settings
from .config import settings as process_settings


def get_settings() -> Settings:
    main = sys.modules.get("backend.app.main")
    configured = getattr(main, "settings", None) if main is not None else None
    return configured if isinstance(configured, Settings) else process_settings
