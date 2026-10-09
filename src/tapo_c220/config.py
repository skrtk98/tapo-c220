"""接続先と認証情報。環境変数、または `KEY=VALUE` 形式のファイル（既定は `.env`）から読む。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class TapoConfig:
    host: str
    user: str                  # カメラアカウント（Tapo アプリの「カメラのアカウント」）。RTSP と ONVIF に使う
    password: str
    cloud_password: str = ""   # Tapo アカウント（クラウド）のパスワード。8800 番の双方向音声にだけ使う
    rtsp_port: int = 554
    onvif_port: int = 2020
    talk_port: int = 8800

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = None) -> TapoConfig:
        """環境変数を優先し、無いものをファイルで補う。ファイルは `TAPO_ENV_FILE`、無ければ `./.env`。"""
        path = Path(env_file or os.environ.get("TAPO_ENV_FILE") or ".env")
        values = _read_env_file(path) if path.is_file() else {}
        values.update({k: v for k, v in os.environ.items() if k.startswith("TAPO_")})
        missing = [k for k in ("TAPO_HOST", "TAPO_USER", "TAPO_PASSWORD") if not values.get(k)]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} が設定されていない（環境変数か {path} に書くこと）")
        return cls(
            host=values["TAPO_HOST"],
            user=values["TAPO_USER"],
            password=values["TAPO_PASSWORD"],
            cloud_password=values.get("TAPO_CLOUD_PASSWORD", ""),
            rtsp_port=int(values.get("TAPO_RTSP_PORT", 554)),
            onvif_port=int(values.get("TAPO_ONVIF_PORT", 2020)),
            talk_port=int(values.get("TAPO_TALK_PORT", 8800)),
        )
