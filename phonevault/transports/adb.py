"""adb link: USB cable or Wi-Fi (adb connect ip:5555). Fast, and it has a shell.

Needs USB debugging ON in Developer options, and the "Allow this computer"
tick once. After `phonevault wifi` the cable can go.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable

from .base import DoneCb, RemoteFile, Transport

SDCARD = "/sdcard"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AdbError(RuntimeError):
    pass


class AdbTransport(Transport):
    name = "adb"

    def __init__(self, adb_path: str, serial: str = "", wifi_address: str = ""):
        self.adb = adb_path
        self.serial = serial
        self._picked = not serial  # True = we choose the phone; False = the owner pinned a serial
        self.wifi_address = wifi_address
        self._desc = ""
        self.problem = ""          # plain words when a phone is there but we will not use it

    # ---- process plumbing -----------------------------------------------
    def _base(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args: str, timeout: int = 120, check: bool = True, binary: bool = False) -> str | bytes:
        cmd = self._base() + list(args)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
        if check and p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip() or p.stdout.decode("utf-8", "replace").strip()
            raise AdbError(f"adb {' '.join(args[:3])}: {err}")
        return p.stdout if binary else p.stdout.decode("utf-8", "replace")

    def shell(self, command: str, timeout: int = 120) -> str:
        return str(self.run("shell", command, timeout=timeout)).replace("\r\n", "\n")

    # ---- devices --------------------------------------------------------
    def devices(self) -> list[dict]:
        try:
            out = str(self.run("devices", "-l", timeout=20, check=False))
        except Exception:
            return []
        devs = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            d = {"serial": parts[0], "state": parts[1], "wifi": ":" in parts[0]}
            for kv in parts[2:]:
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    d[k] = v
            devs.append(d)
        return devs

    @staticmethod
    def is_emulator(serial: str) -> bool:
        return serial.startswith("emulator-")

    def _real_devices(self) -> list[dict]:
        """Devices that are a phone: online, and never an Android emulator (one once got backed up as if it
        were the phone). An emulator is only used when its serial was pinned on purpose (device_serial)."""
        return [d for d in self.devices() if d["state"] == "device"
                and (not self.is_emulator(d["serial"]) or d["serial"] == self.serial)]

    def available(self) -> bool:
        self.problem = ""
        if not self.adb or not Path(self.adb).exists():
            return False
        devs = self._real_devices()
        if not devs and self.wifi_address:
            try:
                self.run("connect", self.wifi_address, timeout=10, check=False)
                devs = self._real_devices()
            except Exception:
                devs = []
        if not devs:
            return False
        if self.serial and self.serial not in [d["serial"] for d in devs] and not self._picked:
            self.problem = f"The phone {self.serial} (device_serial) is not connected."
            return False
        if not self.serial or self.serial not in [d["serial"] for d in devs]:
            usb = [d for d in devs if not d["wifi"]]
            if len(usb or devs) > 1:
                self.problem = ("More than one phone is connected. Unplug one, or choose: "
                                "phonevault config --set device_serial=<serial>  (phonevault status lists them)")
                return False
            self.serial = (usb or devs)[0]["serial"]
            self._picked = True
        chosen = next(d for d in devs if d["serial"] == self.serial)
        self._desc = f"adb {'Wi-Fi' if chosen['wifi'] else 'USB'} ({chosen.get('model', self.serial)})"
        return True

    def unauthorized(self) -> bool:
        return any(d["state"] == "unauthorized" and not self.is_emulator(d["serial"]) for d in self.devices())

    def describe(self) -> str:
        return self._desc or "adb"

    @property
    def has_shell(self) -> bool:
        return True

    # ---- device facts ---------------------------------------------------
    def props(self) -> dict[str, str]:
        out = self.shell("getprop")
        props = {}
        for m in re.finditer(r"\[(.+?)\]: \[(.*?)\]", out):
            props[m.group(1)] = m.group(2)
        return props

    def ip_address(self) -> str | None:
        out = self.shell("ip -f inet addr show wlan0 2>/dev/null || ip route")
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None

    def enable_wifi(self, port: int = 5555) -> str:
        """Switch adbd to TCP. Must be on USB when called. Returns 'ip:port'."""
        ip = self.ip_address()
        if not ip:
            raise AdbError("phone has no Wi-Fi address; join the same Wi-Fi as the PC first")
        self.run("tcpip", str(port), timeout=20)
        time.sleep(2)
        addr = f"{ip}:{port}"
        out = str(self.run("connect", addr, timeout=15, check=False))
        if "connected" not in out and "already" not in out:
            raise AdbError(f"adb connect {addr}: {out.strip()}")
        self.wifi_address = addr
        return addr

    # ---- find the phone on the network -----------------------------------
    def mdns_candidates(self) -> list[str]:
        """'ip:port' of every phone adb announces on this Wi-Fi (wireless debugging or tcpip mode)."""
        try:
            out = str(self.run("mdns", "services", timeout=15, check=False))
        except Exception:
            return []
        return parse_mdns(out)

    @staticmethod
    def tailscale_android_ips() -> list[str]:
        """Tailscale peers that look like phones (OS android), by their 100.x address. Empty if no Tailscale."""
        import json as _json
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe") if os.name == "nt" else "tailscale"
        try:
            p = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=15, creationflags=_NO_WINDOW)
            data = _json.loads(p.stdout.decode("utf-8", "replace") or "{}")
        except Exception:
            return []
        ips = []
        for peer in (data.get("Peer") or {}).values():
            if str(peer.get("OS", "")).lower().startswith("android") and peer.get("TailscaleIPs"):
                ips.append(peer["TailscaleIPs"][0])
        return ips

    def try_connect(self, addr: str) -> bool:
        try:
            out = str(self.run("connect", addr, timeout=12, check=False))
        except Exception:
            return False
        if "connected" in out and "cannot" not in out and "failed" not in out:
            return any(d["serial"] == addr and d["state"] == "device" for d in self.devices())
        return False

    def discover(self, saved: str = "", pin_port: int = 5555) -> str | None:
        """Find the phone without a cable. Order: saved address, Wi-Fi mDNS, Tailscale peers on the pinned port.
        When a mDNS (random-port) link works, pin adbd to a fixed port so Tailscale can reach it too."""
        tried: list[str] = []
        for addr in ([saved] if saved else []):
            tried.append(addr)
            if self.try_connect(addr):
                self.serial = addr
                return addr
        for addr in self.mdns_candidates():
            if addr in tried:
                continue
            tried.append(addr)
            if self.try_connect(addr):
                self.serial = addr
                ip = addr.rsplit(":", 1)[0]
                try:
                    self.run("tcpip", str(pin_port), timeout=20)
                    time.sleep(2)
                    pinned = f"{ip}:{pin_port}"
                    if self.try_connect(pinned):
                        self.serial = pinned
                        return pinned
                except Exception:
                    pass
                return addr
        for ip in self.tailscale_android_ips():
            addr = f"{ip}:{pin_port}"
            if addr in tried:
                continue
            if self.try_connect(addr):
                self.serial = addr
                return addr
        return None

    # ---- listing --------------------------------------------------------
    def list_tree(self, root: str, max_depth: int = 6) -> list[RemoteFile]:
        """find + stat in one shell call: size, mtime, path. Handles spaces and Arabic."""
        base = f"{SDCARD}/{root}".rstrip("/")
        q = shlex.quote(base)
        out = self.shell(f"find {q} -type f -exec stat -c '%s\t%Y\t%n' {{}} + 2>/dev/null", timeout=600)
        files: list[RemoteFile] = []
        prefix = SDCARD + "/"
        for line in out.split("\n"):
            if not line or "\t" not in line:
                continue
            try:
                size_s, mtime_s, path = line.split("\t", 2)
                size = int(size_s)
                mtime = float(mtime_s)
            except ValueError:
                continue
            rel = path[len(prefix):] if path.startswith(prefix) else path
            files.append(RemoteFile(rel, rel.rsplit("/", 1)[-1], size, mtime))
        return files

    def list_dir(self, path: str) -> list[RemoteFile]:
        base = f"{SDCARD}/{path}".rstrip("/")
        out = self.shell(f"ls -1p {shlex.quote(base)} 2>/dev/null")
        items = []
        for name in out.split("\n"):
            if not name:
                continue
            is_dir = name.endswith("/")
            name = name.rstrip("/")
            rel = f"{path}/{name}".strip("/")
            items.append(RemoteFile(rel, name, 0, None, is_dir))
        return items

    def top_folders(self) -> list[str]:
        return [f.name for f in self.list_dir("") if f.is_dir]

    # ---- transfer -------------------------------------------------------
    def pull(self, remote_path: str, local_path: Path) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", "-a", f"{SDCARD}/{remote_path}", str(local_path), timeout=3600)

    def pull_batch(self, pairs: list[tuple[str, Path]], on_done: DoneCb,
                   cancel: Callable[[], bool] | None = None,
                   on_start: Callable[[str], None] | None = None,
                   sizes: dict[str, int] | None = None) -> None:
        for remote, local in pairs:
            if cancel and cancel():
                return
            if on_start:
                on_start(remote)
            try:
                self.pull(remote, local)
                on_done(remote, local, True, "")
            except Exception as e:  # noqa: BLE001
                on_done(remote, local, False, str(e))

    def push(self, local_path: Path, remote_path: str) -> None:
        """Only `phonevault restore` uses this. Backup and undo never write to the phone."""
        self.run("push", str(local_path), f"{SDCARD}/{remote_path}", timeout=3600)

    def sha256(self, remote_path: str) -> str | None:
        out = self.shell(f"sha256sum {shlex.quote(SDCARD + '/' + remote_path)} 2>/dev/null", timeout=3600).strip()
        m = re.match(r"\\?([0-9a-f]{64})\b", out)
        return m.group(1) if m else None

    def pull_abs(self, remote_abs: str, local_path: Path) -> None:
        """Pull a path outside /sdcard (APKs live under /data/app)."""
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self.run("pull", remote_abs, str(local_path), timeout=1800)


def parse_mdns(out: str) -> list[str]:
    """Lines look like: 'adb-14da5d8b-AbCdEf<TAB>_adb-tls-connect._tcp<TAB>192.168.1.20:37099'. Pairing entries skipped."""
    found: list[str] = []
    for line in out.replace(chr(13), "").split(chr(10)):
        parts = [x for x in line.split(chr(9)) if x] or line.split()
        if len(parts) < 3:
            continue
        svc, addr = parts[1], parts[-1]
        if "pairing" in svc or ":" not in addr:
            continue
        if addr not in found:
            found.append(addr)
    return found
