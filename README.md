# tapo-c220-survey

TP-Link Tapo C220 を RTSP と ONVIF（Profile S）で操作できる範囲を実機で調べ、その結果をライブラリ `tapo_c220`（配布名は `tapo-c220`）にまとめたもの。
ライブラリの使用例として、ブラウザから一通り操作する薄い WebUI（`examples/webui/`）を添えている。

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
uv run tapo-c220 info           # コマンドラインから（下の表）
uv run pytest                   # カメラ無しで動くテスト
uv run --package tapo-c220-webui examples/webui/app.py   # 使用例の WebUI。http://127.0.0.1:8220 （--host / --port で変更）
```

構成は `uv init --lib` の形（`src/tapo_c220/`、`py.typed`、ビルドは `uv_build`）。WebUI はライブラリに含めず、
同じワークスペースの別のプロジェクト（`examples/webui/`。FastAPI などの依存はそちらが持つ）にしてある。

```
pyproject.toml            # ライブラリ tapo-c220
src/tapo_c220/            # ライブラリ本体（下の表）
tests/
examples/webui/           # 使用例：pyproject.toml、app.py、static/index.html
```

ライブラリの追加の依存：`tapo-c220[tapo]`（設定、microSD の録画。pytapo と cryptography）、
`tapo-c220[packets]`（映像と音声をパケットとして取り出す。PyAV）。無しでも RTSP・ONVIF・スピーカーは使える。

WebUI は既定では手元（127.0.0.1）で待ち受ける。録音・録画は、起こした場所の `recordings/`（`TAPO_RECORD_DIR`）に保存する。

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
    await cam.detect_inverted()               # 天井に逆さに付けている（上下反転がオン）なら、以後 move・step の向きを部屋から見た向きに合わせる
    await cam.play("hello.mp3")               # カメラのスピーカーで鳴らす
    await rec.stop()
    async for chunk in cam.live_audio():      # マイクの音（MP3）
        ...
```

制御 API（設定など）の呼び出しを取り消しても、始まった要求は終わるまで待ってから取り消す（次の呼び出しと重ならない）。要求はカメラに届いて、設定が変わっていることがある。

映像と音声を自分のプログラムへ引き込む口は `tapo_c220.packets`（PyAV が要る。`tapo-c220[packets]`）。
ライブも microSD の録画も、変換していないパケットの列（`Packet(kind, data, time, codec, rate, keyframe)`）として同じ形で渡す。
WebRTC のような配信の方式は、ライブラリは知らない。受け取った側で載せ替える（WebRTC の例が `examples/webui/webrtc.py`）。

```python
packets = await cam.live_packets("stream2")    # packets.LivePackets。映像は H.264、音声は G.711 A-law 8kHz
async for packet in packets:                   # 時刻（packet.time）は、カメラの RTCP のずれを直して途切れなく進む
    ...
await packets.aclose()                         # RTSP を返す（カメラの同時接続は3本まで）

packets = cam.sdcard.packets(start)            # packets.SdPackets。録画を start（UNIX 時刻）から。position で今の位置
```

| モジュール | 役割 |
| --- | --- |
| `tapo_c220.facade` | 全体の窓口の `TapoC220`。向き（left / right / up / down）を画面の動きに合わせる |
| `tapo_c220.onvif` | ONVIF の最小限のクライアント（SOAP を手で組む。依存は httpx だけ） |
| `tapo_c220.media` | RTSP の受信と変換（ffmpeg）。静止画、ライブ配信、録音・録画 |
| `tapo_c220.packets` | ライブと録画の映像・音声を、変換していないパケットの列として渡す（PyAV） |
| `tapo_c220.events` | 検知のイベントを ONVIF で受け取る |
| `tapo_c220.tapo` | Tapo 独自のプロトコルへの窓口だけを置く。`TapoSession` が認証情報を持ち、下の2つを渡す |
| `tapo_c220.tapo.api` | 443 番の制御 API の窓口 `TapoApi`（pytapo を別スレッドで呼ぶ。`[tapo]` の追加の依存が要る） |
| `tapo_c220.tapo.stream` | 8800 番のストリームの窓口 `TapoStream`（接続と認証。依存なし） |
| `tapo_c220.settings` | カメラの設定の読み書き（`CameraSettings`）。`tapo.api` を使う |
| `tapo_c220.talk` | スピーカーへの音声。`tapo.stream` を使う |
| `tapo_c220.sdcard` | microSD の録画。一覧は `tapo.api`、取り出しは `tapo.stream` を使う |

## 調査結果

詳しい調査結果（実測の値、生の応答の形、試してうまくいかなかったこと）は [Wiki](https://github.com/skrtk98/tapo-c220/wiki) にまとめた。
ここには、ライブラリを使うときに要る要点だけを置く。

| 分類 | 経路 | ポート | 認証 | 使いみち | Wiki |
| --- | --- | --- | --- | --- | --- |
| 標準 | RTSP | 554 | カメラアカウント（Digest） | 映像・音声、録音・録画、静止画 | [RTSP](https://github.com/skrtk98/tapo-c220/wiki/RTSP) |
| 標準 | ONVIF | 2020 | カメラアカウント（WS-Security） | 首振り、検知イベント | [ONVIF](https://github.com/skrtk98/tapo-c220/wiki/ONVIF)・[PTZ](https://github.com/skrtk98/tapo-c220/wiki/PTZ)・[検知イベント](https://github.com/skrtk98/tapo-c220/wiki/検知イベント) |
| Tapo 独自 | 制御 API | 443 | Tapo アカウント（ユーザは admin） | 設定、録画の一覧、検知の記録 | [制御 API と設定](https://github.com/skrtk98/tapo-c220/wiki/制御-API-と設定) |
| Tapo 独自 | ストリーム | 8800 | Tapo アカウント（接続ごとに Digest） | スピーカー、録画の取り出し | [8800 番のストリーム](https://github.com/skrtk98/tapo-c220/wiki/8800-番のストリーム)・[microSD の録画](https://github.com/skrtk98/tapo-c220/wiki/microSD-の録画) |

コードもこの分け方に合わせてある。経路ごとの窓口は、RTSP が `media`、ONVIF が `onvif`、Tapo 独自の 443 番が `tapo.api.TapoApi`、8800 番が `tapo.stream.TapoStream`。
窓口を使う機能（`events`・`settings`・`talk`・`sdcard` など）は、その上に並べている。
Tapo 独自の経路は非公式なので、ファームウェアの更新で使えなくなる可能性がある。

| 機能 | 経路 | 結果 |
| --- | --- | --- |
| 映像・音声、録音・録画、静止画 | RTSP | できる（ONVIF の GetSnapshotUri は無いので、静止画も RTSP から切り出す） |
| 首振り | ONVIF PTZ | できる（絶対・相対・連続・停止・プリセット） |
| スピーカーからの再生 | 8800 番 | できる。RTSP・ONVIF には音声を送る経路が無い |
| microSD の録画の一覧と取り出し | 443 番と 8800 番 | できる。等速でしか届かない |
| 設定の読み書き | 443 番 | できる |
| 検知イベントの受信 | ONVIF の Subscribe | できる。PullPoint は使えない |
| 警報をいますぐ鳴らす | — | 確かな API が見つからない。スピーカーへサイレンの音を送って代える |

### 使うときの注意

- **RTSP は同時に3接続まで。** WebRTC・音だけの表示・録音・録画・`live_packets()` は、それぞれ1本使う。
- **RTSP の音声だけは受けられない。** 録音も映像ごと受けて捨てる。
- **カメラの RTCP の時刻が乱れている**（約 0.5 秒ごとに ±0.38 秒）。そのまま使うと映像が毎秒つかえる。
  ライブラリは、ffmpeg では届いた時刻を使い（`-use_wallclock_as_timestamps 1`）、`packets` では時刻を付け直して渡す。
- **首振りの向き。** ONVIF の pan は、速度と位置で正負が逆になる。上下反転（flip）をオンにして天井に逆さに付けると、上下左右とも逆になる。
  `TapoC220.inverted`（None は未確定＝正立として扱う。コンストラクタ引数か代入で明示）と `await cam.detect_inverted()`（`settings.flip()` から決める。
  読めなければ False で警告）があり、inverted のとき `move`・`step` は向きを入れ替える。生の位置を部屋から見た向き
  （pan 正で左、tilt 正で上）に直すのは `cam.room_position(pos)`。`goto`・`position()`・プリセットは生の値のまま。
  CLI の `move`・`step` は `--inverted auto|on|off`（既定 auto）。
- **ContinuousMove の Timeout は整数の秒だけ**（小数を送るとカメラが壊れた応答を返す）。ライブラリは切り上げて送る。省くと 180 秒動き続ける。
- **スピーカー**は、カメラから受信の確認が返らない。カメラが接続を閉じたときと、TCP の確認が返っていない音声が 3 秒ぶんを超えたとき（Linux だけ）に `TalkError` にする。
  分かるのは「カメラが受け取ったか」までで、鳴ったかは分からない。録画の取り出しとは同時に使える。
- **microSD の録画**は、Tapo アカウントのパスワード（`TAPO_CLOUD_PASSWORD`）が要る。
  - 等速でしか届かない（20 秒ぶんに 20.8 秒）。
  - 区間の切れ目では止まらず次の区間へ続き、録画の最新の端で `finished` が届いて正常終了する。
    途中の無通信や finished より先の接続終了は `PlaybackInterrupted`（`SdCardError` の子）。
    `export()` は、途中で切れたら受け取れた分を MP4 に保存してから投げる（例外の `saved` に秒数、`path` に保存先）。
  - 取り出している間は、カメラが区間の一覧を出さない。`segments()` は最後に取れた一覧を返す（まだ読んでいない日は `SdCardError`）。
  - 種別 1 を常時録画、2 を検知による録画として扱っている（推定）。
- **検知イベント**は、カメラからこちらへ HTTP で着信できることが要る（ファイアウォール）。`events()` は LAN 側のアドレスの空いたポートで待ち受け、
  カメラの IP アドレス以外からの接続は受けない。コンテナの中などでは
  `events(port=8802, bind="0.0.0.0", advertise="http://<ホストの LAN の IP>:8802/onvif-notify")` のように、待ち受けるアドレスとカメラに伝える宛先を分けて渡す。
  docker-proxy が中継する構成では送信元が変わり、弾かれる。
  `events()` は変わり目だけを返す。受け取る側が遅れると、検知中の繰り返し通知から捨て、終わりの通知は残す。通知の時刻は当てにならないので、届いた時刻を使う。
- **設定**の書き込みは、警報だけ pytapo の `setAlarm` ではなく `setAlertConfig` で書く。SD カードの初期化とファームウェアの更新の開始は、取り返しがつかないので包んでいない。

### 設定（Tapo 独自の API）

`tapo_c220.settings.CameraSettings`（`cam.settings` でも取れる）。

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

### microSD の録画と検知イベント

```python
sd = cam.sdcard                               # tapo_c220.sdcard.SdCard
days = await sd.days()                         # ["20261008", "20261009", ...]
segments = await sd.segments(days[-1])         # [Segment(start, end, kind), ...]
await sd.export(segments[-1].start, 60, "clip.mp4")   # 60 秒ぶんを MP4 に（約 60 秒かかる）
async for chunk in sd.playback(segments[-1].start):   # 復号した MPEG-TS を、finished まで受け取る
    ...

await cam.settings.set_detection("person", True)   # オンにした検知だけ届く
async for event in cam.events():                    # やめるまで続く。Event(kind, active, time, topic)
    print(event.kind, "検知" if event.active else "収まった")
```

録画をパケットの列として読むのは `sd.packets(start)`（`tapo_c220.packets.SdPackets`）。止めるまで、または録画の終わりまで続く。
過去の検知は `sd.detections(start, end)`（カメラが SD カードに記録した検知の一覧）でも読める。

## 止まり方・長く使ったときについて

- ffmpeg は、起こしたプロセス（WebUI のサーバなど）が強制終了されても一緒に止まる（Linux の `setpriv --pdeathsig` を通して起こす）。
  Linux 以外や、`setpriv`（util-linux）が無い環境では、この仕組みは無い。
- ライブ映像と録画の再生を同時に 40 分流し続けて、止まらないことを確かめた。メモリの測定は [Wiki の「長時間とメモリ」](https://github.com/skrtk98/tapo-c220/wiki/長時間とメモリ)。数時間の連続は試していない。

## WebUI（使用例、`examples/webui/`）

ライブラリの外に置いたサンプルで、`TapoC220` などを HTTP から呼ぶだけの FastAPI のアプリ。

```sh
uv run --package tapo-c220-webui examples/webui/app.py --password 'WebUI のパスワード'
uv run --package tapo-c220-webui examples/webui/app.py --host 0.0.0.0 --password 'WebUI のパスワード' --allowed-host camera.example
```

`--password` または環境変数 `TAPO_WEBUI_PASSWORD` で HTTP Basic 認証を設定する（両方ある場合は `--password` が優先）。
非ループバックのアドレスで待ち受けるにはパスワードが必須。
`create_app()` を直接使う場合も、要求を受けた側がループバック以外ならパスワードが必須。
Host ヘッダで許す名前は `--allowed-host` を複数指定するか、環境変数 `TAPO_WEBUI_ALLOWED_HOSTS` に
カンマ区切りで指定できる。
`0.0.0.0` と `::` で待ち受け、許可名を指定しない場合は Host の検査をしない。
Basic 認証のパスワードは平文 HTTP では盗聴できる。LAN の外から使う場合は TLS の逆プロキシを
前に置くこと（逆プロキシは Host をそのまま渡す。書き換えると、POST が Origin の検査で弾かれる）。
どの待ち受けでも、ほかのサイトからの POST など（Origin が Host と違う要求）と、許可していない Host（DNS リバインディング）は拒む。

- ライブ映像：既定は WebRTC（640×360 / 2560×1440、音あり）。カメラの H.264 と G.711 A-law は WebRTC の標準のコーデックなので、
  変換せずそのまま RTP に載せ替えている。合図は `POST /api/webrtc/offer` の1往復だけで、STUN/TURN は使わない（同じ LAN の中で使う前提）。
  送信キューは数秒ぶんに制限し、遅い受信者にはライブをエラーで切る。microSD の録画再生は、キューが空くまでカメラからの読み取りを待つ。
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
