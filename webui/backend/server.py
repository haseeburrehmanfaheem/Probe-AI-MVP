"""Probe web UI backend: serves the frontend and runs one agent Session per WebSocket.

Run:  python backend/server.py   ->  http://127.0.0.1:8000
"""
import json, os
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from agent import Session, load_sensors
from board import Board

HERE = Path(__file__).parent
FRONTEND = HERE.parent / "frontend"
CONFIG = json.loads(Path(os.environ.get("BOARD_CONFIG", HERE / "board_config.json")).read_text())
CONFIG["cube"] = json.loads((HERE / CONFIG["board_definition"]).read_text())  # probe cube ports and pins
# PROBE_TRANSPORT=serial PROBE_PORT=/dev/cu.usbmodemXXXX use the real board without editing board_config.json
if os.environ.get("PROBE_TRANSPORT"): CONFIG["transport"]["type"] = os.environ["PROBE_TRANSPORT"]
if os.environ.get("PROBE_PORT"): CONFIG["transport"]["port"] = os.environ["PROBE_PORT"]
load_sensors(CONFIG)  # sensors/<id>/: fails loudly on a bad definition
board = Board(CONFIG)  # one board shared by all tabs; the serial port can only be opened once

app = FastAPI()
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index(): return FileResponse(FRONTEND / "index.html")


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    session = Session(websocket, CONFIG, board)
    try:
        while True: await session.handle(await websocket.receive_json())
    except WebSocketDisconnect:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
