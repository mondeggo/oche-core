"""Peschi voice catalogue and local, offline sound packs."""

import asyncio
import csv
import io
import json
import random
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import httpx

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.storage import write_private_json

CATALOGUE = json.loads(Path(__file__).with_name("voices.json").read_text(encoding="utf-8"))
VOICES = {voice["id"]: voice for voice in CATALOGUE}
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
MAX_FILES = 30000


def unpack_pack(archive: Path, destination: Path, voice_id: str) -> dict:
    """Read the provider's CSV and optional nested ZIP without extracting archive paths."""
    with zipfile.ZipFile(archive) as outer:
        if len(outer.infolist()) > MAX_FILES:
            raise ValueError("Voice archive contains too many files.")
        if sum(item.file_size for item in outer.infolist()) > MAX_EXPANDED:
            raise ValueError("Voice archive is too large when expanded.")
        csv_files = [n for n in outer.namelist() if n.lower().endswith(".csv")]
        nested = [n for n in outer.namelist() if n.lower().endswith(".zip")]
        if len(csv_files) != 1 or len(nested) > 1:
            raise ValueError("Expected one voice CSV and at most one audio ZIP.")
        if outer.getinfo(csv_files[0]).file_size > 8 * 1024 * 1024:
            raise ValueError("Voice CSV is too large.")
        rows = list(
            csv.reader(io.StringIO(outer.read(csv_files[0]).decode("utf-8-sig")), delimiter=";")
        )
        if nested:
            with zipfile.ZipFile(io.BytesIO(outer.read(nested[0]))) as sounds:
                return _index_sounds(sounds, rows, destination, voice_id)
        return _index_sounds(outer, rows, destination, voice_id)


def _index_sounds(sounds: zipfile.ZipFile, rows: list, destination: Path, voice_id: str) -> dict:
    entries = sounds.infolist()
    if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_EXPANDED:
        raise ValueError("Voice archive exceeds the installation limit.")
    audio = sorted(
        (item for item in entries if Path(item.filename).suffix.lower() in {".mp3", ".wav"}),
        key=lambda item: item.filename,
    )
    if not audio or len(audio) != len(rows):
        raise ValueError("The audio file count does not match the voice CSV.")
    index: dict[str, list[str]] = {}
    for number, (item, row) in enumerate(zip(audio, rows, strict=True)):
        if item.file_size > 16 * 1024 * 1024 or item.file_size == 0:
            raise ValueError("Invalid audio clip size.")
        cells = [cell.strip().lower() for cell in row if cell.strip()]
        keys = cells[1:] if len(cells) > 1 else cells
        filename = f"{number:05d}{Path(item.filename).suffix.lower()}"
        # Ignore paths in the archive, including absolute paths and symlink attributes.
        (destination / filename).write_bytes(sounds.read(item))
        for key in keys:
            key = re.sub(r"\+\d+$", "", key)
            index.setdefault(key, []).append(filename)
    if not index:
        raise ValueError("The voice CSV contains no sound keys.")
    manifest = {"voice_id": voice_id, "clips": len(audio), "sounds": index}
    write_private_json(destination / "index.json", manifest)
    return manifest


class VoiceLibrary:
    def __init__(self, directory: Path, http: httpx.AsyncClient):
        self.directory, self.http = directory, http
        self.download: dict = {"state": "idle", "voice_id": None, "bytes": 0, "error": None}
        self.task: asyncio.Task | None = None
        self.cache: dict[str, dict] = {}

    def catalogue(self) -> list[dict]:
        return [{**voice, "installed": self.installed(voice["id"])} for voice in CATALOGUE]

    def installed(self, voice_id: str) -> bool:
        return voice_id in VOICES and (self.directory / voice_id / "index.json").is_file()

    def manifest(self, voice_id: str) -> dict:
        if not self.installed(voice_id):
            raise ConnectionProblem("Install the selected voice first.")
        if voice_id not in self.cache:
            try:
                manifest = json.loads(
                    (self.directory / voice_id / "index.json").read_text(encoding="utf-8")
                )
                sounds = manifest.get("sounds") if isinstance(manifest, dict) else None
                if (
                    not isinstance(sounds, dict)
                    or not sounds
                    or any(
                        not isinstance(key, str)
                        or not isinstance(clips, list)
                        or not clips
                        or any(
                            not isinstance(clip, str) or not re.fullmatch(r"\d{5}\.(mp3|wav)", clip)
                            for clip in clips
                        )
                        for key, clips in sounds.items()
                    )
                ):
                    raise ValueError("Invalid voice index")
                self.cache[voice_id] = manifest
            except (OSError, ValueError) as exc:
                raise ConnectionProblem("The installed voice index cannot be read.") from exc
        return self.cache[voice_id]

    def resolve(self, voice_id: str, alternatives: list[str]) -> tuple[str, str] | None:
        sounds = self.manifest(voice_id)["sounds"]
        for key in alternatives:
            clips = sounds.get(key.lower())
            if clips:
                return key, random.choice(clips)
        return None

    def clip_path(self, voice_id: str, clip: str) -> Path:
        if not self.installed(voice_id) or not re.fullmatch(r"\d{5}\.(mp3|wav)", clip):
            raise ConnectionProblem("Unknown voice clip.")
        path = self.directory / voice_id / clip
        if not path.is_file():
            raise ConnectionProblem("Voice clip is missing.")
        return path

    def install(self, voice_id: str) -> dict:
        if voice_id not in VOICES:
            raise ConnectionProblem("Choose a voice from the catalogue.")
        if self.task and not self.task.done():
            if self.download["voice_id"] != voice_id:
                raise ConnectionProblem("A voice is already being installed.")
        elif self.installed(voice_id):
            return {"state": "installed", "voice_id": voice_id}
        else:
            self.download = {
                "state": "downloading",
                "voice_id": voice_id,
                "bytes": 0,
                "total": None,
                "error": None,
            }
            self.task = asyncio.create_task(self._install(voice_id), name="caller-voice-install")
        return dict(self.download)

    async def _install(self, voice_id: str) -> None:
        stage = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".install-", dir=self.directory))
            archive = stage / "download.zip"
            async with self.http.stream(
                "GET", VOICES[voice_id]["url"], timeout=60, follow_redirects=False
            ) as response:
                response.raise_for_status()
                size = int(response.headers.get("content-length", 0))
                if size > MAX_DOWNLOAD:
                    raise ValueError("Voice download exceeds 256 MiB.")
                self.download["total"] = size or None
                with archive.open("wb") as output:
                    async for chunk in response.aiter_bytes(65536):
                        self.download["bytes"] += len(chunk)
                        if self.download["bytes"] > MAX_DOWNLOAD:
                            raise ValueError("Voice download exceeds 256 MiB.")
                        output.write(chunk)
            self.download["state"] = "installing"
            pack = stage / "pack"
            pack.mkdir()
            # Shield the worker so shutdown waits before cleaning its staging directory.
            worker = asyncio.create_task(asyncio.to_thread(unpack_pack, archive, pack, voice_id))
            try:
                manifest = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            pack.rename(self.directory / voice_id)
            self.cache[voice_id] = manifest
            self.download["state"] = "installed"
        except asyncio.CancelledError:
            self.download["state"] = "cancelled"
            raise
        except (
            OSError,
            ValueError,
            httpx.HTTPError,
            zipfile.BadZipFile,
            RuntimeError,
            ConnectionProblem,
        ):
            self.download.update(
                state="error",
                error="Voice installation failed. Check the "
                "connection, free disk space and provider pack; then retry.",
            )
        finally:
            if stage is not None:
                # stage is created above under this library; never remove caller-supplied paths.
                await asyncio.to_thread(shutil.rmtree, stage, True)

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
