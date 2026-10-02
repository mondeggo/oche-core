import asyncio

import httpx

from ochecore.autodarts.auth import DeviceAuth
from ochecore.autodarts.cloud import CloudConnection
from ochecore.caller import Caller
from ochecore.config import ConnectionConfig, Settings
from ochecore.events import EventBus, RawEventRecorder
from ochecore.storage import write_private_json
from ochecore.wled import WLED


class Runtime:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self.debug = RawEventRecorder(settings.data_dir / "debug")
        self.bus = EventBus(recorder=self.debug)
        self.config = settings.connection()
        self.tasks: list[asyncio.Task] = []
        self.lock = asyncio.Lock()
        self._create_connections()
        self.wled = WLED(settings.data_dir / "wled.json", http, self.bus, self.game_state)
        self.caller = Caller(settings.data_dir, http, self.bus, self.game_state)

    def game_state(self) -> dict:
        return self.cloud.game_state()

    def _create_connections(self) -> None:
        self.auth = DeviceAuth(
            self.http,
            self.settings.api_base_url,
            self.config.client_id,
            self.settings.data_dir / "tokens.json",
        )
        self.cloud = CloudConnection(
            self.http, self.auth, self.config.board_id, self.bus, self.settings.reconcile_interval
        )

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self.cloud.run(), name="autodarts-cloud"),
        ]
        self.wled.start()
        self.caller.start()

    async def close(self, *, stop_debug: bool = True) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.wled.close()
        await self.caller.close()
        await self.auth.close()
        if stop_debug:
            await self.debug.stop()

    async def configure(self, config: ConnectionConfig) -> None:
        async with self.lock:
            write_private_json(self.settings.data_dir / "connection.json", config.model_dump())
            await self.close(stop_debug=False)
            if config.client_id != self.config.client_id:
                await self.auth.forget()
            self.config = config
            self._create_connections()
            self.bus.clear()
            self.start()

    async def logout(self) -> None:
        async with self.lock:
            await self.close(stop_debug=False)
            await self.auth.forget()
            self.bus.clear()
            self._create_connections()
            self.start()

    def status(self) -> dict:
        return {
            "auth": self.auth.status(),
            "cloud": self.cloud.status(),
            "events": {
                "received": self.bus.raw_received,
                "subscribers": len(self.bus.normalized_queues),
                "raw_subscribers": len(self.bus.raw_queues),
                "dropped_deliveries": self.bus.dropped,
                "raw_dropped_deliveries": self.bus.raw_dropped,
                "normalized": self.cloud.normalizer.emitted,
                "invalid_match_states": self.cloud.normalizer.invalid_states,
                "debug": self.debug.status(),
            },
        }
