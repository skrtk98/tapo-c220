"""Tapo C220 の操作をひとまとめにした窓口。映像・音声は RTSP、首振りは ONVIF、スピーカーは Tapo 独自の双方向音声。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from enum import StrEnum
from pathlib import Path

from . import media
from .config import TapoConfig
from .events import Event, events
from .media import Recording
from .onvif import OnvifClient, Preset, PtzPosition
from .talk import TalkError, play_alaw

class Direction(StrEnum):
    """画面が動く向き。値は "left" などの文字列で、`Direction("left")` のように文字列からも作れる。"""

    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"

    @property
    def velocity(self) -> tuple[int, int]:
        """画面がこの向きへ動く、ContinuousMove の速度（pan, tilt）の符号。
        C220 の実測では、速度は「正の pan で右、正の tilt で上」。一方、位置（GetStatus・AbsoluteMove・RelativeMove）の
        pan は向きが逆で、正の値で左を向く。tilt は位置も正の値で上。"""
        return _VELOCITY[self]

    @classmethod
    def _missing_(cls, value: object) -> Direction | None:
        raise ValueError(f"向きは {' / '.join(cls)} のどれか: {value}")


_VELOCITY = {Direction.LEFT: (-1, 0), Direction.RIGHT: (1, 0), Direction.UP: (0, 1), Direction.DOWN: (0, -1)}


class TapoC220:
    def __init__(self, cfg: TapoConfig | None = None):
        self.cfg = cfg or TapoConfig.load()
        self.onvif = OnvifClient(self.cfg.host, self.cfg.user, self.cfg.password, port=self.cfg.onvif_port)
        self._speaker = asyncio.Lock()
        self._settings = None

    async def aclose(self) -> None:
        await self.onvif.aclose()

    async def __aenter__(self) -> TapoC220:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def info(self) -> dict:
        """機種・ファームウェアと、ONVIF のプロファイル、PTZ の今の位置。"""
        device, profiles, position = await asyncio.gather(
            self.onvif.device_information(), self.onvif.profiles(), self.onvif.ptz_status())
        return {"device": device, "profiles": profiles, "position": position}

    # --- 映像と音声（RTSP） -------------------------------------------------------------------------------------------

    def rtsp_url(self, stream: str = media.MAIN) -> str:
        """認証情報を含む URL。VLC や ffplay に渡せる。ログには出さないこと。"""
        return media.rtsp_url(self.cfg, stream)

    async def snapshot(self, stream: str = media.MAIN) -> bytes:
        return await media.snapshot(self.cfg, stream)

    def live_audio(self) -> AsyncIterator[bytes]:
        return media.live_audio(self.cfg)

    async def record(self, path: str | Path, *, kind: str = "audio", stream: str = media.MAIN,
                     max_seconds: float | None = None) -> Recording:
        """録音（kind="audio"）か録画（kind="video"）を始める。返した Recording の stop() で終える。"""
        return await media.record(self.cfg, path, kind=kind, stream=stream, max_seconds=max_seconds)

    # --- スピーカー（Tapo 独自の双方向音声） -----------------------------------------------------------------------------

    async def play(self, source: str | Path, *, max_seconds: float | None = None, audio_filter: str = "") -> float:
        """音声ファイル（ffmpeg が読める形式なら何でも）をカメラのスピーカーで鳴らす。鳴らした秒数を返す。
        鳴り終わるまで戻らない。同時には1つだけ（重なった呼び出しは順番を待つ）。
        max_seconds を指定すると先頭のその長さだけを鳴らす。途中でやめるには、この呼び出しをキャンセルする。"""
        alaw = await media.to_alaw(source, max_seconds=max_seconds, audio_filter=audio_filter)
        async with self._speaker:
            await play_alaw(self.cfg.host, self.cfg.cloud_password, alaw, port=self.cfg.talk_port)
        return len(alaw) / 8000

    async def siren(self, seconds: float = 10.0) -> float:
        """サイレンの音をスピーカーで鳴らす（カメラ内蔵の警報をいますぐ鳴らす API が、このファームウェアに無いため）。
        音の大きさは、カメラのスピーカーの音量（settings.set_speaker_volume）で決まる。途中でやめるにはキャンセルする。"""
        alaw = await media.siren_alaw(seconds)
        async with self._speaker:
            await play_alaw(self.cfg.host, self.cfg.cloud_password, alaw, port=self.cfg.talk_port)
        return len(alaw) / 8000

    # --- 設定と検知（Tapo 独自の API、ONVIF のイベント） ------------------------------------------------------------------

    @property
    def settings(self):
        """カメラの設定（settings.CameraSettings）。pytapo が要る（`tapo-c220-survey[tapo]`）。"""
        if self._settings is None:
            from .settings import CameraSettings
            self._settings = CameraSettings(self.cfg)
        return self._settings

    def events(self, *, changes_only: bool = True) -> AsyncIterator[Event]:
        """検知のイベントを、やめるまで受け取り続ける（events.events）。その検知をオンにしておくこと（settings.set_detection）。"""
        return events(self.onvif, changes_only=changes_only)

    # --- 首振り（ONVIF PTZ） ------------------------------------------------------------------------------------------

    async def position(self) -> PtzPosition:
        return await self.onvif.ptz_status()

    async def move(self, direction: Direction | str, speed: float = 0.5, timeout: float | None = None) -> None:
        """その向きへ動かし続ける。stop() を呼ぶか、timeout（秒）で止まる。speed は 0〜1。"""
        pan, tilt = Direction(direction).velocity
        speed = min(max(speed, 0.0), 1.0)
        await self.onvif.continuous_move(pan * speed, tilt * speed, timeout)

    async def step(self, direction: Direction | str, amount: float = 0.05) -> None:
        """その向きへ、決まった量だけ動かす。amount は可動域の全体を 2 としたときの量。"""
        pan, tilt = Direction(direction).velocity
        await self.onvif.relative_move(-pan * amount, tilt * amount)  # 位置の pan は、速度と向きが逆

    async def stop(self) -> None:
        await self.onvif.stop()

    async def goto(self, pan: float, tilt: float) -> None:
        """位置（それぞれ -1〜1）を指定して動かす。pan は正の値で左、tilt は正の値で上。"""
        await self.onvif.absolute_move(pan, tilt)

    async def presets(self) -> list[Preset]:
        return await self.onvif.presets()

    async def save_preset(self, name: str) -> str:
        return await self.onvif.set_preset(name)

    async def goto_preset(self, token: str) -> None:
        await self.onvif.goto_preset(token)

    async def remove_preset(self, token: str) -> None:
        await self.onvif.remove_preset(token)


__all__ = ["Direction", "TalkError", "TapoC220"]
