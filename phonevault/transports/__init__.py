"""Pick the best link that answers: adb (USB, then Wi-Fi), else MTP."""
from __future__ import annotations

from ..core.config import Config, devices_off
from .adb import AdbTransport
from .base import RemoteFile, Transport
from .mtp import MtpTransport

__all__ = ["AdbTransport", "MtpTransport", "RemoteFile", "Transport", "adb_for", "pick_transport", "probe", "use"]

_fixed: Transport | None = None


def use(transport: Transport | None) -> None:
    """Pin one transport for the whole process (the demo phone). None = look for a real phone again."""
    global _fixed
    _fixed = transport


def adb_for(cfg: Config) -> AdbTransport:
    """The one place that builds an adb link."""
    if devices_off():
        raise RuntimeError("Real phones are switched off here (PHONEVAULT_NO_DEVICES).")
    return AdbTransport(cfg.adb, cfg.device_serial, cfg.wifi_address)


def probe(cfg: Config) -> dict:
    """What is reachable right now. Never raises."""
    info = {"adb": False, "adb_desc": "", "adb_unauthorized": False, "adb_devices": [], "mtp": False, "mtp_desc": ""}
    if _fixed is not None:
        info.update(adb=True, adb_desc=_fixed.describe(), demo=bool(getattr(_fixed, "demo", False)))
        return info
    if devices_off():
        return info
    try:
        adb = adb_for(cfg)
        info["adb_devices"] = adb.devices()
        info["adb"] = adb.available()
        info["adb_desc"] = adb.describe() if info["adb"] else ""
        info["adb_unauthorized"] = adb.unauthorized()
        if adb.problem:
            info["adb_error"] = adb.problem
    except Exception as e:  # noqa: BLE001
        info["adb_error"] = str(e)
    return info


def pick_transport(cfg: Config, prefer: str = "auto", check_mtp: bool = True) -> Transport:
    if _fixed is not None:
        return _fixed
    if devices_off():
        raise RuntimeError("Real phones are switched off here (PHONEVAULT_NO_DEVICES).")
    problem = ""
    if prefer in ("auto", "adb"):
        adb = adb_for(cfg)
        if adb.available():
            return adb
        problem = adb.problem
        if prefer == "adb" or problem:
            hint = " (phone shows 'unauthorized': tap Allow on the phone)" if adb.unauthorized() else ""
            raise RuntimeError(problem or ("adb sees no phone. Turn on USB debugging, plug the cable in, and tap Allow" + hint))
    if prefer in ("auto", "mtp") and check_mtp:
        mtp = MtpTransport(cfg.phone_name_hint)
        if mtp.available():
            return mtp
    raise RuntimeError("No phone found. Turn on USB debugging, plug the cable in, unlock the phone and tap Allow. "
                       "No phone here? Try: phonevault demo")
