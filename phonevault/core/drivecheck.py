"""Is the target drive healthy enough to write to? Learned the hard way: an external USB disk started
throwing hardware errors mid-backup and everything written that day came back unreadable. So before
any run we ask Windows two things: the volume's own health flag, and whether the disk logged hardware
errors in the last minutes. On Linux and macOS this check is skipped.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_cache: dict[str, tuple[float, dict]] = {}

PS = r"""
$letter = $env:PV_LETTER
$out = @{ letter = $letter; health = "unknown"; status = "unknown"; recent_errors = 0; free_gb = $null }
try {
  $v = Get-Volume -DriveLetter $letter -ErrorAction Stop
  $out.health = [string]$v.HealthStatus
  $out.status = [string]$v.OperationalStatus
  $out.free_gb = [math]::Round($v.SizeRemaining / 1GB, 1)
} catch { $out.health = "no-volume" }
try {
  $disk = (Get-Partition -DriveLetter $letter -ErrorAction Stop).DiskNumber
  $out.disk = $disk
  $since = (Get-Date).AddMinutes(-15)
  $ev = Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "disk"; StartTime = $since } -ErrorAction Stop
  $pat = "Disk " + $disk + " "
  $out.recent_errors = @($ev | Where-Object { $_.Id -in 7, 11, 51, 153, 154, 157 -and $_.Message -like ("*" + $pat + "*") }).Count
} catch { }
$out | ConvertTo-Json -Compress
"""


def check_drive(path: Path | str, ttl: float = 60.0) -> dict:
    """Returns {letter, health, status, recent_errors, free_gb, ok, reason}. Never raises."""
    p = Path(path)
    letter = (p.drive or "").rstrip(":")
    if os.environ.get("PHONEVAULT_DRIVECHECK", "").lower() in ("off", "0", "false"):
        return {"letter": letter, "health": "skipped", "status": "skipped", "recent_errors": 0, "free_gb": None, "ok": True, "reason": ""}
    if os.name != "nt" or not letter:
        return {"letter": letter, "health": "n/a", "status": "n/a", "recent_errors": 0, "free_gb": None, "ok": True, "reason": ""}
    now = time.time()
    hit = _cache.get(letter)
    if hit and now - hit[0] < ttl:
        return hit[1]
    info: dict = {"letter": letter, "health": "unknown", "status": "unknown", "recent_errors": 0, "free_gb": None}
    try:
        env = dict(os.environ, PV_LETTER=letter)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", PS], capture_output=True, timeout=40,
                           env=env, creationflags=_NO_WINDOW)
        data = json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
        info.update({k: data.get(k, info[k]) for k in info})
    except Exception as e:  # noqa: BLE001
        info["error"] = str(e)
    bad_status = any(w in str(info["status"]).lower() for w in ("repair", "error", "fail", "unavailable"))
    bad_health = str(info["health"]).lower() in ("unhealthy", "warning", "no-volume")
    reasons = []
    if bad_health or bad_status:
        reasons.append(f"Windows reports volume {letter}: health={info['health']}, status={info['status']}")
    if info["recent_errors"]:
        reasons.append(f"{info['recent_errors']} disk hardware error(s) in the last 15 minutes")
    if info["free_gb"] is not None and info["free_gb"] < 2:
        reasons.append(f"only {info['free_gb']} GB free")
    info["ok"] = not reasons
    info["reason"] = "; ".join(reasons)
    _cache[letter] = (now, info)
    return info


def refuse_if_unhealthy(path: Path | str) -> None:
    info = check_drive(path)
    if not info["ok"]:
        raise RuntimeError(f"Target drive {info['letter']}: is not safe to write to. {info['reason']}. "
                           "Nothing was written. Fix the drive or change pc_root.")
