"""Detailed host + process resource snapshot for the System Resources view.

Everything is best-effort and guarded: a missing psutil or an unsupported field
on the current OS degrades to null rather than raising. Rate figures (disk I/O,
network) are computed as deltas since the previous call, so the first poll after
startup reports 0 and subsequent polls (every ~5s) are accurate.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import time
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

_MB = 1024 * 1024
_GB = 1024 ** 3

# Rate state (deltas across calls) and a reused Process handle for its CPU timer.
_last: dict[str, Any] = {"ts": None, "disk": None, "net": None}
_proc: Any = None


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _empty(detail: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": {"app_name": "AIVA"},
        "cpu": {},
        "memory": {},
        "swap": None,
        "disk": {},
        "disk_io": {},
        "network": {},
        "process": {},
        "uptime_seconds": None,
        "boot_time": None,
        "detail": detail,
    }


def resource_snapshot() -> dict[str, Any]:
    """Full host/process resource snapshot. Blocks only trivially; call off-loop."""
    global _proc
    try:
        import psutil
    except Exception:
        return _empty("psutil not installed")

    try:
        now = time.monotonic()

        # ---- platform ----
        platform_info = {
            "app_name": "AIVA",
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}".strip(),
            "os_detail": platform.platform(),
            "python_version": platform.python_version(),
        }

        # ---- cpu (interval=None → delta since last call, non-blocking) ----
        cpu: dict[str, Any] = {}
        try:
            cpu["percent"] = _round(psutil.cpu_percent(interval=None), 1)
            cpu["per_core"] = [_round(v, 1) for v in psutil.cpu_percent(interval=None, percpu=True)]
            cpu["cores"] = psutil.cpu_count(logical=True)
            freq = psutil.cpu_freq()
            cpu["freq_mhz"] = _round(freq.current, 0) if freq else None
            t = psutil.cpu_times_percent(interval=None)
            cpu["times"] = {
                "user": _round(getattr(t, "user", None), 1),
                "system": _round(getattr(t, "system", None), 1),
                "idle": _round(getattr(t, "idle", None), 1),
                "iowait": _round(getattr(t, "iowait", None), 1),
            }
        except Exception:
            _log.debug("cpu probe partial failure", exc_info=True)
        try:
            la = psutil.getloadavg()
            cpu["load_avg"] = [_round(x, 2) for x in la]
        except Exception:
            cpu["load_avg"] = None

        # ---- memory ----
        memory: dict[str, Any] = {}
        try:
            vm = psutil.virtual_memory()
            memory = {
                "total_mb": _round(vm.total / _MB, 0),
                "used_mb": _round(vm.used / _MB, 0),
                "available_mb": _round(vm.available / _MB, 0),
                "percent": _round(vm.percent, 1),
                "cached_mb": _round(getattr(vm, "cached", 0) / _MB, 0) if hasattr(vm, "cached") else None,
                "buffers_mb": _round(getattr(vm, "buffers", 0) / _MB, 0) if hasattr(vm, "buffers") else None,
            }
        except Exception:
            _log.debug("memory probe failed", exc_info=True)

        swap: dict[str, Any] | None = None
        try:
            sw = psutil.swap_memory()
            if sw.total > 0:
                swap = {
                    "total_mb": _round(sw.total / _MB, 0),
                    "used_mb": _round(sw.used / _MB, 0),
                    "percent": _round(sw.percent, 1),
                }
        except Exception:
            swap = None

        # ---- disk usage ----
        disk: dict[str, Any] = {}
        try:
            du = psutil.disk_usage(os.path.abspath(os.sep))
            disk = {
                "total_gb": _round(du.total / _GB, 2),
                "used_gb": _round(du.used / _GB, 2),
                "free_gb": _round(du.free / _GB, 2),
                "percent": _round(du.percent, 1),
            }
        except Exception:
            _log.debug("disk usage probe failed", exc_info=True)

        # ---- disk I/O + network rates (deltas) ----
        dt = None if _last["ts"] is None else max(1e-6, now - _last["ts"])

        disk_io: dict[str, Any] = {}
        try:
            io = psutil.disk_io_counters()
            if io is not None:
                disk_io["read_total"] = int(io.read_bytes)
                disk_io["write_total"] = int(io.write_bytes)
                if dt and _last["disk"] is not None:
                    prev = _last["disk"]
                    disk_io["read_mbps"] = _round((io.read_bytes - prev.read_bytes) / dt / _MB, 2)
                    disk_io["write_mbps"] = _round((io.write_bytes - prev.write_bytes) / dt / _MB, 2)
                    disk_io["read_iops"] = _round((io.read_count - prev.read_count) / dt, 0)
                    disk_io["write_iops"] = _round((io.write_count - prev.write_count) / dt, 0)
                else:
                    disk_io.update({"read_mbps": 0.0, "write_mbps": 0.0, "read_iops": 0.0, "write_iops": 0.0})
                _last["disk"] = io
        except Exception:
            _log.debug("disk io probe failed", exc_info=True)

        network: dict[str, Any] = {}
        try:
            net = psutil.net_io_counters()
            if net is not None:
                network = {
                    "sent_total_gb": _round(net.bytes_sent / _GB, 2),
                    "recv_total_gb": _round(net.bytes_recv / _GB, 2),
                    "packets_sent": int(net.packets_sent),
                    "packets_recv": int(net.packets_recv),
                    "errin": int(net.errin),
                    "errout": int(net.errout),
                    "dropin": int(net.dropin),
                    "dropout": int(net.dropout),
                    "sent_mbps": 0.0,
                    "recv_mbps": 0.0,
                }
                if dt and _last["net"] is not None:
                    prev = _last["net"]
                    network["sent_mbps"] = _round((net.bytes_sent - prev.bytes_sent) / dt / _MB, 2)
                    network["recv_mbps"] = _round((net.bytes_recv - prev.bytes_recv) / dt / _MB, 2)
                _last["net"] = net
        except Exception:
            _log.debug("network probe failed", exc_info=True)

        _last["ts"] = now

        # ---- backend process ----
        process: dict[str, Any] = {}
        try:
            if _proc is None:
                _proc = psutil.Process()
                _proc.cpu_percent(interval=None)  # prime the CPU timer
            with _proc.oneshot():
                process = {
                    "pid": _proc.pid,
                    "memory_mb": _round(_proc.memory_info().rss / _MB, 1),
                    "num_threads": _proc.num_threads(),
                    "cpu_percent": _round(_proc.cpu_percent(interval=None), 1),
                }
        except Exception:
            _log.debug("process probe failed", exc_info=True)

        # ---- uptime ----
        uptime_seconds = None
        boot_time = None
        try:
            bt = psutil.boot_time()
            uptime_seconds = int(time.time() - bt)
            boot_time = datetime.fromtimestamp(bt, tz=timezone.utc).isoformat()
        except Exception:
            pass

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform_info,
            "cpu": cpu,
            "memory": memory,
            "swap": swap,
            "disk": disk,
            "disk_io": disk_io,
            "network": network,
            "process": process,
            "uptime_seconds": uptime_seconds,
            "boot_time": boot_time,
            "detail": None,
        }
    except Exception as exc:
        _log.warning("resource snapshot failed", exc_info=True)
        return _empty(str(exc)[:200])
