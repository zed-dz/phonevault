"""MTP link: the phone as it shows under "This PC" in Explorer. Needs nothing on the phone.

Set phone_name_hint to the name you see there; without it this link stays off, so it can never open
a disk or another device by mistake.

Slow (Windows asks the phone one item at a time) and the phone must be unlocked
with "File transfer" chosen on the USB popup. Good enough as the fallback when
USB debugging is off. Driven through PowerShell + Shell.Application, no extra
install.
"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from ..core.config import devices_off
from .base import DoneCb, RemoteFile, Transport

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PS_PROLOGUE = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
$sh = New-Object -ComObject Shell.Application
$phone = $sh.NameSpace(17).Items() | Where-Object { $_.Name -like ("*" + $env:PV_PHONE + "*") } | Select-Object -First 1
if (-not $phone) { Write-Output "ERR`tphone not found on This PC"; exit 2 }
$storage = $phone.GetFolder.Items() | Where-Object { $_.IsFolder } | Select-Object -First 1
if (-not $storage) { Write-Output "ERR`tphone storage not visible (unlock the phone, choose File transfer)"; exit 3 }
$root = $storage.GetFolder
function Get-Items($folder) {
  # Windows sometimes answers an empty list the first time it opens a phone folder. Ask again.
  $items = $folder.Items()
  $try = 0
  while ($items.Count -eq 0 -and $try -lt 3) { Start-Sleep -Milliseconds 400; $items = $folder.Items(); $try++ }
  return $items
}
function Get-Sub($folder, $name) {
  foreach ($i in (Get-Items $folder)) { if ($i.Name -eq $name) { return $i } }
  return $null
}
function Resolve-Folder($relPath) {
  $f = $root
  if ($relPath -ne "") {
    foreach ($part in $relPath.Split("/")) {
      $it = Get-Sub $f $part
      if (-not $it) { return $null }
      $f = $it.GetFolder
    }
  }
  return $f
}
"""

PS_LIST = PS_PROLOGUE + r"""
$maxDepth = [int]$env:PV_DEPTH
function Walk($folder, $path, $depth) {
  foreach ($it in (Get-Items $folder)) {
    $p = if ($path -eq "") { $it.Name } else { $path + "/" + $it.Name }
    if ($it.IsFolder) {
      if ($depth -lt $maxDepth) { Walk $it.GetFolder $p ($depth + 1) }
    } else {
      $size = $it.ExtendedProperty("System.Size")
      $mod = $it.ExtendedProperty("System.DateModified")
      $ms = ""
      if ($mod) { try { $ms = ([DateTimeOffset]$mod).ToUnixTimeSeconds() } catch { $ms = "" } }
      Write-Output ("F`t" + $p + "`t" + $size + "`t" + $ms)
    }
  }
}
$start = Resolve-Folder $env:PV_ROOT
if (-not $start) { Write-Output ("ERR`tfolder not found: " + $env:PV_ROOT); exit 4 }
Walk $start $env:PV_ROOT 0
Write-Output "END"
"""

PS_PULL = PS_PROLOGUE + r"""
$manifest = Get-Content -LiteralPath $env:PV_MANIFEST -Encoding UTF8
$cache = @{}
foreach ($line in $manifest) {
  if ($line -eq "") { continue }
  $parts = $line.Split("`t")
  $remote = $parts[0]; $local = $parts[1]; $size = [long]$parts[2]
  Write-Output ("START`t" + $remote)
  try {
    $dir = ""; $name = $remote
    if ($remote.Contains("/")) { $dir = $remote.Substring(0, $remote.LastIndexOf("/")); $name = $remote.Substring($remote.LastIndexOf("/") + 1) }
    if (-not $cache.ContainsKey($dir)) { $cache[$dir] = Resolve-Folder $dir }
    $folder = $cache[$dir]
    if (-not $folder) { throw "folder not found: $dir" }
    $item = Get-Sub $folder $name
    $retry = 0
    while (-not $item -and $retry -lt 3) {
      # big phone folders sometimes come back short the first time: open the folder again and look once more
      Start-Sleep -Milliseconds (800 * ($retry + 1))
      $cache[$dir] = Resolve-Folder $dir
      $folder = $cache[$dir]
      if ($folder) { $item = Get-Sub $folder $name }
      $retry++
    }
    if (-not $item) { throw "file not found on phone" }
    $tmpDir = Join-Path (Split-Path -Parent $local) ("mtp_" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $tmpDir | Out-Null
    $dest = $sh.NameSpace($tmpDir)
    $dest.CopyHere($item, 4 + 16 + 512 + 1024)
    $target = Join-Path $tmpDir $name
    $waited = 0; $last = -1; $stable = 0
    while ($true) {
      Start-Sleep -Milliseconds 250; $waited += 0.25
      if (Test-Path -LiteralPath $target) {
        $cur = (Get-Item -LiteralPath $target).Length
        if ($size -gt 0 -and $cur -eq $size) { Start-Sleep -Milliseconds 150; break }
        if ($cur -eq $last) { $stable += 0.25 } else { $stable = 0 }
        $last = $cur
        if ($size -le 0 -and $stable -ge 3) { break }
        if ($stable -ge 60) { throw "copy stalled" }
      }
      if ($waited -gt 7200) { throw "copy timed out" }
    }
    Move-Item -LiteralPath $target -Destination $local -Force
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Output ("OK`t" + $remote)
  } catch {
    Write-Output ("FAIL`t" + $remote + "`t" + $_.Exception.Message)
  }
}
Write-Output "END"
"""


class MtpError(RuntimeError):
    pass


class MtpTransport(Transport):
    name = "mtp"

    def __init__(self, phone_name_hint: str = ""):
        self.hint = phone_name_hint

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PV_PHONE"] = self.hint
        env.update(extra)
        return env

    def _run_script(self, script: str, env: dict[str, str], timeout: int = 3600) -> list[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
            f.write(script)
            path = f.name
        try:
            p = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                               capture_output=True, env=env, timeout=timeout, creationflags=_NO_WINDOW)
            out = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
            lines = out.split("\n")
            for line in lines:
                if line.startswith("ERR\t"):
                    raise MtpError(line.split("\t", 1)[1])
            return lines
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def available(self) -> bool:
        if os.name != "nt" or not self.hint.strip() or devices_off():
            return False
        try:
            lines = self._run_script(PS_PROLOGUE + '\nWrite-Output ("OK`t" + $storage.Name)\n', self._env(), timeout=90)
        except Exception:
            return False
        return any(line.startswith("OK\t") for line in lines)

    def describe(self) -> str:
        return f"MTP ({self.hint})"

    def list_tree(self, root: str, max_depth: int = 6) -> list[RemoteFile]:
        lines = self._run_script(PS_LIST, self._env(PV_ROOT=root, PV_DEPTH=str(max_depth)), timeout=7200)
        files: list[RemoteFile] = []
        for line in lines:
            if not line.startswith("F\t"):
                continue
            _, path, size_s, mtime_s = (line.split("\t") + ["", "", ""])[:4]
            try:
                size = int(size_s) if size_s else 0
            except ValueError:
                size = 0
            try:
                mtime = float(mtime_s) if mtime_s else None
            except ValueError:
                mtime = None
            files.append(RemoteFile(path, path.rsplit("/", 1)[-1], size, mtime))
        return files

    def list_dir(self, path: str) -> list[RemoteFile]:
        script = PS_PROLOGUE + r"""
$f = Resolve-Folder $env:PV_ROOT
if (-not $f) { Write-Output ("ERR`tfolder not found: " + $env:PV_ROOT); exit 4 }
foreach ($it in (Get-Items $f)) { Write-Output (("D","F")[[int](-not $it.IsFolder)] + "`t" + $it.Name) }
Write-Output "END"
"""
        lines = self._run_script(script, self._env(PV_ROOT=path), timeout=600)
        items = []
        for line in lines:
            if line.startswith("D\t") or line.startswith("F\t"):
                kind, name = line.split("\t", 1)
                rel = f"{path}/{name}".strip("/")
                items.append(RemoteFile(rel, name, 0, None, kind == "D"))
        return items

    def pull(self, remote_path: str, local_path: Path) -> None:
        result: dict[str, tuple[bool, str]] = {}
        self.pull_batch([(remote_path, Path(local_path))], lambda r, l, ok, e: result.__setitem__(r, (ok, e)))
        ok, err = result.get(remote_path, (False, "no result"))
        if not ok:
            raise MtpError(err)

    def pull_batch(self, pairs: list[tuple[str, Path]], on_done: DoneCb,
                   cancel: Callable[[], bool] | None = None,
                   on_start: Callable[[str], None] | None = None,
                   sizes: dict[str, int] | None = None) -> None:
        if not pairs:
            return
        sizes = sizes or {}
        with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8") as mf:
            for remote, local in pairs:
                Path(local).parent.mkdir(parents=True, exist_ok=True)
                mf.write(f"{remote}\t{local}\t{sizes.get(remote, 0)}\n")
            manifest = mf.name
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as sf:
            sf.write(PS_PULL)
            script = sf.name
        local_of = dict(pairs)
        proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=self._env(PV_MANIFEST=manifest), creationflags=_NO_WINDOW)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if cancel and cancel():
                    proc.kill()
                    break
                if line.startswith("START\t"):
                    if on_start:
                        on_start(line.split("\t", 1)[1])
                elif line.startswith("OK\t"):
                    r = line.split("\t", 1)[1]
                    on_done(r, Path(local_of[r]), True, "")
                elif line.startswith("FAIL\t"):
                    _, r, err = (line.split("\t", 2) + [""])[:3]
                    on_done(r, Path(local_of.get(r, "")), False, err)
                elif line.startswith("ERR\t"):
                    raise MtpError(line.split("\t", 1)[1])
            proc.wait(timeout=30)
        finally:
            for p in (manifest, script):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def to_epoch(dt: _dt.datetime) -> float:
    return dt.timestamp()
