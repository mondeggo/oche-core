"""Application profiles with one atomic store for lighting and Caller settings."""

import asyncio
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.integrations.caller.service import Caller, CallerConfig
from ochecore.integrations.wled.service import WLED, WLEDConfig
from ochecore.storage import write_private_json


class ProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: str = Field(default_factory=lambda: uuid4().hex)
    wled: WLEDConfig
    callers: dict[str, CallerConfig]

    @model_validator(mode="after")
    def matching_profiles(self):
        if set(self.callers) != {profile.id for profile in self.wled.profiles}:
            raise ValueError("Every profile must have matching lighting and Caller settings")
        return self


class Profiles:
    def __init__(self, path: Path, wled: WLED, caller: Caller):
        self.path, self.wled, self.caller = path, wled, caller
        if path.exists():
            try:
                self.data = ProfileData.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, ValidationError) as exc:
                # Never fall back to old backups and overwrite newer settings.
                raise ConnectionProblem(
                    "Cannot read profiles.json. Restore a valid backup before starting OcheCore."
                ) from exc
            self.wled.config = self.data.wled.model_copy(deep=True)
            self.caller.config = self.data.callers[self.data.wled.active_profile].model_copy(
                deep=True
            )
            self.wled.error = self.caller.error = None
        else:
            # Existing files remain untouched backups. The first edit saves this migration.
            self.data = ProfileData(
                wled=wled.config.model_copy(deep=True),
                callers={
                    profile.id: caller.config.model_copy(deep=True)
                    for profile in wled.config.profiles
                },
            )

    @property
    def revision(self) -> str:
        return f'"{self.data.revision}"'

    def summary(self) -> dict:
        return {
            "active_profile": self.data.wled.active_profile,
            "profiles": [{"id": p.id, "name": p.name} for p in self.data.wled.profiles],
        }

    async def _commit(self, data: ProfileData, *, update_caller: bool = False) -> None:
        data = ProfileData.model_validate(data.model_dump())
        WLED.sync_profiles(data.wled)
        caller = data.callers[data.wled.active_profile]
        if update_caller:
            # A blocked download switch must leave both lighting and persistence unchanged.
            self.caller.library.check_selection(caller.voice)
        data.revision = uuid4().hex
        write_private_json(self.path, data.model_dump(mode="json"))
        previous = self.data
        update_wled = data.wled != previous.wled or self.wled.error is not None
        try:
            if update_wled:
                await self.wled.configure(data.wled, persist=False)
            if update_caller:
                await self.caller.configure(caller.model_copy(deep=True), persist=False)
        except (Exception, asyncio.CancelledError):

            async def restore_previous() -> None:
                write_private_json(self.path, previous.model_dump(mode="json"))
                if update_wled:
                    await self.wled.configure(previous.wled, persist=False)
                if update_caller:
                    await self.caller.configure(
                        previous.callers[previous.wled.active_profile].model_copy(deep=True),
                        persist=False,
                    )

            # Finish rollback before releasing the caller's lock, even if cancelled again.
            restore = asyncio.create_task(restore_previous(), name="profiles-rollback")
            while not restore.done():
                try:
                    await asyncio.shield(restore)
                except asyncio.CancelledError:
                    pass
            restore.result()
            raise
        self.data = data

    async def configure_wled(self, config: WLEDConfig) -> None:
        data = self.data.model_copy(deep=True)
        data.wled = config.model_copy(deep=True)
        data.callers = {
            profile.id: data.callers.get(profile.id, self.caller.config).model_copy(deep=True)
            for profile in config.profiles
        }
        await self._commit(
            data, update_caller=config.active_profile != self.data.wled.active_profile
        )

    async def configure_caller(self, config: CallerConfig) -> None:
        data = self.data.model_copy(deep=True)
        data.callers[data.wled.active_profile] = config.model_copy(deep=True)
        await self._commit(data, update_caller=True)

    async def create(self, name: str, source: Literal["current", "blank"] = "current") -> dict:
        data = self.data.model_copy(deep=True)
        data.wled = self.wled.create_profile_config(name, source)
        data.callers[data.wled.active_profile] = (
            CallerConfig() if source == "blank" else self.caller.config.model_copy(deep=True)
        )
        await self._commit(data, update_caller=True)
        return self.summary()

    async def select(self, profile_id: str) -> dict:
        data = self.data.model_copy(deep=True)
        data.wled = self.wled.select_profile_config(profile_id)
        await self._commit(data, update_caller=True)
        return self.summary()

    async def delete(self, profile_id: str) -> dict:
        data = self.data.model_copy(deep=True)
        data.wled = self.wled.delete_profile_config(profile_id)
        del data.callers[profile_id]
        await self._commit(
            data, update_caller=data.wled.active_profile != self.data.wled.active_profile
        )
        return self.summary()
