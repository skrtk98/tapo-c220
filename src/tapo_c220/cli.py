"""コマンドラインから一通り試す。`tapo-c220 info` など。結果は JSON で標準出力へ出す。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import Direction, MediaError, OnvifError, StreamError, TapoC220

_SWITCH = {"on": True, "off": False}
_DAY_NIGHT = {"auto": "auto", "day": "off", "night": "on"}  # 呼び名 → settings.DayNight の値


def _level(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 100:
        raise ValueError(f"0〜100 で指定する: {text}")
    return value


def _switch(text: str) -> bool:
    if text not in _SWITCH:
        raise ValueError(f"on か off で指定する: {text}")
    return _SWITCH[text]


def _day_night(text: str) -> str:
    if text not in _DAY_NIGHT:
        raise ValueError(f"auto / day / night で指定する: {text}")
    return _DAY_NIGHT[text]


# `set` で書ける項目 → (値の読み方, 設定を書く呼び出し)。s は settings.CameraSettings
_SETTERS = {
    "privacy": (_switch, lambda s, v: s.set_privacy(v)),
    "led": (_switch, lambda s, v: s.set_led(v)),
    "notifications": (_switch, lambda s, v: s.set_notifications(v)),
    "day-night": (_day_night, lambda s, v: s.set_day_night(v)),
    "brightness": (_level, lambda s, v: s.set_image(brightness=v)),
    "contrast": (_level, lambda s, v: s.set_image(contrast=v)),
    "saturation": (_level, lambda s, v: s.set_image(saturation=v)),
    "sharpness": (_level, lambda s, v: s.set_image(sharpness=v)),
    "flip": (_switch, lambda s, v: s.set_flip(v)),
    "lens-correction": (_switch, lambda s, v: s.set_lens_correction(v)),
    "light-frequency": (str, lambda s, v: s.set_light_frequency(v)),
    "osd-date": (_switch, lambda s, v: s.set_osd(date=v)),
    "osd-week": (_switch, lambda s, v: s.set_osd(week=v)),
    "osd-logo": (_switch, lambda s, v: s.set_osd(logo=v)),
    "speaker-volume": (_level, lambda s, v: s.set_speaker_volume(v)),
    "mic-volume": (_level, lambda s, v: s.set_microphone(volume=v)),
    "mic-mute": (_switch, lambda s, v: s.set_microphone(mute=v)),
    "auto-track": (_switch, lambda s, v: s.set_auto_track(v)),
    "recording": (_switch, lambda s, v: s.set_recording(v)),
    "loop-recording": (_switch, lambda s, v: s.set_loop_recording(v)),
}

# `settings` で読める項目 → 設定を読む呼び出し
_GETTERS = {
    "device": lambda s: s.info(),
    "time": lambda s: s.time(),
    "privacy": lambda s: s.privacy(),
    "led": lambda s: s.led(),
    "notifications": lambda s: s.notifications(),
    "day-night": lambda s: _day_night_name(s),
    "image": lambda s: s.image(),
    "flip": lambda s: s.flip(),
    "lens-correction": lambda s: s.lens_correction(),
    "light-frequency": lambda s: s.light_frequency(),
    "osd": lambda s: s.osd(),
    "video": lambda s: s.video(),
    "audio": lambda s: s.audio(),
    "detections": lambda s: s.detections(),
    "alarm": lambda s: s.alarm(),
    "auto-track": lambda s: s.auto_track(),
    "recording": lambda s: s.recording(),
    "firmware": lambda s: s.firmware(),
    "scheduled-reboot": lambda s: s.scheduled_reboot(),
}
_DEFAULT_GETTERS = [name for name in _GETTERS if name not in ("device", "time", "video", "firmware", "scheduled-reboot")]


async def _day_night_name(settings) -> str:
    mode = await settings.day_night()
    return next(name for name, value in _DAY_NIGHT.items() if value == mode.value)


def _timestamp(text: str) -> float:
    """時刻を UNIX 時刻にする。"2026-10-09 15:08:45"（この端末の時刻）か、UNIX 時刻の数字。"""
    try:
        return float(text)
    except ValueError:
        pass
    for form in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return time.mktime(time.strptime(text, form))
        except ValueError:
            continue
    raise ValueError(f'時刻は "YYYY-MM-DD HH:MM:SS" か UNIX 時刻: {text}')


def _local(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def _settings(cam: TapoC220, args: argparse.Namespace) -> None:
    match args.command:
        case "settings":
            unknown = [name for name in args.names if name not in _GETTERS]
            if unknown:
                raise ValueError(f"読める項目は {' / '.join(_GETTERS)}: {' '.join(unknown)}")
            _print({name: await _GETTERS[name](cam.settings) for name in args.names or _DEFAULT_GETTERS})
        case "set":
            parse, write = _SETTERS[args.name]
            await write(cam.settings, parse(args.value))
        case "detect":
            if args.state is None:
                _print(await cam.settings.detection(args.kind) if args.kind else await cam.settings.detections())
            else:
                level = args.sensitivity
                await cam.settings.set_detection(args.kind, _switch(args.state), int(level) if level and level.isdigit() else level)
        case "alarm":
            changes = {"enabled": args.enabled, "sound": args.sound, "light": args.light}
            changes = {name: _switch(value) for name, value in changes.items() if value is not None}
            if args.volume is not None:
                changes["volume"] = args.volume
            if args.duration is not None:
                changes["duration"] = args.duration
            if changes:
                await cam.settings.set_alarm(**changes)
            _print(await cam.settings.alarm())


async def _sd(cam: TapoC220, args: argparse.Namespace) -> None:
    sd = cam.sdcard
    match args.action:
        case "status":
            _print(await sd.status())
        case "days":
            _print(await sd.days())
        case "segments":
            _print([{**asdict(s), "start_local": _local(s.start), "end_local": _local(s.end)}
                    for s in await sd.segments(args.date)])
        case "detections":
            day = time.strptime(args.date, "%Y%m%d")
            start = time.mktime(day)
            end = time.mktime((day.tm_year, day.tm_mon, day.tm_mday + 1, 0, 0, 0, 0, 0, -1))  # 次の日の 0 時（夏時間の日も）
            _print([{**d, "start_local": _local(d["start"]), "end_local": _local(d["end"])}
                    for d in await sd.detections(start, end)])
        case "export":
            shown = [-10.0]

            def progress(done: float) -> None:
                if done - shown[0] >= 10 and sys.stderr.isatty():
                    shown[0] = done
                    print(f"\r{done:.0f} / {args.seconds:.0f} 秒", end="", file=sys.stderr, flush=True)

            got = await sd.export(_timestamp(args.start), args.seconds, args.path, progress=progress)
            if sys.stderr.isatty():
                print(file=sys.stderr)
            _print({"path": args.path, "seconds": round(got, 1)})


async def _run(args: argparse.Namespace) -> None:
    async with TapoC220() as cam:
        match args.command:
            case "info":
                info = await cam.info()
                _print({"device": info["device"], "profiles": [asdict(p) for p in info["profiles"]],
                        "position": asdict(info["position"]), "services": await cam.onvif.services(),
                        "presets": [asdict(p) for p in await cam.presets()]})
            case "snapshot":
                Path(args.path).write_bytes(await cam.snapshot(args.stream))
            case "record":
                recording = await cam.record(args.path, kind=args.kind, max_seconds=args.seconds)
                await recording.wait()
            case "play":
                print(f"{await cam.play(args.path, audio_filter=args.filter):.1f} 秒ぶん鳴らした")
            case "siren":
                print(f"{await cam.siren(args.seconds):.1f} 秒ぶん鳴らした")
            case "move":
                # Ctrl-C で中断されても止める。止める指示が届かなくても、カメラ側の時間切れで止まるようにしておく
                await cam.move(args.direction, args.speed, timeout=args.seconds + 1)
                try:
                    await asyncio.sleep(args.seconds)
                finally:
                    await cam.stop()
            case "step":
                await cam.step(args.direction, args.amount)
            case "goto":
                await cam.goto(args.pan, args.tilt)
            case "position":
                print(json.dumps(asdict(await cam.position())))
            case "preset":
                match args.action:
                    case "list":
                        _print([asdict(p) for p in await cam.presets()])
                    case "save":
                        _print({"token": await cam.save_preset(args.name)})
                    case "goto":
                        await cam.goto_preset(args.token)
                    case "remove":
                        await cam.remove_preset(args.token)
            case "events":
                await _events(cam, args)
            case "sd":
                await _sd(cam, args)
            case _:
                await _settings(cam, args)


async def _events(cam: TapoC220, args: argparse.Namespace) -> None:
    """検知のイベントを、1行に1つの JSON で出し続ける（Ctrl-C か --seconds で終える）。"""
    async def listen() -> None:
        async for event in cam.events(changes_only=not args.all):
            print(json.dumps({**asdict(event), "local": _local(event.time)}, ensure_ascii=False), flush=True)

    try:
        await asyncio.wait_for(listen(), args.seconds)
    except asyncio.TimeoutError:
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapo-c220", description="Tapo C220 を RTSP / ONVIF / Tapo 独自の経路で操作する")
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
    p = sub.add_parser("siren", help="サイレンの音をカメラのスピーカーで鳴らす")
    p.add_argument("--seconds", type=float, default=10.0)
    p = sub.add_parser("move", help="その向きへ、決めた秒数だけ動かす")
    p.add_argument("direction", choices=list(Direction))
    p.add_argument("--seconds", type=float, default=1.0)
    p.add_argument("--speed", type=float, default=0.5)
    p = sub.add_parser("step", help="その向きへ、決めた量だけ動かす")
    p.add_argument("direction", choices=list(Direction))
    p.add_argument("--amount", type=float, default=0.05)
    p = sub.add_parser("goto", help="位置（それぞれ -1〜1）を指定して動かす")
    p.add_argument("pan", type=float)
    p.add_argument("tilt", type=float)

    p = sub.add_parser("preset", help="向きのプリセット").add_subparsers(dest="action", required=True)
    p.add_parser("list", help="一覧")
    p.add_parser("save", help="今の向きを保存する").add_argument("name")
    p.add_parser("goto", help="その向きへ動かす").add_argument("token")
    p.add_parser("remove", help="消す").add_argument("token")

    p = sub.add_parser("events", help="検知のイベントを受け取り続ける（1行に1つの JSON）。検知をオンにしておくこと")
    p.add_argument("--all", action="store_true", help="状態が変わったときだけでなく、届いた通知をすべて出す")
    p.add_argument("--seconds", type=float, default=None, help="この秒数で終える（既定は Ctrl-C まで）")

    p = sub.add_parser("settings", help="設定を読む（項目を省くと、主なものをまとめて）")
    p.add_argument("names", nargs="*", metavar="name", help=" / ".join(_GETTERS))
    p = sub.add_parser("set", help="設定を1つ書く（例: set day-night night、set brightness 60、set privacy on）")
    p.add_argument("name", choices=list(_SETTERS))
    p.add_argument("value", help="on / off、0〜100、day-night は auto / day / night、light-frequency は auto / 50 / 60")
    p = sub.add_parser("detect", help="検知の設定を読む（引数なしで全種類）、または on / off を書く")
    p.add_argument("kind", nargs="?", help="motion / person / pet / vehicle / baby_cry / bark / meow / glass_break / "
                                             "tamper / line_crossing")
    p.add_argument("state", nargs="?", choices=list(_SWITCH))
    p.add_argument("--sensitivity", help='1〜100 か low / normal / high')
    p = sub.add_parser("alarm", help="警報（検知したときに鳴らす・光らせる）の設定を読む。オプションを付けると書く")
    for name in ("enabled", "sound", "light"):
        p.add_argument(f"--{name}", choices=list(_SWITCH))
    p.add_argument("--volume", choices=["low", "normal", "high"])
    p.add_argument("--duration", type=int, help="鳴らす秒数")

    p = sub.add_parser("sd", help="microSD の録画").add_subparsers(dest="action", required=True)
    p.add_parser("status", help="カードの状態")
    p.add_parser("days", help="録画のある日")
    p.add_parser("segments", help="その日の録画の区間").add_argument("date", help="YYYYMMDD")
    p.add_parser("detections", help="その日にカメラが検知して記録した出来事").add_argument("date", help="YYYYMMDD")
    e = p.add_parser("export", help="録画の一部を MP4 に保存する（実時間の速さでしか進まない）")
    e.add_argument("start", help='"YYYY-MM-DD HH:MM:SS"（この端末の時刻）か UNIX 時刻')
    e.add_argument("seconds", type=float)
    e.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (OnvifError, MediaError, StreamError, RuntimeError, ValueError) as e:  # TapoApiError は RuntimeError
        sys.exit(f"失敗: {e}")
    except ModuleNotFoundError as e:
        sys.exit(f"失敗: {e.name} が入っていない（uv sync --extra tapo で入れる）")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
