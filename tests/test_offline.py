"""カメラ無しで確かめられる部分（設定の読み込み、SOAP の組み立てと解析、TS の組み立て、向きの対応）。"""

import httpx
import pytest

from tapo_c220 import OnvifClient, OnvifError, TapoC220, TapoConfig
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
    await camera.move("right", 0.4)
    assert 'x="0.400000" y="0.000000"' in sent[-1]
    await camera.step("right", 0.1)
    assert 'x="-0.100000" y="0.000000"' in sent[-1]
    await camera.step("up", 0.1)
    assert 'y="0.100000"' in sent[-1]
    with pytest.raises(ValueError):
        await camera.move("sideways")


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
