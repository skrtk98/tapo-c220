"""カメラ無しで確かめられる部分（設定の読み込み、SOAP の組み立てと解析、TS の組み立て、向きの対応）。"""

import httpx
import pytest

from tapo_c220 import Direction, OnvifClient, OnvifError, TapoC220, TapoConfig
from tapo_c220.onvif import redact
from tapo_c220.talk import Muxer, _reply_params, crc32_mpeg, digest_password, header_packets

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


async def test_webrtc_retimes_sender_report_jumps():
    """カメラの pts は約 0.35 秒戻っては約 0.42 秒飛ぶ。付け直した時刻は、途切れなく進む。"""
    from fractions import Fraction

    import av
    from aiortc import MediaStreamTrack

    from tapo_c220.webrtc import _Retimed

    class Source(MediaStreamTrack):
        def __init__(self, kind, time_base, size, stamps):
            super().__init__()
            self.kind, self._packets = kind, []
            for pts in stamps:
                packet = av.Packet(size)
                packet.pts, packet.time_base = pts, time_base
                self._packets.append(packet)

        async def recv(self):
            return self._packets.pop(0)

    frame = 3000  # 90kHz で 1/30 秒
    video = _Retimed(Source("video", Fraction(1, 90000), 10,
                            [50000, 50000 + frame, 50000 + 2 * frame - 31500, 50000 + 3 * frame - 31500,
                             50000 + 4 * frame + 6300]))
    assert [(await video.recv()).pts for _ in range(5)] == [0, frame, 2 * frame, 3 * frame, 4 * frame]
    audio = _Retimed(Source("audio", Fraction(1, 8000), 512, [9000, 9512, 6900, 7412]))
    assert [(await audio.recv()).pts for _ in range(4)] == [0, 512, 1024, 1536]


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


def test_sdplayer_audio_frames():
    """録画の G.711 を PCM に戻し、20ms ずつのフレームにして音声のトラックへ渡す。"""
    from tapo_c220.sdplayer import SdPlayer, _g711_table

    assert _g711_table("mulaw")[0xFF] == 0 and _g711_table("mulaw")[0x00] == -32124 and _g711_table("mulaw")[0x80] == 32124
    assert _g711_table("alaw")[0xD5] == 8 and _g711_table("alaw")[0x55] == -8
    player = SdPlayer(None, 1000.0)
    player._ts.codec = ("mulaw", 16000)
    player._push_audio(b"\xff" * 500)   # 320 標本で1フレーム。余りの 180 は次に回る
    player._push_audio(b"\xff" * 200)
    frames = [player.audio.queue.get_nowait() for _ in range(2)]
    assert [(f.samples, f.sample_rate, f.pts) for f in frames] == [(320, 16000, 0), (320, 16000, 320)]
    assert player.audio.queue.empty() and player.position == 1000.0
