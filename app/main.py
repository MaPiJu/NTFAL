"""FastAPI dashboard — read-only display of the daily snapshot.

The server only reads cache/snapshot.json (written by run.py); it never
contacts the exchange and never places orders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_APP_DIR = Path(__file__).parent

app = FastAPI(
    title="Elder Triple Screen Scanner",
    description="Read-only daily Triple Screen + Impulse analysis of Hyperliquid perps. "
    "Informational only — not financial advice, places no trades.",
)
app.mount("/static", StaticFiles(directory=_APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_APP_DIR / "templates")


def snapshot_path() -> Path:
    return Path(os.environ.get("SNAPSHOT_PATH", "cache/snapshot.json"))


@app.get("/api/snapshot")
def api_snapshot() -> JSONResponse:
    path = snapshot_path()
    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={"error": f"no snapshot at {path} — run `python run.py` first"},
        )
    return JSONResponse(content=json.loads(path.read_text()))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html")
