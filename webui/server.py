"""Probe web UI: serves the single-page app and runs one agent Session per WebSocket.

Run from this directory:  python server.py   ->  http://127.0.0.1:8000
"""
import json, os
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from agent import Session
from board import Board

HERE = Path(__file__).parent
CONFIG = json.loads(Path(os.environ.get("BOARD_CONFIG", HERE / "board_config.json")).read_text())
board = Board(CONFIG)  # one board shared by all tabs; the serial port can only be opened once

app = FastAPI()
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
def index(): return FileResponse(HERE / "static" / "index.html")


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
