"""Tapo 独自の制御 API（443 番）への窓口。ログインと暗号化の手順が込み入っているので pytapo に任せる。

pytapo は同期の HTTP なので、1つずつ別スレッドで呼ぶ。認証は Tapo アカウント（クラウド）のパスワード（ユーザは admin）。
呼び出しを取り消しても始まった要求は止められず、カメラに届いて設定が変わることがある。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pytapo import Tapo



class TapoApiError(RuntimeError):
    """カメラの API を呼べなかった、またはカメラが要求を拒んだ。"""


class TapoApi:
    def __init__(self, host: str, cloud_password: str):
        self.host, self._password = host, cloud_password
        self._client: Tapo | None = None
        self._lock = asyncio.Lock()

    def _connect(self) -> Tapo:
        if not self._password:
            raise TapoApiError("TAPO_CLOUD_PASSWORD（Tapo アカウントのパスワード）が設定されていない")
        if self._client is None:
            self._client = Tapo(self.host, "admin", self._password, self._password, printWarnInformation=False)
        return self._client

    async def call(self, work: Callable[[Tapo], Any]) -> Any:
        """pytapo のクライアントを渡して work を呼ぶ。失敗したら、次はログインからやり直す。

        取り消しても始まった要求は止められず、終わるまで待つ。カメラに届いて設定が変わることがある。
        """
        async with self._lock:
            task = asyncio.ensure_future(asyncio.to_thread(lambda: work(self._connect())))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                try:
                    task.result()
                except (TapoApiError, asyncio.CancelledError):
                    pass
                except Exception:
                    self._client = None
                raise
            except TapoApiError:
                raise
            except Exception as e:  # pytapo は素の Exception を投げる
                self._client = None
                raise TapoApiError(f"カメラの API を呼べなかった（{str(e)[:200] or type(e).__name__}）") from None

    async def execute(self, method: str, params: dict) -> dict:
        """API のメソッドを名前で直接呼ぶ（このライブラリが包んでいない設定を触るとき）。"""
        return await self.call(lambda api: api.executeFunction(method, params))

    def close(self) -> None:
        self._client = None
