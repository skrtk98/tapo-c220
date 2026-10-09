"""TapoC220 を HTTP から呼ぶだけの薄い WebUI。認証は無いので、信頼できるネットワークの中だけで使うこと。"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import MAIN, SUB, Direction, MediaError, OnvifError, Recording, TalkError, TapoC220
from ..sdcard import SdCard
from ..webrtc import WebRtcSessions

_STATIC = Path(__file__).parent / "static"
_NAME = re.compile(r"^(sd-)?[0-9]{8}-[0-9]{6}(-[0-9]{3})?\.(wav|mp4)$")
_MOVE_TIMEOUT = 5.0             # 押しっぱなしの首振りを、止める指示が届かなくてもカメラ側で止める時間（秒）
_RECORD_MAX_SECONDS = 3600.0    # 止め忘れた録音・録画を終える時間
_SD_EXPORT_MAX_SECONDS = 3600.0  # SD カードから1回に取り出せる長さ（実時間の速さでしか届かない）
_UPLOAD_MAX = 20 * 1024 * 1024  # スピーカーで鳴らす音声の大きさの上限


class Move(BaseModel):
    direction: Direction
    speed: float = Field(0.5, ge=0, le=1)


class Step(BaseModel):
    direction: Direction
    amount: float = Field(0.05, gt=0, le=1)


class Goto(BaseModel):
    pan: float = Field(ge=-1, le=1)
    tilt: float = Field(ge=-1, le=1)


class PresetName(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class Offer(BaseModel):
    sdp: str = Field(max_length=65536)
    type: str = Field("offer", pattern="^offer$")
    stream: str = Field(SUB, pattern="^stream[12]$")


class SdExport(BaseModel):
    start: float = Field(gt=0)                                  # UNIX 時刻（秒）
    seconds: float = Field(gt=0, le=_SD_EXPORT_MAX_SECONDS)


class RecordStart(BaseModel):
    kind: str = Field("audio", pattern="^(audio|video)$")
    stream: str = Field(MAIN, pattern="^stream[12]$")


def create_app(camera: TapoC220 | None = None, record_dir: str | Path | None = None) -> FastAPI:
    directory = Path(record_dir or os.environ.get("TAPO_RECORD_DIR") or "recordings")
    state: dict[str, Recording | None] = {"recording": None}
    export: dict = {"task": None, "name": "", "seconds": 0.0, "done": 0.0, "error": ""}  # SD カードからの取り出し
    playing: set[asyncio.Task] = set()  # スピーカーで鳴らしている（順番待ちを含む）処理
    record_lock = asyncio.Lock()  # 開始と停止を1つずつ行う（同時の開始で ffmpeg を2本起こさない）

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.camera = camera or TapoC220()
        app.state.webrtc = WebRtcSessions(app.state.camera.cfg)
        app.state.sdcard = SdCard(app.state.camera.cfg)
        yield
        if export["task"] is not None:
            export["task"].cancel()
            await asyncio.gather(export["task"], return_exceptions=True)
        await app.state.webrtc.close()
        if state["recording"] is not None:
            try:
                await state["recording"].stop()
            except MediaError:
                pass
        await app.state.camera.aclose()

    app = FastAPI(title="Tapo C220", lifespan=lifespan)

    def cam() -> TapoC220:
        return app.state.camera

    @app.exception_handler(OnvifError)
    @app.exception_handler(MediaError)
    @app.exception_handler(TalkError)
    async def device_error(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=502)

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/info")
    async def info() -> dict:
        data = await cam().info()
        return {"device": data["device"], "profiles": [asdict(p) for p in data["profiles"]],
                "position": asdict(data["position"]), "speaker": bool(cam().cfg.cloud_password)}

    # --- 映像と音声 ---------------------------------------------------------------------------------------------------

    @app.get("/api/snapshot.jpg")
    async def snapshot(stream: str = MAIN) -> Response:
        if stream not in (MAIN, SUB):
            raise HTTPException(400, "stream は stream1 か stream2")
        return Response(await cam().snapshot(stream), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/webrtc/offer")
    async def webrtc_offer(body: Offer) -> dict:
        """WebRTC の合図。ブラウザの offer を受け取り、answer を返す（映像は H.264、音声は G.711 A-law のまま送る）。"""
        answer = await app.state.webrtc.answer(body.sdp, body.type, body.stream)
        return {"sdp": answer.sdp, "type": answer.type}

    @app.get("/live.mp3")
    async def live_audio() -> StreamingResponse:
        return StreamingResponse(cam().live_audio(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    # --- 録音・録画 ---------------------------------------------------------------------------------------------------

    def recording_status() -> dict:
        recording = state["recording"]
        if recording is not None and not recording.running:  # 上限の時間で自然に終わった
            recording = state["recording"] = None
        if recording is None:
            return {"recording": False}
        return {"recording": True, "kind": recording.kind, "name": recording.path.name,
                "seconds": round(time.time() - recording.started, 1)}

    @app.get("/api/record")
    async def record_status() -> dict:
        return recording_status()

    @app.post("/api/record/start")
    async def record_start(body: RecordStart) -> dict:
        async with record_lock:
            if recording_status()["recording"]:
                raise HTTPException(409, "すでに記録している")
            now = time.time()  # ミリ秒までを名前に入れる（同じ秒のうちに録り直しても、前のファイルを上書きしない）
            name = (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
                    + (".wav" if body.kind == "audio" else ".mp4"))
            state["recording"] = await cam().record(directory / name, kind=body.kind, stream=body.stream,
                                                    max_seconds=_RECORD_MAX_SECONDS)
            return recording_status()

    @app.post("/api/record/stop")
    async def record_stop() -> dict:
        async with record_lock:
            recording, state["recording"] = state["recording"], None
            if recording is None:
                raise HTTPException(409, "記録していない")
            path = await recording.stop()
            return {"name": path.name, "bytes": path.stat().st_size}

    def recorded(name: str) -> Path:
        path = directory / name
        if not _NAME.match(name) or not path.is_file():
            raise HTTPException(404, "そのファイルは無い")
        return path

    @app.get("/api/recordings")
    async def recordings() -> list[dict]:
        active = {state["recording"].path.name if state["recording"] is not None else "",
                  export["name"] if export["task"] is not None else ""}
        files = sorted((p for p in directory.glob("*") if _NAME.match(p.name) and p.name not in active),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "bytes": p.stat().st_size} for p in files]

    @app.get("/recordings/{name}")
    async def recording_file(name: str) -> FileResponse:
        return FileResponse(recorded(name))

    @app.delete("/api/recordings/{name}")
    async def recording_delete(name: str) -> dict:
        recorded(name).unlink()
        return {"deleted": name}

    # --- SD カードの録画 ----------------------------------------------------------------------------------------------

    def export_status() -> dict:
        return {"running": export["task"] is not None, "name": export["name"], "seconds": export["seconds"],
                "done": round(export["done"], 1), "error": export["error"]}

    @app.get("/api/sd")
    async def sd_overview() -> dict:
        status, days = await asyncio.gather(app.state.sdcard.status(), app.state.sdcard.days())
        return {"status": status, "days": days}

    @app.get("/api/sd/segments")
    async def sd_segments(date: str) -> list[dict]:
        return [asdict(s) for s in await app.state.sdcard.segments(date)]

    @app.get("/api/sd/export")
    async def sd_export_status() -> dict:
        return export_status()

    @app.post("/api/sd/export")
    async def sd_export(body: SdExport) -> dict:
        """録画の一部を MP4 として recordings に取り出す。実時間の速さでしか届かないので、裏で進めて進み具合を返す。"""
        if export["task"] is not None:
            raise HTTPException(409, "すでに取り出している")
        name = "sd-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(body.start)) + ".mp4"
        export.update(name=name, seconds=body.seconds, done=0.0, error="")

        async def work() -> None:
            try:
                await app.state.sdcard.export(body.start, body.seconds, directory / name,
                                              progress=lambda done: export.update(done=done))
            except asyncio.CancelledError:
                export["error"] = "取り出しを止めた"
            except (TalkError, MediaError) as e:
                export["error"] = str(e)
            finally:
                export["task"] = None

        export["task"] = asyncio.create_task(work())
        return export_status()

    @app.post("/api/sd/export/cancel")
    async def sd_export_cancel() -> dict:
        if export["task"] is not None:
            export["task"].cancel()
        return {"ok": True}

    # --- スピーカー ---------------------------------------------------------------------------------------------------

    @app.post("/api/speaker/play")
    async def speaker_play(file: UploadFile, gain_db: float = 0.0) -> dict:
        """音声ファイルを受け取り、カメラのスピーカーで鳴らす。鳴り終わってから応答する。"""
        if not -30 <= gain_db <= 30:
            raise HTTPException(400, "gain_db は -30〜30")
        data = await file.read(_UPLOAD_MAX + 1)
        if len(data) > _UPLOAD_MAX:
            raise HTTPException(413, "音声ファイルが大きすぎる")
        with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "").suffix[:8]) as tmp:
            tmp.write(data)
            tmp.flush()
            task = asyncio.create_task(cam().play(tmp.name, audio_filter=f"volume={gain_db:.1f}dB" if gain_db else ""))
            playing.add(task)
            try:
                seconds = await task
            except asyncio.CancelledError:
                if not task.cancelled():  # 止められたのは再生ではなく、この要求そのもの（ブラウザが切れた等）
                    task.cancel()
                    raise
                return {"stopped": True}
            finally:
                playing.discard(task)
        return {"seconds": round(seconds, 2)}

    @app.post("/api/speaker/stop")
    async def speaker_stop() -> dict:
        """鳴らしている途中の音声を止める。"""
        for task in playing:
            task.cancel()
        return {"stopped": len(playing)}

    # --- 首振り -------------------------------------------------------------------------------------------------------

    @app.get("/api/ptz")
    async def ptz_position() -> dict:
        return asdict(await cam().position())

    @app.post("/api/ptz/move")
    async def ptz_move(body: Move) -> dict:
        await cam().move(body.direction, body.speed, timeout=_MOVE_TIMEOUT)
        return {"ok": True}

    @app.post("/api/ptz/stop")
    async def ptz_stop() -> dict:
        await cam().stop()
        return asdict(await cam().position())

    @app.post("/api/ptz/step")
    async def ptz_step(body: Step) -> dict:
        await cam().step(body.direction, body.amount)
        return asdict(await cam().position())

    @app.post("/api/ptz/goto")
    async def ptz_goto(body: Goto) -> dict:
        await cam().goto(body.pan, body.tilt)
        return asdict(await cam().position())

    @app.get("/api/ptz/presets")
    async def presets() -> list[dict]:
        return [asdict(p) for p in await cam().presets()]

    @app.post("/api/ptz/presets")
    async def preset_save(body: PresetName) -> dict:
        return {"token": await cam().save_preset(body.name)}

    @app.post("/api/ptz/presets/{token}/goto")
    async def preset_goto(token: str) -> dict:
        await cam().goto_preset(token)
        return {"ok": True}

    @app.delete("/api/ptz/presets/{token}")
    async def preset_remove(token: str) -> dict:
        await cam().remove_preset(token)
        return {"ok": True}

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Tapo C220 の WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス（既定は手元だけ。認証が無いので公開しないこと）")
    parser.add_argument("--port", type=int, default=8220)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
