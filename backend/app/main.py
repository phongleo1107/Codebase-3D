"""ASGI entry point: ``uvicorn app.main:app``.

Separate from `app/api/app.py` so that building an application is free of side
effects and configuring the process is not. `configure_logging` replaces the
root logger's handlers, which is right for a server and wrong inside a test —
so it happens here, where only a real run reaches it.
"""

from app.api.app import create_app
from app.logging_setup import configure_logging

configure_logging()

app = create_app()
