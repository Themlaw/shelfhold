"""Throwaway: FastAPI serving the spike build (uses the real backend factory)."""
import sys
from pathlib import Path

import uvicorn

from shelfhold.interfaces.http_api.app import create_http_app


class H:
    def migration_progress(self):
        return None

    def readiness(self):
        return {}


app = create_http_app(health=H(), on_startup=lambda: None, version="0.0.0", frontend_dir=Path(sys.argv[1]))
uvicorn.run(app, port=8765, log_level="warning")
