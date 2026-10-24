"""TP-Link Tapo C220 を RTSP・ONVIF（Profile S）・Tapo 独自の双方向音声で操作する。"""

from .facade import Direction, TapoC220
from .config import TapoConfig
from .events import Event
from .media import MAIN, SUB, MediaError, Recording
from .onvif import OnvifClient, OnvifError, Preset, Profile, PtzPosition
from .tapo import StreamError, TapoSession, TapoStream
from .talk import TalkError, TapoTalk

__all__ = [
    "MAIN", "SUB", "Direction", "Event", "MediaError", "OnvifClient", "OnvifError", "Preset", "Profile",
    "PlaybackInterrupted", "PtzPosition", "Recording", "StreamError", "TalkError", "TapoC220", "TapoConfig",
    "TapoSession", "TapoStream", "TapoTalk",
]


def __getattr__(name: str):
    if name == "PlaybackInterrupted":  # microSD の依存を使わない利用者には cryptography を要求しない
        from .sdcard import PlaybackInterrupted
        return PlaybackInterrupted
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
