"""
Floorplan Navigation Graph Editor — Web Server.

Serves the editor UI and floor images, handles save/load of the nav graph JSON.

Usage:
    uv run python server.py
    Then open http://<rpi-ip>:8800 in your browser.
"""

import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("map-editor")

MAP_DIR = Path(__file__).resolve().parent.parent.parent / "dev-console" / "data" / "map"
STATIC_DIR = Path(__file__).resolve().parent / "static"
GRAPH_PATH = MAP_DIR / "nav_graph.json"

FLOOR_FILES = {
    "ground_floor": "ground_floor.png",
    "1st_floor": "1st_floor.png",
    "2nd_floor": "2nd_floor.png",
    "3rd_floor": "3rd_floor.png",
}

app = FastAPI(title="Pepper Nav Graph Editor")

# Serve floor images
app.mount("/maps", StaticFiles(directory=str(MAP_DIR)), name="maps")
# Serve static frontend files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/floors")
async def get_floors():
    return FLOOR_FILES


@app.get("/api/graph")
async def get_graph():
    if GRAPH_PATH.exists():
        data = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        log.info("Graph loaded from %s", GRAPH_PATH)
        return data
    return None


class SaveRequest(BaseModel):
    graph: dict


@app.post("/api/graph")
async def save_graph(req: SaveRequest):
    GRAPH_PATH.write_text(json.dumps(req.graph, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Graph saved to %s (%d bytes)", GRAPH_PATH, GRAPH_PATH.stat().st_size)
    return {"ok": True, "path": str(GRAPH_PATH)}


if __name__ == "__main__":
    import uvicorn
    log.info("Map dir: %s", MAP_DIR)
    log.info("Serving on http://0.0.0.0:8800")
    uvicorn.run(app, host="0.0.0.0", port=8800)
