"""Tapo 独自のプロトコル。RTSP・ONVIF（カメラアカウントで認証）と違い、Tapo アカウント（クラウド）のパスワードで認証する。

経路は2つあり、それぞれに窓口がある：

- 443 番の制御 API（暗号化した JSON を1往復ずつ）：`api.TapoApi`。設定（settings）と、録画の一覧（sdcard）が使う。pytapo が要る。
- 8800 番のストリーム（1本の接続に流し続ける）：`stream.TapoStream`。スピーカー（talk）と、録画の取り出し（sdcard）が使う。

`TapoSession` が認証情報を持ち、2つの窓口を渡す。
"""

from __future__ import annotations

from ..config import TapoConfig
from .stream import StreamError, TapoStream


class TapoSession:
    def __init__(self, cfg: TapoConfig | None = None):
        self.cfg = cfg or TapoConfig.load()
        self.stream = TapoStream(self.cfg.host, self.cfg.cloud_password, port=self.cfg.talk_port)
        self._api = None

    @property
    def available(self) -> bool:
        """Tapo アカウントのパスワードが設定されている（無ければ、どちらの経路も使えない）。"""
        return self.stream.available

    @property
    def api(self):
        """443 番の制御 API（api.TapoApi）。pytapo が要る（`tapo-c220-survey[tapo]`）。"""
        if self._api is None:
            from .api import TapoApi
            self._api = TapoApi(self.cfg.host, self.cfg.cloud_password)
        return self._api


__all__ = ["StreamError", "TapoSession", "TapoStream"]
