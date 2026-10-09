"""コマンドラインから一通り試す。`tapo-c220 info` など。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import DIRECTIONS, MediaError, OnvifError, TalkError, TapoC220


async def _run(args: argparse.Namespace) -> None:
    async with TapoC220() as cam:
        match args.command:
            case "info":
                info = await cam.info()
                print(json.dumps({"device": info["device"], "profiles": [asdict(p) for p in info["profiles"]],
                                  "position": asdict(info["position"]), "services": await cam.onvif.services(),
                                  "presets": [asdict(p) for p in await cam.presets()]}, ensure_ascii=False, indent=2))
            case "snapshot":
                Path(args.path).write_bytes(await cam.snapshot(args.stream))
            case "record":
                recording = await cam.record(args.path, kind=args.kind, max_seconds=args.seconds)
                await recording.wait()
            case "play":
                print(f"{await cam.play(args.path, audio_filter=args.filter):.1f} 秒ぶん鳴らした")
            case "move":
                await cam.move(args.direction, args.speed)
                await asyncio.sleep(args.seconds)
                await cam.stop()
            case "step":
                await cam.step(args.direction, args.amount)
            case "goto":
                await cam.goto(args.pan, args.tilt)
            case "position":
                print(json.dumps(asdict(await cam.position())))


def main() -> None:
    parser = argparse.ArgumentParser(prog="tapo-c220", description="Tapo C220 を RTSP / ONVIF / 双方向音声で操作する")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="機種、プロファイル、PTZ の位置などを JSON で出す")
    sub.add_parser("position", help="PTZ の今の位置")
    p = sub.add_parser("snapshot", help="静止画を1枚 JPEG で保存する")
    p.add_argument("path")
    p.add_argument("--stream", default="stream1", choices=["stream1", "stream2"])
    p = sub.add_parser("record", help="録音（.wav）か録画（.mp4）をする")
    p.add_argument("path")
    p.add_argument("--kind", default="audio", choices=["audio", "video"])
    p.add_argument("--seconds", type=float, default=10.0)
    p = sub.add_parser("play", help="音声ファイルをカメラのスピーカーで鳴らす")
    p.add_argument("path")
    p.add_argument("--filter", default="", help="ffmpeg の音声フィルタ（例: volume=-6dB）")
    p = sub.add_parser("move", help="その向きへ、決めた秒数だけ動かす")
    p.add_argument("direction", choices=list(DIRECTIONS))
    p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--speed", type=float, default=0.5)
    p = sub.add_parser("step", help="その向きへ、決めた量だけ動かす")
    p.add_argument("direction", choices=list(DIRECTIONS))
    p.add_argument("--amount", type=float, default=0.05)
    p = sub.add_parser("goto", help="位置（それぞれ -1〜1）を指定して動かす")
    p.add_argument("pan", type=float)
    p.add_argument("tilt", type=float)
    try:
        asyncio.run(_run(parser.parse_args()))
    except (OnvifError, MediaError, TalkError, RuntimeError) as e:
        sys.exit(f"失敗: {e}")


if __name__ == "__main__":
    main()
