"""カメラの設定と、設定に近い操作（pytapo が要る。`tapo-c220-survey[tapo]`）。

RTSP と ONVIF で触れない項目を、Tapo 独自の API（443 番）で読み書きする。C220（ファームウェア 1.3.1）が応答した項目だけを包んでいる。
この機種に無いもの（バッテリー、白色ライト、PIR、チャイム、荷物の検知）は扱わない。
SD カードの初期化とファームウェアの更新の開始は、取り返しがつかないので包んでいない（必要なら TapoApi.execute で呼べる）。
書き込みは、実機で値を変えて読み直し、元に戻して確かめた（再起動・往復・位置合わせ・定期の再起動の時刻は、呼んでいない）。
"""

from __future__ import annotations

from enum import StrEnum

from .api import TapoApi, TapoApiError
from .config import TapoConfig

_on = lambda value: value == "on"  # noqa: E731
_switch = lambda enabled: "on" if enabled else "off"  # noqa: E731


class Detection(StrEnum):
    """検知の種類。"""

    MOTION = "motion"
    PERSON = "person"
    PET = "pet"
    VEHICLE = "vehicle"
    BABY_CRY = "baby_cry"
    BARK = "bark"
    MEOW = "meow"
    GLASS_BREAK = "glass_break"
    TAMPER = "tamper"
    LINE_CROSSING = "line_crossing"


# 検知の種類 → pytapo のメソッド名の語幹（get〜 / set〜）
_DETECTIONS = {
    Detection.MOTION: "MotionDetection", Detection.PERSON: "PersonDetection", Detection.PET: "PetDetection",
    Detection.VEHICLE: "VehicleDetection", Detection.BABY_CRY: "BabyCryDetection", Detection.BARK: "BarkDetection",
    Detection.MEOW: "MeowDetection", Detection.GLASS_BREAK: "GlassBreakDetection",
    Detection.TAMPER: "TamperDetection", Detection.LINE_CROSSING: "LinecrossingDetection",
}


class DayNight(StrEnum):
    """昼夜の切り替え。AUTO は明るさで自動、NIGHT は常に赤外線（白黒）、DAY は常にカラー。"""

    AUTO = "auto"
    NIGHT = "on"
    DAY = "off"


# 画質の項目 → API の名前。値はどれも 0〜100
_IMAGE = {"brightness": "luma", "contrast": "contrast", "saturation": "saturation", "sharpness": "sharpness"}


class CameraSettings:
    def __init__(self, cfg: TapoConfig | None = None, api: TapoApi | None = None):
        self.api = api or TapoApi(cfg or TapoConfig.load())

    # --- 本体 --------------------------------------------------------------------------------------------------------

    async def info(self) -> dict:
        """機種、ファームウェア、名前など。"""
        return (await self.api.call(lambda a: a.getBasicInfo()))["device_info"]["basic_info"]

    async def time(self) -> dict:
        """カメラの時計（seconds_from_1970、local_time）と、タイムゾーン（zone_id、timezone、timing_mode）。"""
        clock = await self.api.call(lambda a: a.getTime())
        zone = await self.api.call(lambda a: a.getTimezone())
        return {**clock["system"]["clock_status"], **zone["system"]["basic"]}

    async def privacy(self) -> bool:
        """プライバシーモード（レンズを隠し、映像と音声を止める）。"""
        return _on((await self.api.call(lambda a: a.getPrivacyMode()))["enabled"])

    async def set_privacy(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setPrivacyMode(enabled))

    async def led(self) -> bool:
        return _on((await self.api.call(lambda a: a.getLED()))["enabled"])

    async def set_led(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setLEDEnabled(enabled))

    async def reboot(self) -> None:
        """カメラを再起動する。1分ほど応答しなくなる。"""
        await self.api.call(lambda a: a.reboot())

    async def scheduled_reboot(self) -> dict:
        """定期の再起動（enabled、day、time）。"""
        return (await self.api.call(lambda a: a.getReboot()))["timing_reboot"]["reboot"]

    async def set_scheduled_reboot(self, enabled: bool, time: str | None = None, day: int | None = None) -> None:
        """time は "03:00:00" の形、day は曜日（7 は毎日）。"""
        await self.api.call(lambda a: a.setReboot(enabled, time, day))

    async def firmware(self) -> dict:
        """更新の状態（state など）と、自動更新の設定（auto_enabled、auto_time）。読むだけ
        （自動更新の設定を書く setFirmwareAutoUpgradeConfig は、この経路の認証では拒まれる）。"""
        status = await self.api.call(lambda a: a.getFirmwareUpdateStatus())
        auto = await self.api.call(lambda a: a.getFirmwareAutoUpgradeConfig())
        common = auto["auto_upgrade"]["common"]
        return {**status["cloud_config"]["upgrade_status"], "auto_enabled": _on(common["enabled"]),
                "auto_time": common["time"]}

    async def notifications(self) -> bool:
        """Tapo アプリへの通知。"""
        return _on((await self.api.call(lambda a: a.getNotificationsEnabled()))["notification_enabled"])

    async def set_notifications(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setNotificationsEnabled(enabled))

    # --- 画質 --------------------------------------------------------------------------------------------------------

    async def day_night(self) -> DayNight:
        return DayNight(await self.api.call(lambda a: a.getDayNightMode()))

    async def set_day_night(self, mode: DayNight | str) -> None:
        mode = DayNight(mode)
        await self.api.call(lambda a: a.setDayNightMode(mode.value))

    async def image(self) -> dict[str, int]:
        """明るさ・コントラスト・彩度・シャープネス（どれも 0〜100）。"""
        common = (await self.api.call(lambda a: a.getDayNightModeConfig()))["image"]["common"]
        return {name: int(common[key]) for name, key in _IMAGE.items()}

    async def set_image(self, *, brightness: int | None = None, contrast: int | None = None,
                        saturation: int | None = None, sharpness: int | None = None) -> None:
        given = {"brightness": brightness, "contrast": contrast, "saturation": saturation, "sharpness": sharpness}
        values = {}
        for name, value in given.items():
            if value is None:
                continue
            if not 0 <= value <= 100:
                raise ValueError(f"{name} は 0〜100: {value}")
            values[_IMAGE[name]] = str(int(value))
        if values:
            await self.api.execute("setDayNightModeConfig", {"image": {"common": values}})

    async def flip(self) -> bool:
        """映像の上下左右の反転（天井に付けるとき）。"""
        return bool(await self.api.call(lambda a: a.getImageFlipVertical()))

    async def set_flip(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setImageFlipVertical(enabled))

    async def lens_correction(self) -> bool:
        """レンズの歪みの補正。"""
        return bool(await self.api.call(lambda a: a.getLensDistortionCorrection()))

    async def set_lens_correction(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setLensDistortionCorrection(enabled))

    async def light_frequency(self) -> str:
        """照明のちらつきを抑える電源周波数（"auto" / "50" / "60"）。"""
        return await self.api.call(lambda a: a.getLightFrequencyMode())

    async def set_light_frequency(self, mode: str) -> None:
        if mode not in ("auto", "50", "60"):
            raise ValueError(f"mode は auto / 50 / 60: {mode}")
        await self.api.call(lambda a: a.setLightFrequencyMode(mode))

    async def video(self) -> dict:
        """メインストリームの符号化の設定（resolution、bitrate、frame_rate、encode_type など）。読むだけ。"""
        return (await self.api.call(lambda a: a.getVideoQualities()))["video"]["main"]

    async def video_capability(self) -> dict:
        """選べる解像度・ビットレート・符号化の種類。"""
        return (await self.api.call(lambda a: a.getVideoCapability()))["video_capability"]["main"]

    async def osd(self) -> dict[str, bool]:
        """映像に重ねる表示（date、week、logo）のオン・オフ。"""
        osd = (await self.api.call(lambda a: a.getOsd()))["OSD"]
        return {name: _on(osd[name]["enabled"]) for name in ("date", "week", "logo") if name in osd}

    async def set_osd(self, *, date: bool | None = None, week: bool | None = None, logo: bool | None = None) -> None:
        changes = {name: {"enabled": _switch(value)}
                   for name, value in {"date": date, "week": week, "logo": logo}.items() if value is not None}
        if changes:
            await self.api.execute("setOsd", {"method": "set", "OSD": changes})

    # --- 音声 --------------------------------------------------------------------------------------------------------

    async def audio(self) -> dict:
        """スピーカーとマイクの音量（0〜100）、マイクのミュート。"""
        config = (await self.api.call(lambda a: a.getAudioConfig()))["audio_config"]
        return {"speaker_volume": int(config["speaker"]["volume"]), "microphone_volume": int(config["microphone"]["volume"]),
                "microphone_mute": _on(config["microphone"]["mute"])}

    async def set_speaker_volume(self, volume: int) -> None:
        if not 0 <= volume <= 100:
            raise ValueError(f"volume は 0〜100: {volume}")
        await self.api.call(lambda a: a.setSpeakerVolume(str(int(volume))))

    async def set_microphone(self, *, volume: int | None = None, mute: bool | None = None) -> None:
        if volume is not None and not 0 <= volume <= 100:
            raise ValueError(f"volume は 0〜100: {volume}")
        await self.api.call(lambda a: a.setMicrophone(None if volume is None else str(int(volume)), mute))

    # --- 検知と警報 ---------------------------------------------------------------------------------------------------

    async def detection(self, kind: Detection | str) -> dict:
        """その種類の検知の設定。enabled（真偽）と、機種が返すその他の項目（sensitivity など）。"""
        name = _DETECTIONS[Detection(kind)]
        raw = await self.api.call(lambda a: getattr(a, "get" + name)())
        raw = raw.get("detection", raw)  # ライン横断だけ、1段深い
        return {**raw, "enabled": _on(raw.get("enabled"))}

    async def detections(self) -> dict[str, bool]:
        """すべての種類の、オン・オフ。"""
        return {kind.value: (await self.detection(kind))["enabled"] for kind in Detection}

    async def set_detection(self, kind: Detection | str, enabled: bool, sensitivity: int | str | None = None) -> None:
        """sensitivity は 1〜100 か "low" / "normal" / "high"（ライン横断には無い）。"""
        kind = Detection(kind)
        name = _DETECTIONS[kind]
        if kind is Detection.LINE_CROSSING:
            await self.api.call(lambda a: a.setLinecrossingDetection(enabled))
        else:
            await self.api.call(lambda a: getattr(a, "set" + name)(enabled, sensitivity or False))

    async def alarm(self) -> dict:
        """警報（検知したときに鳴らす・光らせる）の設定。enabled、sound、light、volume（"low" / "normal" / "high"）など。"""
        raw = (await self.api.execute("getAlertConfig", {"msg_alarm": {"name": ["chn1_msg_alarm_info"]}}))
        raw = raw["msg_alarm"]["chn1_msg_alarm_info"]
        return {"enabled": _on(raw["enabled"]), "sound": "sound" in raw["alarm_mode"], "light": "light" in raw["alarm_mode"],
                "volume": raw.get("alarm_volume"), "duration": int(raw.get("alarm_duration") or 0), "type": raw.get("alarm_type")}

    async def set_alarm(self, *, enabled: bool | None = None, sound: bool | None = None, light: bool | None = None,
                        volume: str | None = None, duration: int | None = None) -> None:
        """与えた項目だけを変える。sound と light のどちらかは真のままにすること。
        警報をいますぐ鳴らす API は、このファームウェアでは見つからない（pytapo の startManualAlarm も setSirenStatus も
        「そのメソッドは無い」で拒まれる）。いますぐ鳴らすには TapoC220.siren() で、スピーカーへサイレンの音を送る。
        （pytapo の setAlarm が使う書き方は、このファームウェアでは拒まれる。setAlertConfig で書く。）"""
        changes: dict = {}
        if enabled is not None:
            changes["enabled"] = _switch(enabled)
        if sound is not None or light is not None:
            current = await self.alarm()
            sound = current["sound"] if sound is None else sound
            light = current["light"] if light is None else light
            if not sound and not light:
                raise ValueError("sound と light のどちらかは真にすること")
            changes["alarm_mode"] = [name for name, used in (("light", light), ("sound", sound)) if used]
        if volume is not None:
            if volume not in ("low", "normal", "high"):
                raise ValueError(f"volume は low / normal / high: {volume}")
            changes["alarm_volume"] = volume
        if duration is not None:
            changes["alarm_duration"] = str(int(duration))
        if changes:
            await self.api.execute("setAlertConfig", {"msg_alarm": {"chn1_msg_alarm_info": changes}})

    # --- 首振り -------------------------------------------------------------------------------------------------------

    async def auto_track(self) -> bool:
        """動くものを追って首を振る。"""
        return _on((await self.api.call(lambda a: a.getAutoTrackTarget()))["enabled"])

    async def set_auto_track(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setAutoTrackTarget(enabled))

    async def cruise(self, axis: str | None) -> None:
        """端から端まで往復させる。axis は "x"（左右）か "y"（上下）、None で止める。"""
        if axis not in ("x", "y", None):
            raise ValueError(f"axis は x / y / None: {axis}")
        await self.api.call(lambda a: a.setCruise(axis is not None, axis or False))

    async def calibrate(self) -> None:
        """モーターの位置を合わせ直す（端まで動いてから元の向きに戻る）。"""
        await self.api.call(lambda a: a.calibrateMotor())

    # --- SD カードへの録画 --------------------------------------------------------------------------------------------

    async def recording(self) -> dict:
        """録画の設定。enabled、loop（いっぱいになったら古いものから消す）、曜日ごとの予定（monday〜sunday）。"""
        plan = await self.api.call(lambda a: a.getRecordPlan())
        loop = await self.api.call(lambda a: a.getCircularRecordingConfig())
        return {**plan, "enabled": _on(plan.get("enabled")), "loop": _on(loop.get("loop"))}

    async def set_recording(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setRecordPlan(enabled))

    async def set_loop_recording(self, enabled: bool) -> None:
        await self.api.call(lambda a: a.setCircularRecordingConfig(enabled))


__all__ = ["CameraSettings", "DayNight", "Detection", "TapoApiError"]
