"""検知のイベントを ONVIF で受け取る。カメラから、こちらが開けた HTTP の口へ通知を送らせる（WS-BaseNotification の Subscribe）。

C220 で実測したこと（ファームウェア 1.3.1）：

- PullPoint（CreatePullPointSubscription → PullMessages）は使えない。購読は作れるが、PullMessages を送ると
  カメラが何も返さずに接続を切る（WS-Addressing の見出しや Timeout の書き方を変えても同じ）。
- Subscribe で通知の宛先（こちらの URL）を渡すと、カメラがそこへ Notify を POST してくる。認証は付いてこない。
- トピックは RuleEngine/CellMotionDetector/Motion（と VideoSource/MotionAlarm）・PeopleDetector/People・
  TamperDetector/Tamper・LineCrossDetector/LineCross・TPSmartEventDetector/TPSmartEvent。
  検知している間は真（IsMotion="true" など）を約1秒ごとに送り続け、終わると偽を1回送る。
- 通知に付いている時刻（UtcTime）は当てにならない。人物の検知では、最初に検知したときの時刻のまま変わらなかった
  （カメラ自身の検知の記録と照らすと、通知そのものは検知の約1秒後に届いている）。時刻は、届いた時点の手元の時計で付ける。
- カメラの設定でその検知をオンにしていないと、何も届かない（settings.CameraSettings.set_detection）。
  人物の検知は、動体の検知がオフでも届く。
"""

from __future__ import annotations

import asyncio
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

from .onvif import OnvifClient, OnvifError, _find, _findall, _text

_WSN = "http://docs.oasis-open.org/wsn/b-2"
_WSA = "http://www.w3.org/2005/08/addressing"
_LIFETIME = 120          # 購読の寿命（秒）。この半分ごとに延ばす
_BODY_MAX = 256 * 1024   # 通知1つの大きさの上限
_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

# トピックの末尾 → 検知の種類
_KINDS = {"Motion": "motion", "MotionAlarm": "motion", "Tamper": "tamper", "LineCross": "line_crossing",
          "People": "person", "TPSmartEvent": "smart"}


@dataclass(frozen=True)
class Event:
    kind: str       # motion / tamper / line_crossing / person / smart。知らないトピックは、その末尾の名前
    active: bool    # 検知している（真）か、終わった（偽）か
    time: float     # 通知が届いた時刻（UNIX 時刻、手元の時計）
    topic: str      # tns1:RuleEngine/CellMotionDetector/Motion のような、元のトピック


def parse_messages(root: ET.Element) -> list[Event]:
    """Notify（や PullMessagesResponse）から、イベントを取り出す。"""
    found = []
    for notification in _findall(root, "NotificationMessage"):
        topic = _text(notification, "Topic")
        outer = _find(notification, "Message")
        message = next((e for e in outer.iter() if e is not outer and e.tag.endswith("Message")), None) \
            if outer is not None else None
        data = _find(message, "Data") if message is not None else None
        if data is None:
            continue
        values = [item.get("Value", "").lower() for item in _findall(data, "SimpleItem")]
        name = topic.rsplit("/", 1)[-1]
        found.append(Event(_KINDS.get(name, name), any(v in ("true", "1") for v in values), time.time(), topic))
    return found


def _addresses(host: str) -> tuple[str, str]:
    """カメラの IP アドレスと、カメラから見えるこちらの IP アドレス。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 9))  # UDP なので何も送らない。経路だけ決まる
        return sock.getpeername()[0], sock.getsockname()[0]


async def events(onvif: OnvifClient, *, changes_only: bool = True, port: int = 0) -> AsyncIterator[Event]:
    """検知のイベントを、やめるまで受け取り続ける。購読は自動で延ばし、やめるときに解約する。

    カメラからの通知を受けるために、こちらの LAN 側のアドレスで HTTP の口を開ける（port が 0 なら空いている番号）。
    カメラの IP アドレス以外からの接続は受けない。ファイアウォールでカメラからの着信を塞いでいると、何も届かない。
    changes_only が真なら、検知が始まったときと終わったときだけを返す（偽なら、カメラが送る約1秒ごとの通知をすべて返す）。
    """
    host = await asyncio.to_thread(_addresses, urlsplit(onvif.url).hostname)
    camera, local = host
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)

    async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if writer.get_extra_info("peername")[0] != camera:
                return
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            if not 0 < length <= _BODY_MAX:
                return
            body = await asyncio.wait_for(reader.readexactly(length), 5)
            writer.write(_OK)
            await writer.drain()
            for event in parse_messages(ET.fromstring(body)):
                if not queue.full():
                    queue.put_nowait(event)
        except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ET.ParseError):
            pass  # 途中で切れた、通知として読めない。捨てる
        finally:
            writer.close()

    server = await asyncio.start_server(receive, local, port)
    consumer = f"http://{local}:{server.sockets[0].getsockname()[1]}/onvif-notify"
    states: dict[str, bool] = {}
    try:
        while True:
            subscribed = await onvif.call(
                f'<wsnt:Subscribe xmlns:wsnt="{_WSN}" xmlns:wsa="{_WSA}"><wsnt:ConsumerReference><wsa:Address>{consumer}'
                f'</wsa:Address></wsnt:ConsumerReference><wsnt:InitialTerminationTime>PT{_LIFETIME}S'
                f'</wsnt:InitialTerminationTime></wsnt:Subscribe>')
            address = _text(subscribed, "Address")
            if not address:
                raise OnvifError("イベントの購読の宛先が返ってこない")
            renew_at = time.monotonic() + _LIFETIME / 2
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), max(0.0, renew_at - time.monotonic()))
                    except TimeoutError:
                        try:
                            await onvif.call(f'<wsnt:Renew xmlns:wsnt="{_WSN}"><wsnt:TerminationTime>PT{_LIFETIME}S'
                                             f'</wsnt:TerminationTime></wsnt:Renew>', url=address)
                        except OnvifError as e:
                            if e.code:  # カメラが拒んだ（再起動などで購読が消えた）。作り直す
                                break
                            raise       # カメラに届かない
                        renew_at = time.monotonic() + _LIFETIME / 2
                        continue
                    if changes_only and states.get(event.kind) == event.active:
                        continue
                    states[event.kind] = event.active
                    yield event
            finally:
                try:  # やめるとき（キャンセルを含む）は、カメラ側の購読を残さない
                    await asyncio.shield(onvif.call(f'<wsnt:Unsubscribe xmlns:wsnt="{_WSN}"/>', url=address))
                except (OnvifError, asyncio.CancelledError):
                    pass
            # 購読が消えていた間に収まった知らせは届かない。検知中だったものは、収まったことにして知らせる
            for kind in [kind for kind, active in states.items() if active]:
                yield Event(kind, False, time.time(), "")
            states.clear()
    finally:
        server.close()


__all__ = ["Event", "events", "parse_messages"]
