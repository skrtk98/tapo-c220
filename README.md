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
uv run tapo-c220 info           # コマンドラインから（下の表）
uv run pytest                   # カメラ無しで動くテスト
```

WebUI には認証が無い。既定では手元（127.0.0.1）でしか待ち受けない。録音・録画は `recordings/`（`TAPO_RECORD_DIR`）に保存する。

コマンドライン（`tapo-c220 <コマンド> --help` で引数が出る。結果は JSON で出す）：

| コマンド | すること |
| --- | --- |
| `info` / `position` | 機種・プロファイル・PTZ の位置 |
| `snapshot PATH` / `record PATH [--kind video] [--seconds N]` | 静止画、録音・録画 |
| `play PATH` / `siren [--seconds N]` | スピーカーで音声ファイル・サイレンを鳴らす |
| `move 向き` / `step 向き` / `goto PAN TILT` / `preset list\|save\|goto\|remove` | 首振りとプリセット |
| `settings [項目…]` | 設定を読む（省くと主なものをまとめて） |
| `set 項目 値` | 設定を1つ書く。例：`set day-night night`、`set brightness 60`、`set privacy on` |
| `detect [種類 [on\|off]] [--sensitivity N]` | 検知の設定を読む・切り替える |
| `alarm [--enabled on\|off] [--volume high] …` | 警報の設定を読む・書く |
| `events [--seconds N] [--all]` | 検知イベントを受け取り続ける（1行に1つの JSON） |
| `sd status\|days\|segments DATE\|detections DATE` | microSD の状態、録画のある日、区間、検知の記録 |
| `sd export "2026-10-09 15:08:45" 60 clip.mp4` | 録画の一部を MP4 に保存する（等速でしか進まない） |

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
    async for chunk in cam.live_audio():      # マイクの音（MP3）
        ...
```

ブラウザへのライブ配信は `tapo_c220.webrtc.WebRtcSessions`（aiortc が要る。`[webui]` に含まれる）：

```python
sessions = WebRtcSessions(cam.cfg)
answer = await sessions.answer(offer_sdp, stream="stream2")   # ブラウザの offer を渡し、answer.sdp を返す
```

| モジュール | 役割 |
| --- | --- |
| `tapo_c220.facade` | 全体の窓口の `TapoC220`。向き（left / right / up / down）を画面の動きに合わせる |
| `tapo_c220.onvif` | ONVIF の最小限のクライアント（SOAP を手で組む。依存は httpx だけ） |
| `tapo_c220.media` | RTSP の受信と変換（ffmpeg）。静止画、ライブ配信、録音・録画 |
| `tapo_c220.webrtc` | ライブ映像と音声を WebRTC でブラウザへ送る（aiortc） |
| `tapo_c220.events` | 検知のイベントを ONVIF で受け取る |
| `tapo_c220.tapo` | Tapo 独自のプロトコルへの窓口だけを置く。`TapoSession` が認証情報を持ち、下の2つを渡す |
| `tapo_c220.tapo.api` | 443 番の制御 API の窓口 `TapoApi`（pytapo を別スレッドで呼ぶ。`[tapo]` の追加の依存が要る） |
| `tapo_c220.tapo.stream` | 8800 番のストリームの窓口 `TapoStream`（接続と認証。依存なし） |
| `tapo_c220.settings` | カメラの設定の読み書き（`CameraSettings`）。`tapo.api` を使う |
| `tapo_c220.talk` | スピーカーへの音声。`tapo.stream` を使う |
| `tapo_c220.sdcard` | microSD の録画。一覧は `tapo.api`、取り出しは `tapo.stream` を使う |
| `tapo_c220.sdplayer` | microSD の録画を、指定した時刻から WebRTC で流し続ける |
| `tapo_c220.webui` | FastAPI の WebUI（`TapoC220` を HTTP から呼ぶだけ） |

## 調査結果

### 経路の分け方

| 分類 | 経路 | ポート | 通信の形 | 認証 | 使いみち |
| --- | --- | --- | --- | --- | --- |
| 標準 | RTSP | 554 | RTP を流し続ける | カメラアカウント（Digest） | 映像・音声、録音・録画、静止画 |
| 標準 | ONVIF | 2020 | SOAP を1往復ずつ | カメラアカウント（WS-Security） | 首振り、検知イベント |
| Tapo 独自 | 制御 API | 443 | 暗号化した JSON を1往復ずつ | Tapo アカウント（ユーザは admin） | 設定、録画の一覧、検知の記録 |
| Tapo 独自 | ストリーム | 8800 | 1本の接続に流し続ける | Tapo アカウント（接続ごとに Digest） | スピーカー、録画の取り出し |

コードもこの分け方に合わせてある。経路ごとの窓口は、RTSP が `media`、ONVIF が `onvif`、Tapo 独自の 443 番が `tapo.api.TapoApi`、8800 番が `tapo.stream.TapoStream`。
窓口を使う機能（`events`・`settings`・`talk`・`sdcard` など）は、その上に並べている。

### どの機能を、どの経路で実現できたか

| 機能 | 経路 | 結果 |
| --- | --- | --- |
| 映像・音声のストリーミング | RTSP（554 番） | できる |
| 録音・録画 | RTSP を ffmpeg で保存 | できる |
| 静止画 | RTSP から1枚切り出す | できる（ONVIF の GetSnapshotUri は無い） |
| 首振り（上下左右） | ONVIF PTZ（2020 番） | できる（絶対・相対・連続・停止・プリセット） |
| スピーカーからの再生 | **RTSP/ONVIF では不可**。Tapo 独自の双方向音声（8800 番） | できる（非公式のプロトコル） |
| microSD の録画の一覧と取り出し | **RTSP/ONVIF では不可**。Tapo 独自の API（443 番）と再生（8800 番） | できる（非公式。等速でしか届かない） |
| 設定の読み書き（画質、昼夜、検知、音量など） | Tapo 独自の API（443 番） | できる（非公式） |
| 検知イベントの受信 | ONVIF のイベント（カメラからこちらへ通知させる） | できる（PullPoint は不可） |
| 警報をいますぐ鳴らす | Tapo 独自の API | **API が見つからない**。スピーカーへサイレンの音を送って代える |

開いているポートは 443・554・2020・8800。

### RTSP

- URL は `rtsp://ユーザ:パスワード@ホスト:554/stream1`。認証は Digest、カメラアカウントを使う。
- `stream1` は H.264 High 2560×1440、`stream2` は H.264 High 640×360。音声はどちらも G.711 A-law の 8kHz モノラル。
  ONVIF は音声を 16kHz と答えるが、SDP も実際の中身も 8kHz。
- `stream8` は MJPEG 640×360（ONVIF の profile_3「jpegStream」）。ただし5秒間に届いた約300枚のうち別の画像は5枚だけで、
  同じ画像を送り直してくる（約 25Mbps）。Chromium ではこの JPEG が灰色にしか描かれなかった。
  このストリームは使っていない。
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

送信が届いていないことには、次の2つで気づいて `TalkError` にする（鳴っていないのに、鳴らしたことにしない）。
分かるのは「カメラが受け取ったか」までで、受け取った音を実際に鳴らしたかは、この経路では分からない。

- カメラが接続を閉じた。
- 送ったのにカメラが受け取っていない音声が 3 秒ぶんを超えた（TCP の確認が返っていない量を OS に尋ねる。Linux だけ）。
  カメラが止まっても、OS の送信バッファが埋まるまでは書き込みが成功し続けるので、書き込みの成否だけでは数分気づけない。
  ふだんは 0.05 秒ぶんほどしか溜まらない（20 秒の再生で実測）。

録画の取り出し（同じ 8800 番）とは、同時に使える。再生の途中で鳴らしても、鳴らしている途中で再生を始めても、両方とも最後まで進んだ。

非公式のプロトコルなので、ファームウェアの更新で使えなくなる可能性がある。

### microSD の録画

ONVIF に録画の検索・再生のサービス（Profile G）は無く、RTSP でも再生できない。Tapo 独自の2つの経路を使う。どちらも認証は Tapo アカウントのパスワード。

- 一覧は 443 番の独自の API。ログインの手順が込み入っているので [pytapo](https://github.com/JurajNyiri/pytapo) に任せている。
  録画のある日（カメラの時刻での日付）、1日の中の区間（開始・終了の UNIX 時刻と種別）、カードの状態（容量と空き）が取れる。
  種別は 1 と 2 があり、1 は長く連続した区間、2 は短い区間として返るので、1 を常時録画、2 を検知による録画として表示している（推定）。
- 取り出しは 8800 番。スピーカーと同じ Digest 認証のあと、時刻を指定した playback の要求を送ると、AES-CBC で暗号化された MPEG-TS が届く
  （鍵は認証の応答の nonce とパスワードのハッシュから作る）。映像は H.264 2560×1440、音声は G.711 µ-law 16kHz（ストリーム種別 0x91）。
  ffmpeg はこの音声の種別を知らないので、音声は TS から自分で取り出して MP4 に入れ直している。
- ファイルの転送ではなく「再生」なので、等速でしか届かない（20 秒ぶんの取り出しに 20.8 秒）。
  2 倍以上の速さを指定するとデータは速く届くが、音声が来なくなり、映像の時刻の進み方も変わるため、使っていない。
- 終了時刻の指定は守られず流れ続けるので、映像の時刻（PTS）で必要な長さに達したらこちらから切る。
  録画の無い時刻を指定すると、映像は届かず終了の通知だけが返る。
- 取り出している間は、区間の一覧をカメラが出さない（一覧に要る ID の発行が `USER_ID_FULL`（-71101）で拒まれ、先に取った ID も無効になる）。
  取り出しの `client_id` にその ID を使うと一覧は取れるが、今度は取り出しのほうが止まる。両立する方法は見つからなかった。
  録画のある日・検知の記録・カードの状態は、取り出している間も取れる。
  そこで `segments()` は最後に取れた一覧を覚えておき、取り出している間はそれを返す（まだ読んでいない日は `SdCardError`）。

```python
sd = cam.sdcard                               # tapo_c220.sdcard.SdCard
days = await sd.days()                         # ["20261008", "20261009", ...]
segments = await sd.segments(days[-1])         # [Segment(start, end, kind), ...]
await sd.export(segments[-1].start, 60, "clip.mp4")   # 60 秒ぶんを MP4 に（約 60 秒かかる）
async for chunk in sd.playback(segments[-1].start):   # 復号した MPEG-TS を、やめるまで受け取り続ける
    ...
```

ブラウザでの再生は `tapo_c220.sdplayer.SdPlayer`。指定した時刻からの録画を WebRTC のトラックにして、止めるまで流し続ける。
映像の H.264 は変換せず送り、音声は µ-law 16kHz が WebRTC の標準の形ではないので、PCM に戻して Opus にする。
いま流している位置（録画の時刻）は、TS の PTS の進みから求める（`position`）。

### 設定（Tapo 独自の API）

`tapo_c220.settings.CameraSettings`（`cam.settings` でも取れる）。認証は Tapo アカウントのパスワード（ユーザは admin）。

```python
s = cam.settings
await s.set_day_night("auto")                 # auto / on（夜・赤外線）/ off（昼・カラー）
await s.set_image(brightness=55)              # 明るさ・コントラスト・彩度・シャープネス（0〜100）
await s.set_detection("person", True)         # motion / person / pet / vehicle / baby_cry / bark / meow / glass_break / tamper / line_crossing
await s.set_alarm(enabled=True, sound=False)  # 検知したときの警報（音・光・音量）
await s.set_privacy(True)                     # プライバシーモード
```

| 分類 | 読む | 書く |
| --- | --- | --- |
| 本体 | `info` `time` `privacy` `led` `scheduled_reboot` `firmware` `notifications` | `set_privacy` `set_led` `set_scheduled_reboot` `set_notifications` `reboot` |
| 画質 | `day_night` `image` `flip` `lens_correction` `light_frequency` `osd` `video` `video_capability` | `set_day_night` `set_image` `set_flip` `set_lens_correction` `set_light_frequency` `set_osd` |
| 音声 | `audio` | `set_speaker_volume` `set_microphone` |
| 検知・警報 | `detection` `detections` `alarm` | `set_detection` `set_alarm` |
| 首振り | `auto_track` | `set_auto_track` `cruise` `calibrate` |
| 録画 | `recording` | `set_recording` `set_loop_recording` |

- 書き込みは、実機で値を変えて読み直し、元に戻して確かめた（LED、通知、昼夜、画質の4項目、歪みの補正、電源周波数、OSD の曜日、
  スピーカーとマイクの音量、マイクのミュート、検知の10種類、自動追尾、上書き録画、警報の音・光・音量、プライバシーモード）。
  `set_recording`・`set_flip`・`set_scheduled_reboot` は、今と同じ値を書いて拒まれないことだけ確かめた。
  `reboot`・`cruise`・`calibrate` は呼んでいない。映像の符号化（解像度・ビットレート）は読むだけ。
- 警報の設定は、pytapo の `setAlarm` の書き方（`method: set`）が拒まれる。`setAlertConfig` で書く。
- 警報をいますぐ鳴らす API は見つからない。pytapo の `startManualAlarm`（`manual_msg_alarm`）、新しいファームウェア向けと
  される `manual_alarm`、`setSirenStatus` は、どれも「そのメソッドは無い」（-40210）。代わりに `cam.siren(秒)` が、
  サイレンの音を作ってスピーカーの経路（8800 番）で鳴らす。音の大きさはスピーカーの音量で決まる。カメラの光は点かない。
- ファームウェアの自動更新の設定の書き込みは、認証のエラー（-40209）で拒まれる。包んでいない。
- SD カードの初期化とファームウェアの更新の開始は、取り返しがつかないので包んでいない。
- この機種に無いもの：バッテリー、白色ライト、PIR、チャイム、荷物の検知。

### 検知イベント（ONVIF）

```python
await cam.settings.set_detection("person", True)   # オンにした検知だけ届く
async for event in cam.events():                    # やめるまで続く。Event(kind, active, time, topic)
    print(event.kind, "検知" if event.active else "収まった")
```

- PullPoint（CreatePullPointSubscription → PullMessages）は使えない。購読は作れるが、PullMessages を送るとカメラが何も返さずに切る。
- WS-BaseNotification の Subscribe で、こちらが開けた HTTP の口の URL を渡すと、カメラがそこへ Notify を POST してくる。
  `events()` は LAN 側のアドレスの空いたポートで待ち受け、カメラの IP アドレス以外からの接続は受けない。
  カメラからこちらへ着信できること（ファイアウォール）が要る。
- 届くのは動体（Motion）・人物（People）・いたずら（Tamper）・ライン横断（LineCross）・TPSmartEvent。
  検知している間は真を約1秒ごとに送り、収まると偽を1回送る。`events()` は変わり目だけを返す。
- 通知に付いている時刻（UtcTime）は当てにならない（人物の検知で、最初の時刻のまま変わらなかった）。届いた時刻を使っている。
- 実機で届いたのを確かめたのは動体と人物。いたずら・ライン横断・TPSmartEvent は起こせていない。
  首振りの直後の動体は、届くときと届かないときがあった。
- 過去の検知は `SdCard.detections(start, end)`（カメラが SD カードに記録した検知の一覧）でも読める。

## 止まり方・長く使ったときについて

- ライブ映像と録画の再生を同時に 40 分流し続けて、止まらないこと、録画の区間の切れ目をまたいでも続くことを確かめた（Chromium）。
  このとき、サーバのメモリが毎分約 0.7MB ずつ増え続けた。原因は RTSP を読ませていた aiortc の `MediaPlayer` で、
  自前のスレッドで読む形（`webrtc._Reader`）に替えると増えなくなった（8 分で +0.6MB）。数時間の連続は試していない。

- ffmpeg は、起こしたプロセス（WebUI のサーバなど）が強制終了されても一緒に止まる（Linux の `setpriv --pdeathsig` を通して起こす）。
  以前は、ファイルへ録音・録画している ffmpeg が残り、カメラの RTSP の同時接続（3本）を埋めたままになった。
  Linux 以外や、`setpriv`（util-linux）が無い環境では、この仕組みは無い。

## WebUI

- ライブ映像：既定は WebRTC（640×360 / 2560×1440、音あり）。カメラの H.264 と G.711 A-law は WebRTC の標準のコーデックなので、
  変換せずそのまま RTP に載せ替えている。合図は `POST /api/webrtc/offer` の1往復だけで、STUN/TURN は使わない（同じ LAN の中で使う前提）。
  切れたら2秒後につなぎ直す。Chromium で両方の解像度の再生を確認した（遅れの実測はしていない）。
  カメラは High プロファイルで送ってくるが、ブラウザと取り決めるのは Baseline（`42001f`）のまま。Chromium は復号できたが、
  ほかのブラウザでは未確認。
- 映像なしで音だけ聞く表示もある（MP3 を HTTP の応答として流し続けるだけで、遅れの制御や再接続は無い）。
- WebRTC・音だけの表示・録音・録画は、それぞれ RTSP を1本使う。カメラの上限は合わせて3本。
- 首振り：押している間だけ動く（矢印キーでも可）か、1回押すと少し動くかを選べる。プリセットの保存・移動・削除。
- 録音（WAV）・録画（MP4）：開始と停止、保存したファイルの再生・ダウンロード・削除。同時には1つ、最長1時間。
- microSD の録画：選んだ日の録画の区間をタイムラインに帯で描く（幅は 24時間・6時間・1時間・10分）。押した時刻から WebRTC で再生し、
  再生している位置の線が動く。再生中に別の時刻を押すと、そこへ移る（1 秒弱）。止めるまで、または録画の終わりまで流れ続ける。
  再生している位置から長さ（最長1時間）を指定して MP4 に保存もできる（`sd-日付-時刻.mp4` として録音・録画の一覧に並ぶ）。
  カメラの再生は1本ずつなので、再生と保存は同時にできない。
  再生・保存している間は、まだ開いていない日のタイムラインを読めない（カメラの制約。止めてから選び直す）。
- スピーカー：音声ファイルを送る、またはブラウザのマイクで録って送る（リアルタイムの通話ではない）。長さの上限は無く
  （ファイルは 20MB まで）、途中で止められる。サイレン（5〜60 秒）も同じ経路で鳴らす。
- 検知イベント：動体・人物・いたずら・ライン横断の検知をオン・オフし、届いたイベント（検知した・収まった）を新しい順に並べる。
  サーバが購読し（最初にページを開いたとき）、ブラウザは2秒ごとに問い合わせる。
- 画質・昼夜：昼夜の切り替え（自動・昼・夜）と、明るさ・コントラスト・彩度・シャープネス。動かすとすぐカメラに書く。
