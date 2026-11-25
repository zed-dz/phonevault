"""One progress bus. The CLI bar and the web dashboard both listen here.

State is a plain dict snapshot (safe to JSON). Events are small dicts pushed
to every subscriber queue. Thread-safe; the engine runs in a worker thread.
"""
from __future__ import annotations

import collections
import queue
import threading
import time
from typing import Any


class Events:
    def __init__(self, log_size: int = 300):
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self.log: collections.deque = collections.deque(maxlen=log_size)
        self.state: dict[str, Any] = self.blank()
        self._t0 = 0.0
        self._bytes_t0 = 0
        self._ticked = 0.0

    @staticmethod
    def blank() -> dict[str, Any]:
        return {
            "phase": "idle",        # idle | scanning | planning | copying | undoing | done | failed | cancelled
            "kind": "",
            "run_id": "",
            "total_files": 0, "done_files": 0, "skipped": 0, "failed": 0, "dups": 0,
            "total_bytes": 0, "done_bytes": 0, "file_bytes": 0, "file_done_bytes": 0,
            "current": "", "current_album": "", "current_dst": "",
            "speed_bps": 0.0, "eta_s": None,
            "albums": {},           # album -> {planned, copied, dups, skipped, failed, bytes}
            "message": "",
            "started": None, "finished": None,
            "transport": "",
        }

    # ---- pub/sub --------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, type_: str, **data: Any) -> None:
        ev = {"type": type_, "t": time.time(), **data}
        with self._lock:
            if type_ in ("log", "file_done", "file_failed", "run_start", "run_done", "warn"):
                self.log.append(ev)
            for q in list(self._subs):
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    # ---- state helpers --------------------------------------------------
    def set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)
        self.emit("state", **kw)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = dict(self.state)
            s["albums"] = {k: dict(v) for k, v in self.state["albums"].items()}
            s["log"] = list(self.log)[-60:]
            return s

    def log_line(self, text: str, level: str = "info") -> None:
        self.emit("log", text=text, level=level)

    def reset(self, kind: str, transport: str = "") -> None:
        with self._lock:
            self.state = self.blank()
            self.state["kind"] = kind
            self.state["transport"] = transport
            self.state["started"] = time.time()
            self._t0 = time.time()
            self._bytes_t0 = 0
        self.emit("state", **self.snapshot())

    def album_bump(self, album: str, field: str, n: int = 1) -> None:
        with self._lock:
            a = self.state["albums"].setdefault(album, {"planned": 0, "copied": 0, "dups": 0, "skipped": 0, "failed": 0, "bytes": 0})
            a[field] = a.get(field, 0) + n

    def tick_speed(self) -> None:
        """Recompute speed + ETA from done_bytes."""
        with self._lock:
            s = self.state
            el = max(time.time() - (s["started"] or time.time()), 0.001)
            speed = s["done_bytes"] / el
            s["speed_bps"] = speed
            left = s["total_bytes"] - s["done_bytes"]
            s["eta_s"] = (left / speed) if speed > 0 and left > 0 else None
            if time.time() - self._ticked < 0.35:
                return
            self._ticked = time.time()
            live = {"speed_bps": speed, "eta_s": s["eta_s"], "albums": {k: dict(v) for k, v in s["albums"].items()}}
        self.emit("state", **live)      # so an open page shows the speed and the album table while it runs
