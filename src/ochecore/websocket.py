import asyncio

import anyio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ochecore.http import same_origin

router = APIRouter()


@router.websocket("/events")
async def websocket_events(ws: WebSocket):
    await stream_events(ws, raw=False)


@router.websocket("/events/raw")
async def raw_websocket_events(ws: WebSocket):
    await stream_events(ws, raw=True)


async def stream_events(ws: WebSocket, raw: bool):
    scheme = "https" if ws.url.scheme == "wss" else "http"
    if not same_origin(ws.headers.get("origin"), ws.headers.get("host", ""), scheme):
        await ws.close(code=1008)
        return
    await ws.accept()
    runtime = ws.app.state.runtime

    async def wait_disconnect():
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return

    async def send_events(queue):
        while True:
            event = await queue.get()
            payload = event if raw else event.model_dump(mode="json")
            await asyncio.wait_for(ws.send_json(payload), timeout=10)

    with runtime.bus.subscribe(mode="raw" if raw else "normalized") as queue:
        try:
            async with anyio.create_task_group() as group:

                async def sender():
                    try:
                        await send_events(queue)
                    finally:
                        group.cancel_scope.cancel()

                group.start_soon(sender)
                try:
                    await wait_disconnect()
                finally:
                    group.cancel_scope.cancel()
        except* (WebSocketDisconnect, RuntimeError, TimeoutError):
            pass
