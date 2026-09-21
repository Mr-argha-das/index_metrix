#!/usr/bin/env python
"""BOT INDEXER development launcher.

    python run.py                     # host 0.0.0.0, port 8000
    python run.py --port 9000 --reload

In production use:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BOT INDEXER")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="auto-reload (development only)")
    args = parser.parse_args()

    # The app's pydantic-settings layer loads .env automatically.
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
