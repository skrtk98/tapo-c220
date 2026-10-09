"""ONVIF（Profile S）の最小限のクライアント。SOAP を手で組み立てて送る。

C220 で実測したこと（ファームウェア 1.3.1）：

- 2020 番の `/onvif/service` に全サービスがある。認証は WS-Security の UsernameToken（PasswordDigest）だけで、HTTP Digest は使えない。
- PTZ は AbsoluteMove・RelativeMove・ContinuousMove・Stop・プリセット（8件まで）が使える。GotoHomePosition は無い。
- ContinuousMove の Timeout は整数の秒（`PT1S`）だけ。小数（`PT0.5S`）を送ると、カメラの HTTP サーバが壊れた応答を返す。
- GetStatus の位置は動いている途中も刻々と変わるが、MoveStatus は常に UNKNOWN。動き終えたかは、位置が変わらなくなったことで判断する。
- 動いている途中に次の移動を送ると、前の移動は打ち切られる。
- GetSnapshotUri は無い（静止画は RTSP から切り出す）。GetAudioOutputs は空で、スピーカーへ送る経路は ONVIF に無い。
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import httpx

_NAMESPACES = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
_XMLNS = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in _NAMESPACES.items())
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
_BASE64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class OnvifError(RuntimeError):
    """カメラが SOAP Fault を返した、または応答を読めなかった。code は `ter:ActionNotSupported` のような細分コード。"""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Profile:
    token: str
    name: str
    encoding: str
    width: int
    height: int
    ptz: bool


@dataclass(frozen=True)
class PtzPosition:
    pan: float   # -1〜1
    tilt: float  # -1〜1


@dataclass(frozen=True)
class Preset:
    token: str
    name: str
    position: PtzPosition | None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    """名前空間を無視して、子孫から最初の要素を探す（カメラごとに接頭辞も名前空間の版も揺れるため）。"""
    return next((e for e in element.iter() if _local(e.tag) == name), None)


def _findall(element: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in element.iter() if _local(e.tag) == name]


def _text(element: ET.Element, name: str, default: str = "") -> str:
    found = _find(element, name)
    return (found.text or "").strip() if found is not None else default


def _position(element: ET.Element) -> PtzPosition | None:
    pan_tilt = _find(element, "PanTilt")
    if pan_tilt is None or "x" not in pan_tilt.attrib:
        return None
    return PtzPosition(float(pan_tilt.attrib["x"]), float(pan_tilt.attrib["y"]))


def _vector(pan: float, tilt: float) -> str:
    return f'<tt:PanTilt x="{pan:.6f}" y="{tilt:.6f}"/>'


class OnvifClient:
    def __init__(self, host: str, user: str, password: str, *, port: int = 2020, timeout: float = 10.0,
                 profile: str = "profile_1"):
        self.url = f"http://{host}:{port}/onvif/service"
        self.user, self._password = user, password
        self.profile = profile
        self._http = httpx.AsyncClient(timeout=timeout)
        self._clock_offset: timedelta | None = None  # カメラの時計 − 手元の時計

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- SOAP -------------------------------------------------------------------------------------------------------

    def _security(self) -> str:
        nonce = os.urandom(16)
        created = (datetime.now(UTC) + (self._clock_offset or timedelta())).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()).decode()
        return (f'<s:Header><Security xmlns="{_WSSE}"><UsernameToken><Username>{escape(self.user)}</Username>'
                f'<Password Type="{_DIGEST}">{digest}</Password>'
                f'<Nonce EncodingType="{_BASE64}">{base64.b64encode(nonce).decode()}</Nonce>'
                f'<Created xmlns="{_WSU}">{created}</Created></UsernameToken></Security></s:Header>')

    async def _post(self, body: str, *, auth: bool) -> ET.Element:
        envelope = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {_XMLNS}>'
                    f'{self._security() if auth else ""}<s:Body>{body}</s:Body></s:Envelope>')
        try:
            response = await self._http.post(self.url, content=envelope.encode(),
                                             headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except httpx.HTTPError as e:
            raise OnvifError(f"カメラの ONVIF に届かない（{type(e).__name__}）") from None
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise OnvifError(f"ONVIF の応答を読めない（HTTP {response.status_code}）") from None
        fault = _find(root, "Fault")
        if fault is not None:
            codes = [(e.text or "").strip() for e in _findall(fault, "Value")]
            reason = _text(fault, "Text")
            raise OnvifError(f"ONVIF の要求が拒まれた（{' / '.join(codes)}{'：' + reason if reason else ''}）",
                             code=codes[-1] if codes else "")
        payload = _find(root, "Body")
        if payload is None or len(payload) == 0:
            raise OnvifError("ONVIF の応答に中身が無い")
        return payload[0]

    async def call(self, body: str) -> ET.Element:
        """認証つきで要求を1つ送り、応答の Body の中身を返す。body は `<tds:GetDeviceInformation/>` のような要素。"""
        if self._clock_offset is None:
            await self._sync_clock()
        return await self._post(body, auth=True)

    async def _sync_clock(self) -> None:
        """カメラの時計とのずれを測る。PasswordDigest の Created が大きくずれると認証に失敗するため。"""
        self._clock_offset = timedelta()
        try:
            utc = _find(await self._post("<tds:GetSystemDateAndTime/>", auth=False), "UTCDateTime")
            fields = {name: int(_text(utc, name)) for name in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
            camera = datetime(fields["Year"], fields["Month"], fields["Day"], fields["Hour"], fields["Minute"],
                              fields["Second"], tzinfo=UTC)
            self._clock_offset = camera - datetime.now(UTC)
        except (OnvifError, TypeError, ValueError):
            pass  # 測れなければ手元の時計のまま試す

    # --- 機器とメディア -----------------------------------------------------------------------------------------------

    async def device_information(self) -> dict[str, str]:
        info = await self.call("<tds:GetDeviceInformation/>")
        return {_local(e.tag): (e.text or "").strip() for e in info}

    async def services(self) -> dict[str, str]:
        """サービスの名前空間 → 版。"""
        reply = await self.call("<tds:GetServices><tds:IncludeCapability>false</tds:IncludeCapability></tds:GetServices>")
        return {_text(s, "Namespace"): f"{_text(s, 'Major')}.{_text(s, 'Minor')}" for s in _findall(reply, "Service")}

    async def profiles(self) -> list[Profile]:
        reply = await self.call("<trt:GetProfiles/>")
        result = []
        for p in _findall(reply, "Profiles"):
            encoder = _find(p, "VideoEncoderConfiguration")
            resolution = _find(encoder, "Resolution") if encoder is not None else None
            result.append(Profile(
                token=p.attrib.get("token", ""), name=_text(p, "Name"),
                encoding=_text(encoder, "Encoding") if encoder is not None else "",
                width=int(_text(resolution, "Width", "0")) if resolution is not None else 0,
                height=int(_text(resolution, "Height", "0")) if resolution is not None else 0,
                ptz=_find(p, "PTZConfiguration") is not None,
            ))
        return result

    async def stream_uri(self, profile: str | None = None) -> str:
        """RTSP の URL（認証情報は含まない）。"""
        reply = await self.call(
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"{self._profile(profile, 'trt')}</trt:GetStreamUri>")
        return _text(reply, "Uri")

    # --- PTZ --------------------------------------------------------------------------------------------------------

    def _profile(self, profile: str | None = None, prefix: str = "tptz") -> str:
        return f"<{prefix}:ProfileToken>{escape(profile or self.profile)}</{prefix}:ProfileToken>"

    async def ptz_status(self) -> PtzPosition:
        position = _position(await self.call(f"<tptz:GetStatus>{self._profile()}</tptz:GetStatus>"))
        if position is None:
            raise OnvifError("PTZ の位置を読めない")
        return position

    async def continuous_move(self, pan: float, tilt: float, timeout: float | None = None) -> None:
        """速度（-1〜1）を与えて動かし続ける。stop() か timeout（秒。カメラが整数しか受けないので切り上げる）で止まる。"""
        limit = f"<tptz:Timeout>PT{max(1, math.ceil(timeout))}S</tptz:Timeout>" if timeout else ""
        await self.call(f"<tptz:ContinuousMove>{self._profile()}<tptz:Velocity>{_vector(pan, tilt)}</tptz:Velocity>"
                        f"{limit}</tptz:ContinuousMove>")

    async def relative_move(self, pan: float, tilt: float) -> None:
        """今の位置から、正規化した座標（全域が -1〜1）でずらす。"""
        await self.call(f"<tptz:RelativeMove>{self._profile()}<tptz:Translation>{_vector(pan, tilt)}</tptz:Translation>"
                        "</tptz:RelativeMove>")

    async def absolute_move(self, pan: float, tilt: float) -> None:
        await self.call(f"<tptz:AbsoluteMove>{self._profile()}<tptz:Position>{_vector(pan, tilt)}</tptz:Position>"
                        "</tptz:AbsoluteMove>")

    async def stop(self) -> None:
        await self.call(f"<tptz:Stop>{self._profile()}<tptz:PanTilt>true</tptz:PanTilt></tptz:Stop>")

    async def presets(self) -> list[Preset]:
        reply = await self.call(f"<tptz:GetPresets>{self._profile()}</tptz:GetPresets>")
        return [Preset(p.attrib.get("token", ""), _text(p, "Name"), _position(p)) for p in _findall(reply, "Preset")]

    async def set_preset(self, name: str) -> str:
        """今の位置をプリセットとして覚える。トークンを返す。"""
        reply = await self.call(f"<tptz:SetPreset>{self._profile()}<tptz:PresetName>{escape(name)}</tptz:PresetName>"
                                "</tptz:SetPreset>")
        return _text(reply, "PresetToken")

    async def goto_preset(self, token: str) -> None:
        await self.call(f"<tptz:GotoPreset>{self._profile()}<tptz:PresetToken>{escape(token)}</tptz:PresetToken>"
                        "</tptz:GotoPreset>")

    async def remove_preset(self, token: str) -> None:
        await self.call(f"<tptz:RemovePreset>{self._profile()}<tptz:PresetToken>{escape(token)}</tptz:PresetToken>"
                        "</tptz:RemovePreset>")


def redact(text: str, *secrets: str) -> str:
    """ログやエラーに出す前に、認証情報を伏せる。"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return re.sub(r"(rtsp://)[^@/\s]+@", r"\1***@", text)
