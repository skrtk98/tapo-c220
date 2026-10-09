# tapo-c220-survey

TP-Link Tapo C220 を RTSP と ONVIF（Profile S）で操作できる範囲を実機で調べ、その結果をライブラリ `tapo_c220` と薄い WebUI にまとめたもの。

調べた個体は Tapo C220（ハードウェア 2.0、ファームウェア 1.3.1 Build 260515）。

## 使い方

`ffmpeg` が要る。接続先と認証情報は `.env`（または環境変数）に書く。

```
TAPO_HOST=192.168.x.x
TAPO_USER=...            # カメラアカウント（Tapo アプリ →「高度な設定」→「カメラのアカウント」）。RTSP と ONVIF 用
TAPO_PASSWORD=...
TAPO_CLOUD_PASSWORD=...  # Tapo アカウント（クラウド）のパスワード。スピーカーで鳴らすときだけ要る
```

```sh
uv sync
uv run tapo-c220-web            # http://127.0.0.1:8220 （--host / --port で変更）
uv run tapo-c220 info           # コマンドラインから。ほかに snapshot / record / play / move / step / goto / position
uv run pytest                   # カメラ無しで動くテスト
```

WebUI には認証が無い。既定では手元（127.0.0.1）でしか待ち受けない。録音・録画は `recordings/`（`TAPO_RECORD_DIR`）に保存する。

ライブラリとして使う例：

```python
from tapo_c220 import TapoC220

async with TapoC220() as cam:                 # .env を読む
    jpeg = await cam.snapshot()               # 静止画（2560×1440）
    rec = await cam.record("out.wav")         # 録音。kind="video" で録画（MP4）
    await cam.move("left", speed=0.5)         # 動かし続ける
    await cam.stop()
    await cam.step("up", 0.05)                # 決まった量だけ動かす
    await cam.play("hello.mp3")               # カメラのスピーカーで鳴らす
    await rec.stop()
    async for chunk in cam.live_mjpeg():      # JPEG の連続（multipart/x-mixed-replace）
        ...
```

ブラウザへのライブ配信は `tapo_c220.webrtc.WebRtcSessions`（aiortc が要る。`[webui]` に含まれる）：

```python
sessions = WebRtcSessions(cam.cfg)
answer = await sessions.answer(offer_sdp, stream="stream2")   # ブラウザの offer を渡し、answer.sdp を返す
```

| モジュール | 役割 |
| --- | --- |
| `tapo_c220.camera` | 窓口の `TapoC220`。向き（left / right / up / down）を画面の動きに合わせる |
| `tapo_c220.onvif` | ONVIF の最小限のクライアント（SOAP を手で組む。依存は httpx だけ） |
| `tapo_c220.media` | RTSP の受信と変換（ffmpeg）。静止画、ライブ配信、録音・録画 |
| `tapo_c220.webrtc` | ライブ映像と音声を WebRTC でブラウザへ送る（aiortc） |
| `tapo_c220.talk` | Tapo 独自の双方向音声（スピーカー） |
| `tapo_c220.webui` | FastAPI の WebUI（`TapoC220` を HTTP から呼ぶだけ） |

## 調査結果

### どの機能を、どの経路で実現できたか

| 機能 | 経路 | 結果 |
| --- | --- | --- |
| 映像・音声のストリーミング | RTSP（554 番） | できる |
| 録音・録画 | RTSP を ffmpeg で保存 | できる |
| 静止画 | RTSP から1枚切り出す | できる（ONVIF の GetSnapshotUri は無い） |
| 首振り（上下左右） | ONVIF PTZ（2020 番） | できる（絶対・相対・連続・停止・プリセット） |
| スピーカーからの再生 | **RTSP/ONVIF では不可**。Tapo 独自の双方向音声（8800 番） | できる（非公式のプロトコル） |

開いているポートは 443・554・2020・8800。

### RTSP

- URL は `rtsp://ユーザ:パスワード@ホスト:554/stream1`。認証は Digest、カメラアカウントを使う。
- `stream1` は H.264 High 2560×1440、`stream2` は H.264 High 640×360。音声はどちらも G.711 A-law の 8kHz モノラル。
  ONVIF は音声を 16kHz と答えるが、SDP も実際の中身も 8kHz。
- `stream8` は MJPEG 640×360（ONVIF の profile_3「jpegStream」）。ただし5秒間に届いた約300枚のうち別の画像は5枚だけで、
  同じ画像を送り直してくる（約 25Mbps）。Chromium ではこの JPEG が灰色にしか描かれなかった。
  このため WebUI の MJPEG 表示は、`stream2` を ffmpeg で JPEG に変換している。
- 同時に受けられるのは3接続まで。4本めは接続時に拒まれる（同じストリーム3本でも、別々のストリームでも同じ）。
- 音声のトラックだけを SETUP すると（ffmpeg の `-allowed_media_types audio`）、何も届かず時間切れになる。
  録音するときも映像ごと受け取って捨てる。
- **RTCP の時刻が乱れている。** RTP のタイムスタンプは正しく進み、パケットも滑らかに届く（30fps、到着の間隔は中央値 33ms・最大 80ms 弱、
  ping は 2ms 前後で損失なし）。しかし約 0.5 秒ごとに届く RTCP の Sender Report の NTP 時刻が、交互に約 ±0.38 秒ずれている。
  libav（ffmpeg）はこれに合わせて pts を直すので、pts が約 0.35 秒戻っては約 0.42 秒飛ぶことを毎秒くり返す。
  そのまま使うと、WebRTC ではブラウザが映像を毎秒止め（20 秒で 20 回・計 7 秒）、音声の約 43% を補間で埋めた。MP4 の録画も映像が毎秒つかえる。
  対策として、WebRTC ではタイムスタンプを付け直し（音声は標本の数、映像は飛びを普通の進み幅に置き換える）、
  ffmpeg では届いた時刻を使う（`-use_wallclock_as_timestamps 1`）。対策後は、止まり 0 回・補間 0%・30fps。
- カメラへ音声を送るトラックは無い。`Require: www.onvif.org/ver20/backchannel` をつけて DESCRIBE しても、SDP は映像と音声（受信）の2本だけ。

### ONVIF

- 入口は `http://ホスト:2020/onvif/service`。認証は WS-Security の UsernameToken（PasswordDigest）だけ。
- サービスは device・media（ver10）・events・analytics・imaging・ptz・deviceIO。版はどれも 2.20。
- プロファイルは `profile_1`（stream1）、`profile_2`（stream2）、`profile_3`（stream8）の3つで、どれにも PTZ がつく。
- 対応しない操作は `ter:ActionNotSupported` が返る：GetSnapshotUri、GotoHomePosition。
- GetAudioOutputs と GetAudioDecoderConfigurations は空。deviceIO の能力は AudioOutputs="1" と答えるが、使える出力は無い。
- イベントは PullPoint 対応で、動体・いたずら・ライン横断・人物・TPSmartEvent のトピックがある（中身は未検証）。

### PTZ

- 位置・移動量・速度はどれも -1〜1 の正規化した値。ズームは無い。プリセットは8件まで（保存・移動・削除を確認）。
- **pan の向きが、速度と位置で逆。** ContinuousMove の速度は正の pan で画面が右へ動くが、
  位置（GetStatus・AbsoluteMove・RelativeMove）は正の pan で左を向く。tilt はどちらも正で上。
  （この個体を据え置きで使ったときの実測。画像の反転設定で変わるかは未検証。）
- ContinuousMove の Timeout は整数の秒だけ。`PT0.5S` を送ると、カメラの HTTP サーバが壊れた応答（500）を返す。
  ライブラリは秒を切り上げて送る。Timeout を省くとカメラの既定（180 秒）まで動き続けるので、WebUI は 5 秒を指定し、
  押している間は 3 秒ごとに送り直す。
- 1秒間動かしたときの位置の変化は、pan が速度 0.1 / 0.5 / 1.0 で約 0.06 / 0.15 / 0.20、tilt が速度 0.5 で約 0.71
  （指示の往復の時間を含む粗い値）。tilt は正規化した値に対して速い。
- 移動の指示はすぐ応答が返る。GetStatus の位置は動いている途中も変わるが、MoveStatus は常に UNKNOWN なので、
  動き終えたかは位置が変わらなくなったことで判断する。動いている途中に次の移動を送ると、前の移動は打ち切られる。
- 範囲外の AbsoluteMove は `ter:InvalidPosition` で拒まれる。

### スピーカー

RTSP にも ONVIF にも音声を送る経路が無いので、Tapo アプリが使う独自の双方向音声を使う（手順は go2rtc の `pkg/tapo` を参考にした）。

1. 8800 番の `POST /stream` を Digest 認証する。ユーザは `admin`、パスワードは **Tapo アカウントのパスワード**の
   SHA256（大文字の16進）。カメラアカウントのパスワードではない。
2. `{"params":{"talk":{"mode":"aec"},"method":"get"}}` を送り、`session_id` を受け取る。
3. G.711 A-law（8kHz）を MPEG-TS に包み、20ms ずつ実時間の速さで multipart として送る。

カメラから受信の確認は返らない。テスト音を鳴らしながらカメラのマイクで録音し、再生の時刻に音量が上がることは確かめた。
非公式のプロトコルなので、ファームウェアの更新で使えなくなる可能性がある。

## WebUI

- ライブ映像：既定は WebRTC（640×360 / 2560×1440、音あり）。カメラの H.264 と G.711 A-law は WebRTC の標準のコーデックなので、
  変換せずそのまま RTP に載せ替えている。合図は `POST /api/webrtc/offer` の1往復だけで、STUN/TURN は使わない（同じ LAN の中で使う前提）。
  切れたら2秒後につなぎ直す。Chromium で両方の解像度の再生を確認した（遅れの実測はしていない）。
  カメラは High プロファイルで送ってくるが、ブラウザと取り決めるのは Baseline（`42001f`）のまま。Chromium は復号できたが、
  ほかのブラウザでは未確認。
- 代わりの表示：MJPEG（640×360、音なし）、音だけ（MP3）。どちらも HTTP の応答を流し続けるだけで、遅れの制御や再接続は無い。
- WebRTC・MJPEG・音だけの表示・録音・録画は、それぞれ RTSP を1本使う。カメラの上限は合わせて3本。
- 首振り：押している間だけ動く（矢印キーでも可）か、1回押すと少し動くかを選べる。プリセットの保存・移動・削除。
- 録音（WAV）・録画（MP4）：開始と停止、保存したファイルの再生・ダウンロード・削除。同時には1つ、最長1時間。
- スピーカー：音声ファイルを送る、またはブラウザのマイクで録って送る（リアルタイムの通話ではない）。
