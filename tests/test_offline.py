"""カメラ無しで確かめられる部分（設定の読み込み、SOAP の組み立てと解析、TS の組み立て、向きの対応）。"""

import asyncio
import socket
import sys
import time

import httpx
import pytest

from tapo_c220 import Direction, MediaError, OnvifClient, OnvifError, TapoC220, TapoConfig
from tapo_c220.onvif import PtzPosition, redact
from tapo_c220 import talk
from tapo_c220.tapo.stream import Connection, digest_password
from tapo_c220.talk import Muxer, TalkError, _reply_params, crc32_mpeg, header_packets, play_alaw

CFG = TapoConfig(host="192.0.2.1", user="user", password="p@ss/word", cloud_password="cloud")


def _envelope(body: str) -> str:
    return ('<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
            f'xmlns:tt="http://www.onvif.org/ver10/schema"><SOAP-ENV:Body>{body}</SOAP-ENV:Body></SOAP-ENV:Envelope>')


def _client(handler) -> tuple[OnvifClient, list[str]]:
    sent: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request.content.decode())
        if "GetSystemDateAndTime" in sent[-1]:
            return httpx.Response(500, text="no clock")
        return handler(sent[-1])

    client = OnvifClient(CFG.host, CFG.user, CFG.password)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    return client, sent


def test_config_file_and_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nTAPO_HOST=192.0.2.9\nexport TAPO_USER="me"\nTAPO_PASSWORD=a=b\nOTHER=1\n')
    for name in ("TAPO_HOST", "TAPO_USER", "TAPO_PASSWORD", "TAPO_CLOUD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    cfg = TapoConfig.load(env)
    assert (cfg.host, cfg.user, cfg.password, cfg.cloud_password) == ("192.0.2.9", "me", "a=b", "")
    monkeypatch.setenv("TAPO_HOST", "192.0.2.10")
    assert TapoConfig.load(env).host == "192.0.2.10"
    with pytest.raises(RuntimeError, match="TAPO_HOST"):
        monkeypatch.delenv("TAPO_HOST")
        TapoConfig.load(tmp_path / "missing")


async def test_onvif_status_and_auth_header():
    client, sent = _client(lambda _: httpx.Response(200, text=_envelope(
        '<tptz:GetStatusResponse><tptz:PTZStatus><tt:Position><tt:PanTilt x="0.25" y="-0.5"></tt:PanTilt>'
        "</tt:Position></tptz:PTZStatus></tptz:GetStatusResponse>")))
    position = await client.ptz_status()
    assert (position.pan, position.tilt) == (0.25, -0.5)
    assert "PasswordDigest" in sent[-1] and CFG.password not in sent[-1]


async def test_onvif_fault_becomes_error():
    client, _ = _client(lambda _: httpx.Response(500, text=_envelope(
        "<SOAP-ENV:Fault><SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value><SOAP-ENV:Subcode>"
        "<SOAP-ENV:Value>ter:InvalidPosition</SOAP-ENV:Value></SOAP-ENV:Subcode></SOAP-ENV:Code>"
        "<SOAP-ENV:Reason><SOAP-ENV:Text>Destination out of bounds</SOAP-ENV:Text></SOAP-ENV:Reason></SOAP-ENV:Fault>")))
    with pytest.raises(OnvifError, match="out of bounds") as error:
        await client.absolute_move(2, 0)
    assert error.value.code == "ter:InvalidPosition"


async def test_continuous_move_timeout_is_whole_seconds():
    """小数の秒（PT0.5S）は、カメラの HTTP サーバを壊す。"""
    client, sent = _client(lambda _: httpx.Response(200, text=_envelope("<tptz:ContinuousMoveResponse/>")))
    await client.continuous_move(0.5, 0, timeout=0.5)
    assert "<tptz:Timeout>PT1S</tptz:Timeout>" in sent[-1]
    await client.continuous_move(0.5, 0, timeout=4.2)
    assert "<tptz:Timeout>PT5S</tptz:Timeout>" in sent[-1]


async def test_directions_follow_the_view():
    """速度は正の pan で右。位置（RelativeMove）の pan は逆で、正の値で左。"""
    camera = TapoC220(CFG)
    client, sent = _client(lambda _: httpx.Response(200, text=_envelope("<tptz:OkResponse/>")))
    camera.onvif = client
    await camera.move(Direction.RIGHT, 0.4)
    assert 'x="0.400000" y="0.000000"' in sent[-1]
    await camera.step("right", 0.1)
    assert 'x="-0.100000" y="0.000000"' in sent[-1]
    await camera.step("up", 0.1)
    assert 'y="0.100000"' in sent[-1]
    with pytest.raises(ValueError, match="left / right / up / down"):
        await camera.move("sideways")
    assert Direction("up") is Direction.UP and Direction.UP == "up"


def test_redact_hides_credentials():
    assert redact("rtsp://user:secret@192.0.2.1:554/stream1 failed for secret", "secret") == \
        "rtsp://***@192.0.2.1:554/stream1 failed for ***"


def test_ts_packets():
    assert crc32_mpeg(b"123456789") == 0x0376E6E7
    header = header_packets()
    assert len(header) == 376 and header[0] == header[188] == 0x47
    mux = Muxer()
    first, second = mux.audio(b"\xd5" * 160), mux.audio(b"\xd5" * 160)
    assert len(first) == len(second) == 188 and first[1] & 0x40
    assert first[3] & 0xF == 0 and second[3] & 0xF == 1   # 連続性の番号が進む
    assert first[-160:] == b"\xd5" * 160


def test_talk_helpers():
    assert digest_password("abc", "3") == "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
    assert digest_password("abc", "") == "900150983CD24FB0D6963F7D28E17F72"
    assert _reply_params(b'--b\r\n\r\n{"params":{"session_id":"7"') is None
    assert _reply_params(b'--b\r\n\r\n{"params":{"session_id":"7","error_code":0}}\r\n') == {"session_id": "7", "error_code": 0}


def test_sdcard_ts_reader_and_decryptor():
    """スピーカー用に組み立てた TS（音声だけ）を、SD カード用の読み取りで元に戻せる。断片の切れ目は任意。"""
    import hashlib

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from tapo_c220.sdcard import SdCardError, TsReader, decryptor

    mux = Muxer()
    sound = bytes(range(160)) * 5
    stream = mux.header() + b"".join(mux.audio(sound[i:i + 160]) for i in range(0, len(sound), 160))
    reader = TsReader()
    for i in range(0, len(stream), 100):
        reader.feed(stream[i:i + 100])
    assert reader.codec == ("alaw", 8000) and bytes(reader.audio) == sound and reader.video_seconds == 0

    key, iv = hashlib.md5(b"nonce:PASS").digest(), hashlib.md5(b"admin:nonce").digest()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    decrypt = decryptor("nonce", "PASS")
    assert decrypt(encryptor.update(b"hello world" + b"\x05" * 5) + encryptor.finalize()) == b"hello world"
    with pytest.raises(SdCardError):
        decryptor("nonce", "WRONG")(b"\x00" * 32)


@pytest.mark.parametrize(
    ("parts", "eof", "timeout", "expected", "message"),
    [
        ([("video/mp2t", b"ts"), ("application/json", b'{"type":"finished"}')], True, False, None, ""),
        ([("video/mp2t", b"ts")], False, True, "PlaybackInterrupted", "届かなくなった"),
        ([("video/mp2t", b"ts")], True, False, "PlaybackInterrupted", "接続を閉じた"),
        ([], True, False, "NoRecording", "録画が SD カードに無い"),
    ],
)
async def test_sd_playback_distinguishes_finished_stall_close_and_no_video(
    monkeypatch, parts, eof, timeout, expected, message,
):
    from types import SimpleNamespace

    import tapo_c220
    from tapo_c220 import PlaybackInterrupted, sdcard
    from tapo_c220.sdcard import NoRecording, SdCard

    assert "PlaybackInterrupted" in tapo_c220.__all__

    def part(kind: str, data: bytes) -> bytes:
        return (f"--x\r\nContent-Type: {kind}\r\nContent-Length: {len(data)}\r\n\r\n".encode()
                + data + b"\r\n")

    reader = asyncio.StreamReader()
    reader.feed_data(b"".join(part(kind, data) for kind, data in parts))
    if eof:
        reader.feed_eof()

    class Connection:
        nonce, password = "nonce", "password"

        def __init__(self):
            self.reader = reader
            self.closed = False

        def request(self, request):
            pass

        async def close(self):
            self.closed = True

    connection = Connection()

    class Stream:
        async def connect(self):
            return connection

    monkeypatch.setattr(sdcard, "decryptor", lambda nonce, password: lambda data: data)
    if timeout:
        monkeypatch.setattr(sdcard, "_STALL_TIMEOUT", 0.01)
    sd = SdCard(SimpleNamespace(api=None, stream=Stream()))

    if expected is None:
        assert [chunk async for chunk in sd.playback(100)] == [b"ts"]
    else:
        error = PlaybackInterrupted if expected == "PlaybackInterrupted" else NoRecording
        with pytest.raises(error, match=message):
            async for _ in sd.playback(100):
                pass
    assert connection.closed


async def test_sd_export_saves_partial_clip_and_reports_interruption(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tapo_c220 import sdcard
    from tapo_c220.sdcard import PlaybackInterrupted, SdCard

    class Ts:
        video_seconds = 0.0
        codec = None

        def feed(self, data):
            self.video_seconds = 2.25

        def take_audio(self):
            return b""

    async def playback(start):
        yield b"partial ts"
        raise PlaybackInterrupted("録画が途中で届かなくなった（15 秒なにも来ない）")

    async def make_mp4(ts, video, audio, path, seconds):
        assert video.read_bytes() == b"partial ts" and seconds == 10
        path.write_bytes(b"mp4")

    monkeypatch.setattr(sdcard, "TsReader", Ts)
    monkeypatch.setattr(sdcard, "_make_mp4", make_mp4)
    sd = SdCard(SimpleNamespace(api=None, stream=None))
    sd.playback = playback
    path = tmp_path / "partial.mp4"

    with pytest.raises(PlaybackInterrupted, match="秒ぶんまで保存した") as error:
        await sd.export(100, 10, path)
    assert error.value.saved == 2.25 and error.value.path == path
    assert path.read_bytes() == b"mp4"


async def test_sd_packets_propagates_playback_interrupted():
    from types import SimpleNamespace

    from tapo_c220.packets import SdPackets
    from tapo_c220.sdcard import PlaybackInterrupted

    packets = SdPackets(SimpleNamespace(), 100)

    async def interrupted():
        raise PlaybackInterrupted("カメラが録画の途中で接続を閉じた")

    packets._task = asyncio.create_task(interrupted())
    await asyncio.sleep(0)
    packets._queue.put_nowait(None)
    with pytest.raises(PlaybackInterrupted, match="接続を閉じた"):
        await anext(packets)


def test_event_notification():
    """カメラが送ってくる Notify から、検知の種類と、始まり・終わりを読む。"""
    import xml.etree.ElementTree as ET

    from tapo_c220.events import parse_messages

    def message(topic: str, name: str, value: str) -> str:
        return (f'<wsnt:NotificationMessage><wsnt:Topic>tns1:{topic}</wsnt:Topic><wsnt:Message>'
                f'<tt:Message PropertyOperation="Changed" UtcTime="2026-10-10T04:13:10Z"><tt:Source>'
                f'<tt:SimpleItem Value="true" Name="Rule"/></tt:Source><tt:Data><tt:SimpleItem Value="{value}" Name="{name}"/>'
                f'</tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>')

    root = ET.fromstring(
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
        'xmlns:tt="http://www.onvif.org/ver10/schema"><e:Body><wsnt:Notify>'
        + message("RuleEngine/PeopleDetector/People", "IsPeople", "false")
        + message("RuleEngine/CellMotionDetector/Motion", "IsMotion", "true")
        + message("VideoSource/MotionAlarm", "State", "true") + '</wsnt:Notify></e:Body></e:Envelope>')
    assert [(e.kind, e.active) for e in parse_messages(root)] == [("person", False), ("motion", True), ("motion", True)]


async def test_events_bind_and_advertise():
    """コンテナの中などでは、待ち受けるアドレスと、カメラに伝える宛先を別に指定できる。"""
    from tapo_c220.events import events

    _require_local_sockets()
    sent = []

    class Onvif:
        url = "http://127.0.0.1:2020/onvif/service"

        async def call(self, body, url=None):
            sent.append(body)
            raise OnvifError("止める")

    with pytest.raises(OnvifError):
        await anext(events(Onvif(), bind="127.0.0.1", advertise="http://192.0.2.1:8802/onvif-notify"))
    assert "<wsa:Address>http://192.0.2.1:8802/onvif-notify</wsa:Address>" in sent[0]


async def test_settings_requests():
    """設定の書き込みは、与えた項目だけを、カメラの API の名前に直して送る。"""
    from tapo_c220.settings import CameraSettings, DayNight, Detection

    sent = []

    class Api:
        async def execute(self, method, params):
            sent.append((method, params))
            return {"msg_alarm": {"chn1_msg_alarm_info": {"enabled": "off", "alarm_mode": ["light", "sound"],
                                                         "alarm_volume": "high", "alarm_duration": "0"}}}

    settings = CameraSettings(api=Api())
    await settings.set_image(brightness=60, sharpness=40)
    await settings.set_image()   # 何も与えなければ送らない
    await settings.set_alarm(sound=False, volume="low")
    await settings.set_osd(week=True)
    assert sent == [
        ("setDayNightModeConfig", {"image": {"common": {"luma": "60", "sharpness": "40"}}}),
        ("getAlertConfig", {"msg_alarm": {"name": ["chn1_msg_alarm_info"]}}),
        ("setAlertConfig", {"msg_alarm": {"chn1_msg_alarm_info": {"alarm_mode": ["light"], "alarm_volume": "low"}}}),
        ("setOsd", {"method": "set", "OSD": {"week": {"enabled": "on"}}}),
    ]
    with pytest.raises(ValueError):
        await settings.set_image(contrast=101)
    with pytest.raises(ValueError):
        await settings.set_alarm(sound=False, light=False)
    assert DayNight("on") is DayNight.NIGHT and Detection("line_crossing") is Detection.LINE_CROSSING


class _FakeStream:
    """手元の TCP サーバを、認証済みのカメラの 8800 番のふりをさせる。"""

    timeout = 2.0

    def __init__(self, port: int):
        self.port = port

    async def connect(self) -> Connection:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        return Connection(reader, writer, "nonce", "password")


def _require_local_sockets() -> None:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError:
        pytest.skip("実行環境がローカル socket を許可していない")


async def _fake_camera(behave) -> tuple[asyncio.Server, _FakeStream]:
    _require_local_sockets()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"}}\r\n")  # 双方向音声の開始の要求
        writer.write(b'--x\r\nContent-Type: application/json\r\n\r\n{"params":{"error_code":0,"session_id":"7"}}\r\n')
        await writer.drain()
        try:
            await behave(reader, writer)
        finally:
            writer.close()

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # 読まなければすぐ詰まるように、受信の窓を小さくする
    sock.bind(("127.0.0.1", 0))
    server = await asyncio.start_server(serve, sock=sock)
    return server, _FakeStream(sock.getsockname()[1])


async def test_talk_notices_camera_that_closes():
    async def close_soon(reader, writer):
        await reader.read(1000)

    server, stream = await _fake_camera(close_soon)
    async with server:
        with pytest.raises(TalkError, match="閉じた") as error:
            await play_alaw(stream, b"\xd5" * 8000 * 5)
        assert error.value.partial


@pytest.mark.skipif(sys.platform != "linux", reason="届いていない量は Linux でしか調べられない")
async def test_talk_notices_camera_that_stops_reading(monkeypatch):
    monkeypatch.setattr(talk, "_BACKLOG_SECONDS", 0.5)
    stop = asyncio.Event()

    async def never_read(reader, writer):
        reader._transport.pause_reading()
        await stop.wait()

    server, stream = await _fake_camera(never_read)
    async with server:
        started = time.monotonic()
        with pytest.raises(TalkError, match="受け取らなくなった"):
            await play_alaw(stream, b"\xd5" * 8000 * 30)
        assert time.monotonic() - started < 10  # 送信バッファが埋まるのを待たずに気づく
        stop.set()


async def test_talk_completes_when_camera_reads():
    got = bytearray()

    async def read_all(reader, writer):
        while chunk := await reader.read(65536):
            got.extend(chunk)

    server, stream = await _fake_camera(read_all)
    async with server:
        await play_alaw(stream, b"\xd5" * 800)
    assert got.count(b"audio/mp2t") == 6  # 番組表と、20ms ぶんが5つ


async def test_segments_come_from_memory_while_camera_refuses():
    from types import SimpleNamespace

    from tapo_c220.sdcard import SdCard, SdCardError
    from tapo_c220.tapo.api import TapoApiError

    class Api:
        busy = False

        async def call(self, work):
            if self.busy:  # 録画を取り出している間のカメラの応答
                raise TapoApiError('カメラの API を呼べなかった（Error: USER_ID_FULL, "error_code": -71101）')
            return work(SimpleNamespace(getRecordings=lambda date: [
                {"search_video_results_1": {"startTime": 200, "endTime": 300, "vedio_type": 2}},
                {"search_video_results_0": {"startTime": 100, "endTime": 200, "vedio_type": 1}}]))

    api = Api()
    sd = SdCard(SimpleNamespace(api=api, stream=None))
    first = await sd.segments("20261009")
    assert [(s.start, s.kind) for s in first] == [(100.0, "continuous"), (200.0, "event")]
    api.busy = True
    assert await sd.segments("20261009") == first
    with pytest.raises(SdCardError, match="再生・保存している間"):
        await sd.segments("20261010")


def test_cli_arguments_and_values():
    from tapo_c220 import cli

    args = cli._parser().parse_args(["sd", "export", "2026-10-09 15:08:45", "30", "out.mp4"])
    assert (args.command, args.action, args.seconds) == ("sd", "export", 30.0)
    assert cli._timestamp("1791558525") == 1791558525.0
    assert cli._local(cli._timestamp("2026-10-09 15:08:45")) == "2026-10-09 15:08:45"
    with pytest.raises(ValueError, match="時刻"):
        cli._timestamp("yesterday")
    assert cli._SETTERS["day-night"][0]("night") == "on" and cli._SETTERS["mic-mute"][0]("off") is False
    with pytest.raises(ValueError):
        cli._SETTERS["brightness"][0]("101")
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["set", "no-such-setting", "on"])


async def test_cli_writes_settings():
    from types import SimpleNamespace

    from tapo_c220 import cli

    calls = []

    class Settings:
        def __getattr__(self, name):
            async def record(*args, **kwargs):
                calls.append((name, args, kwargs))
            return record

    cam = SimpleNamespace(settings=Settings())
    for argv in (["set", "day-night", "day"], ["set", "sharpness", "70"], ["set", "osd-week", "on"],
                 ["detect", "person", "on", "--sensitivity", "40"], ["detect", "bark", "off"]):
        await cli._settings(cam, cli._parser().parse_args(argv))
    assert calls == [("set_day_night", ("off",), {}), ("set_image", (), {"sharpness": 70}),
                     ("set_osd", (), {"week": True}), ("set_detection", ("person", True, 40), {}),
                     ("set_detection", ("bark", False, None), {})]


def test_retimer_smooths_sender_report_jumps():
    from tapo_c220.packets import Retimer

    retime = Retimer()
    # 40ms ごとに進むはずの映像の時刻が、0.35 秒戻っては 0.42 秒飛ぶ（C220 の RTCP のずれ）
    out = [retime(pts) for pts in (5.00, 5.04, 5.08, 4.73, 4.77, 5.19, 5.23)]
    assert out == pytest.approx([0, 0.04, 0.08, 0.12, 0.16, 0.20, 0.24])


async def test_live_packets_stop_when_reader_is_too_slow(monkeypatch):
    from types import SimpleNamespace

    from tapo_c220 import packets

    monkeypatch.setattr(packets, "_BACKLOG", 5)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_soon_threadsafe", lambda callback, *args: callback(*args))
    video = SimpleNamespace(type="video", codec_context=SimpleNamespace(name="h264"))

    class Raw:
        size, time_base, is_keyframe, stream = 3, 1, False, video

        def __init__(self, pts):
            self.pts = pts

        def __bytes__(self):
            return b"abc"

    closed = []
    container = SimpleNamespace(streams=SimpleNamespace(video=[video], audio=[]), close=lambda: closed.append(True),
                                demux=lambda *streams: (Raw(i / 30) for i in range(100)))
    live = packets.LivePackets(container)
    assert (await anext(live)).data == b"abc"   # 読み始めると、カメラは 100 個を一気に送ってくる
    await asyncio.sleep(0.1)
    with pytest.raises(MediaError, match="遅すぎ"):
        async for _ in live:
            pass
    assert closed and live._queue.qsize() < 10


async def test_inverted_camera_swaps_directions_and_room_position():
    camera = TapoC220(CFG, inverted=True)
    client, sent = _client(lambda _: httpx.Response(200, text=_envelope("<tptz:OkResponse/>")))
    camera.onvif = client
    await camera.step("up", 0.1)          # 逆さ：up を指示すると下向きの指示になる
    assert 'y="-0.100000"' in sent[-1]
    await camera.step("left", 0.1)        # left → right の指示（位置の pan は負）
    assert 'x="-0.100000"' in sent[-1]
    await camera.move("right", 0.4)
    assert 'x="-0.400000"' in sent[-1]
    assert camera.room_position(PtzPosition(0.5, -0.2)) == PtzPosition(-0.5, 0.2)
    assert TapoC220(CFG, inverted=False).room_position(PtzPosition(0.5, -0.2)) == PtzPosition(0.5, -0.2)


async def test_detect_inverted_reads_flip_and_explicit_wins():
    class Settings:
        def __init__(self, value=None, error=None):
            self.value, self.error, self.calls = value, error, 0

        async def flip(self):
            self.calls += 1
            if self.error:
                raise self.error
            return self.value

    camera = TapoC220(CFG)
    assert camera.inverted is None
    camera._settings = Settings(True)
    assert await camera.detect_inverted() is True and camera.inverted is True
    explicit = TapoC220(CFG, inverted=False)
    explicit._settings = Settings(True)
    assert await explicit.detect_inverted() is False and explicit._settings.calls == 0
    broken = TapoC220(CFG)
    broken._settings = Settings(error=OSError("down"))
    assert await broken.detect_inverted() is False and broken.inverted is None
