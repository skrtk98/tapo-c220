"""tapo_c220 ライブラリの使用例：TapoC220 を HTTP から呼ぶだけの薄い WebUI。

リポジトリの直下で `uv run --package tapo-c220-webui examples/webui/app.py` として起こす（`.env` と `recordings/` は、起こした場所のものを使う）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import ipaddress
import os
import re
import secrets
import tempfile
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from tapo_c220 import MAIN, SUB, Direction, MediaError, OnvifError, PlaybackInterrupted, Recording, StreamError, TapoC220
from tapo_c220.settings import DayNight, Detection
from tapo_c220.tapo.api import TapoApiError

from webrtc import SdPlayer, WebRtcSessions

_STATIC = Path(__file__).parent / "static"
_NAME = re.compile(r"^(sd-)?[0-9]{8}-[0-9]{6}(-[0-9]{3})?\.(wav|mp4)$")
_MOVE_TIMEOUT = 5.0             # 押しっぱなしの首振りを、止める指示が届かなくてもカメラ側で止める時間（秒）
_RECORD_MAX_SECONDS = 3600.0    # 止め忘れた録音・録画を終える時間
_SD_EXPORT_MAX_SECONDS = 3600.0  # SD カードから1回に取り出せる長さ（実時間の速さでしか届かない）
_UPLOAD_MAX = 20 * 1024 * 1024  # スピーカーで鳴らす音声の大きさの上限
_EVENT_RETRY = 10.0             # 検知イベントの購読に失敗したあと、次に試すまでの時間（秒）


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


class SdPlay(BaseModel):
    sdp: str = Field(max_length=65536)
    start: float = Field(gt=0)  # UNIX 時刻（秒）


class SdExport(BaseModel):
    start: float = Field(gt=0)                                  # UNIX 時刻（秒）
    seconds: float = Field(gt=0, le=_SD_EXPORT_MAX_SECONDS)


class Siren(BaseModel):
    seconds: float = Field(10.0, gt=0, le=60)


class Image(BaseModel):
    brightness: int | None = Field(None, ge=0, le=100)
    contrast: int | None = Field(None, ge=0, le=100)
    saturation: int | None = Field(None, ge=0, le=100)
    sharpness: int | None = Field(None, ge=0, le=100)


class DayNightMode(BaseModel):
    mode: DayNight


class DetectionSwitch(BaseModel):
    kind: Detection
    enabled: bool


class RecordStart(BaseModel):
    kind: str = Field("audio", pattern="^(audio|video)$")
    stream: str = Field(MAIN, pattern="^stream[12]$")


def create_app(camera: TapoC220 | None = None, record_dir: str | Path | None = None,
               password: str | None = None, allowed_hosts: list[str] | None = None) -> FastAPI:
    directory = Path(record_dir or os.environ.get("TAPO_RECORD_DIR") or "recordings")
    state: dict[str, Recording | None] = {"recording": None}
    export: dict = {"task": None, "name": "", "seconds": 0.0, "done": 0.0, "saved": 0.0, "error": ""}  # SD カードからの取り出し
    player: dict[str, SdPlayer | None] = {"current": None}  # SD カードの録画の再生（1つだけ）
    playing: set[asyncio.Task] = set()  # スピーカーで鳴らしている（順番待ちを含む）処理
    record_lock = asyncio.Lock()  # 開始と停止を1つずつ行う（同時の開始で ffmpeg を2本起こさない）
    # 検知イベントの受信。最初に問い合わせが来たときに購読を始め、新しいものから 200 件を覚えておく
    detected: dict = {"task": None, "seq": 0, "log": deque(maxlen=200), "active": {}, "error": "", "failed": 0.0}

    async def stop_player() -> None:
        current, player["current"] = player["current"], None
        if current is not None:
            await current.stop()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.camera = camera or TapoC220()
        try:
            await app.state.camera.detect_inverted()   # 逆さに付けていれば、矢印の向きを合わせる
        except Exception:  # noqa: BLE001
            pass
        app.state.webrtc = WebRtcSessions(app.state.camera.cfg)
        app.state.sdcard = app.state.camera.sdcard
        yield
        for task in (export["task"], detected["task"]):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await stop_player()
        await app.state.webrtc.close()
        if state["recording"] is not None:
            try:
                await state["recording"].stop()
            except MediaError:
                pass
        await app.state.camera.aclose()

    app = FastAPI(title="Tapo C220", lifespan=lifespan)

    @app.middleware("http")
    async def protect_browser_requests(request: Request, call_next):
        host = request.headers.get("host", "")
        if allowed_hosts is not None and not _host_allowed(host, allowed_hosts):
            return JSONResponse({"detail": "許可されていない Host"}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and not _origin_matches_host(origin, host):
                return JSONResponse({"detail": "Origin が Host と一致しない（ほかのサイトからの要求）"}, status_code=403)
        if password is not None and not _has_password(request.headers.get("authorization"), password):
            return JSONResponse({"detail": "認証が要る"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Tapo C220"'})
        return await call_next(request)

    def cam() -> TapoC220:
        return app.state.camera

    @app.exception_handler(OnvifError)
    @app.exception_handler(MediaError)
    @app.exception_handler(StreamError)
    @app.exception_handler(TapoApiError)
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
                "position": asdict(data["position"]), "speaker": cam().tapo.available}

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
                "done": round(export["done"], 1), "saved": round(export["saved"], 1), "error": export["error"]}

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
        if app.state.sdcard.busy:
            raise HTTPException(409, "録画を再生している間は取り出せない（再生を止めること）")
        name = "sd-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(body.start)) + ".mp4"
        export.update(name=name, seconds=body.seconds, done=0.0, saved=0.0, error="")

        async def work() -> None:
            try:
                export["saved"] = await app.state.sdcard.export(body.start, body.seconds, directory / name,
                                                                 progress=lambda done: export.update(done=done))
            except asyncio.CancelledError:
                export["error"] = "取り出しを止めた"
            except PlaybackInterrupted as e:
                export.update(saved=e.saved, done=e.saved, error=str(e))
            except (StreamError, MediaError) as e:
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

    @app.post("/api/sd/play")
    async def sd_play(body: SdPlay) -> dict:
        """録画を、指定した時刻から WebRTC で流し始める。止めるまで（または録画の終わりまで）続く。前の再生は止める。"""
        await stop_player()
        if app.state.sdcard.busy:
            raise HTTPException(409, "録画を取り出している間は再生できない")
        current = SdPlayer(app.state.sdcard, body.start)

        async def closed() -> None:
            await current.stop()
            if player["current"] is current:
                player["current"] = None

        answer = await app.state.webrtc.answer_tracks(body.sdp, "offer", [current.video, current.audio],
                                                      codecs={"video": "video/H264"}, on_close=closed)
        player["current"] = current
        current.begin()
        return {"sdp": answer.sdp, "type": answer.type}

    @app.get("/api/sd/play")
    async def sd_play_status() -> dict:
        current = player["current"]
        if current is None:
            return {"playing": False}
        return {"playing": not current.finished, "start": current.start, "position": current.position,
                "error": current.error}

    @app.post("/api/sd/play/stop")
    async def sd_play_stop() -> dict:
        await stop_player()
        return {"ok": True}

    # --- スピーカー ---------------------------------------------------------------------------------------------------

    async def speak(sound) -> dict:
        """スピーカーで鳴らす処理を、止められるようにして待つ。鳴り終わってから応答する。"""
        task = asyncio.create_task(sound)
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

    @app.post("/api/speaker/play")
    async def speaker_play(file: UploadFile, gain_db: float = 0.0) -> dict:
        """音声ファイルを受け取り、カメラのスピーカーで鳴らす。"""
        if not -30 <= gain_db <= 30:
            raise HTTPException(400, "gain_db は -30〜30")
        data = await file.read(_UPLOAD_MAX + 1)
        if len(data) > _UPLOAD_MAX:
            raise HTTPException(413, "音声ファイルが大きすぎる")
        with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "").suffix[:8]) as tmp:
            tmp.write(data)
            tmp.flush()
            return await speak(cam().play(tmp.name, audio_filter=f"volume={gain_db:.1f}dB" if gain_db else ""))

    @app.post("/api/speaker/siren")
    async def speaker_siren(body: Siren) -> dict:
        """サイレンの音を鳴らす（カメラ内蔵の警報をいますぐ鳴らす API が無いので、スピーカーへ音を送る）。"""
        return await speak(cam().siren(body.seconds))

    @app.post("/api/speaker/stop")
    async def speaker_stop() -> dict:
        """鳴らしている途中の音声を止める。"""
        for task in playing:
            task.cancel()
        return {"stopped": len(playing)}

    # --- 設定と検知 ---------------------------------------------------------------------------------------------------

    @app.get("/api/settings")
    async def settings() -> dict:
        s = cam().settings
        return {"day_night": await s.day_night(), "image": await s.image(), "detections": await s.detections()}

    @app.post("/api/settings/image")
    async def settings_image(body: Image) -> dict:
        await cam().settings.set_image(**body.model_dump())
        return await cam().settings.image()

    @app.post("/api/settings/day-night")
    async def settings_day_night(body: DayNightMode) -> dict:
        await cam().settings.set_day_night(body.mode)
        return {"day_night": await cam().settings.day_night()}

    @app.post("/api/settings/detection")
    async def settings_detection(body: DetectionSwitch) -> dict:
        await cam().settings.set_detection(body.kind, body.enabled)
        return {body.kind.value: (await cam().settings.detection(body.kind))["enabled"]}

    async def listen() -> None:
        try:
            async for event in cam().events():
                detected["seq"] += 1
                detected["active"][event.kind] = event.active
                detected["log"].append({"seq": detected["seq"], "kind": event.kind, "active": event.active,
                                        "time": event.time})
        except (OnvifError, OSError) as e:
            detected["error"] = str(e) or type(e).__name__
            detected["active"].clear()  # 収まった知らせを受け取れないので、検知中のままにしない
            detected["failed"] = time.monotonic()
        finally:
            detected["task"] = None

    @app.get("/api/events")
    async def events(after: int = 0) -> dict:
        """検知イベントのうち、番号が after より後のもの。購読は、最初にここが呼ばれたときに始める。"""
        if detected["task"] is None and time.monotonic() - detected["failed"] > _EVENT_RETRY:
            detected["error"] = ""
            detected["task"] = asyncio.create_task(listen())
        return {"events": [e for e in detected["log"] if e["seq"] > after], "seq": detected["seq"],
                "active": detected["active"], "listening": detected["task"] is not None, "error": detected["error"]}

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


def is_loopback_host(host: str) -> bool:
    """待ち受け先がループバックアドレスか調べる。"""
    if host.lower() == "localhost":
        return True
    value = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _host_name(value: str) -> str | None:
    """Host または許可名から、ポートを除いたホスト名を取り出す。"""
    try:
        parsed = urlsplit("//" + value)
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return None
        parsed.port  # 不正なポート番号も拒む
        if not parsed.hostname:
            return None
        try:
            return str(ipaddress.ip_address(parsed.hostname))
        except ValueError:
            return parsed.hostname.lower().rstrip(".")
    except ValueError:
        return None


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    name = _host_name(host)
    return name is not None and any(name == _host_name(allowed) for allowed in allowed_hosts)


def _origin_matches_host(origin: str, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
            return False
        parsed.port  # 不正なポート番号も拒む
        return parsed.netloc.lower() == host.lower()
    except ValueError:
        return False


def _has_password(authorization: str | None, password: str) -> bool:
    if authorization is None:
        return False
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "basic":
        return False
    try:
        credentials = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return False
    _, separator, supplied = credentials.partition(b":")
    expected = password.encode("utf-8")
    matches = secrets.compare_digest(supplied, expected)
    return bool(separator and matches)


def build_allowed_hosts(host: str, configured: list[str]) -> list[str] | None:
    """待ち受け先と標準のローカル名、追加の許可名から Host 一覧を作る。"""
    if host in ("0.0.0.0", "::") and not configured:
        return None
    bind_host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ipaddress.ip_address(bind_host)
        bind_host = f"[{address.compressed}]" if address.version == 6 else str(address)
    except ValueError:
        pass
    return [bind_host, "localhost", "127.0.0.1", "[::1]", *configured]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tapo C220 の WebUI",
        epilog="Basic 認証のパスワードは、平文の HTTP では盗聴できる。LAN の外から使うなら、TLS の逆プロキシを前に置くこと。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス（既定は 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8220)
    parser.add_argument("--password", help="WebUI の Basic 認証パスワード（環境変数 TAPO_WEBUI_PASSWORD より優先）")
    parser.add_argument("--allowed-host", action="append", default=None,
                        help="Host ヘッダで許す名前（複数指定可。TAPO_WEBUI_ALLOWED_HOSTS と併用）")
    return parser


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    args.password = args.password if args.password is not None else os.environ.get("TAPO_WEBUI_PASSWORD")
    args.password = args.password or None
    configured = [name.strip() for name in os.environ.get("TAPO_WEBUI_ALLOWED_HOSTS", "").split(",") if name.strip()]
    configured.extend(args.allowed_host or [])
    args.allowed_hosts = build_allowed_hosts(args.host, configured)
    if not is_loopback_host(args.host) and not args.password:
        parser.error("ループバック以外で待ち受けるなら、パスワードが要る（--password か TAPO_WEBUI_PASSWORD）")
    return args


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    args = _parse_args(argv)
    uvicorn.run(create_app(password=args.password, allowed_hosts=args.allowed_hosts), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
