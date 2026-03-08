import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
import psutil
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

app = FastAPI(title="Status Monitor")

# ---------------------------------------------------------------------------
# Docker client (lazy)
# ---------------------------------------------------------------------------
_docker_client: docker.DockerClient | None = None


def get_docker() -> docker.DockerClient:
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    return _docker_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PM2_HOME = os.environ.get("PM2_HOME", os.path.expanduser("~/.pm2"))


def _bytes_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PB"


def _seconds_human(s: float) -> str:
    days, rem = divmod(int(s), 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# /api/system
# ---------------------------------------------------------------------------
@app.get("/api/system")
async def system_stats():
    cpu_percent = psutil.cpu_percent(interval=0.4)
    cpu_freq = psutil.cpu_freq()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    disk_io = psutil.disk_io_counters()
    boot = psutil.boot_time()
    uptime_s = time.time() - boot

    # Temperatures — try psutil first, fall back to sysfs
    temps: dict[str, float] = {}
    try:
        sensor_temps = psutil.sensors_temperatures()
        if sensor_temps:
            for label, entries in sensor_temps.items():
                for e in entries:
                    key = e.label or label
                    temps[key] = e.current
    except Exception:
        pass

    if not temps:
        thermal_base = Path("/sys/class/thermal")
        if thermal_base.exists():
            for zone in sorted(thermal_base.glob("thermal_zone*")):
                try:
                    t = int((zone / "temp").read_text().strip()) / 1000
                    name = (zone / "type").read_text().strip()
                    temps[name] = round(t, 1)
                except Exception:
                    pass

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(),
        "cpu_freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
        "ram_total": mem.total,
        "ram_used": mem.used,
        "ram_percent": mem.percent,
        "ram_total_h": _bytes_human(mem.total),
        "ram_used_h": _bytes_human(mem.used),
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_percent": disk.percent,
        "disk_total_h": _bytes_human(disk.total),
        "disk_used_h": _bytes_human(disk.used),
        "net_sent": net.bytes_sent,
        "net_recv": net.bytes_recv,
        "net_sent_h": _bytes_human(net.bytes_sent),
        "net_recv_h": _bytes_human(net.bytes_recv),
        "disk_read": disk_io.read_bytes if disk_io else 0,
        "disk_write": disk_io.write_bytes if disk_io else 0,
        "disk_read_h": _bytes_human(disk_io.read_bytes) if disk_io else "N/A",
        "disk_write_h": _bytes_human(disk_io.write_bytes) if disk_io else "N/A",
        "uptime_s": uptime_s,
        "uptime_h": _seconds_human(uptime_s),
        "boot_time": datetime.fromtimestamp(boot, tz=timezone.utc).isoformat(),
        "temps": temps,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# /api/containers
# ---------------------------------------------------------------------------
@app.get("/api/containers")
async def containers():
    client = get_docker()
    results = []
    for c in client.containers.list(all=True):
        c.reload()
        health = c.attrs.get("State", {}).get("Health", {}).get("Status", "none")
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
        results.append(
            {
                "name": c.name,
                "id": c.short_id,
                "image": c.image.tags[0] if c.image.tags else str(c.image.id)[:19],
                "status": c.status,
                "health": health,
                "restart_count": c.attrs.get("RestartCount", 0),
                "created": c.attrs.get("Created", ""),
                "ports": ports,
            }
        )
    return results


# ---------------------------------------------------------------------------
# /api/pm2
# ---------------------------------------------------------------------------
async def _pm2_jlist() -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "pm2", "jlist",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PM2_HOME": PM2_HOME},
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return []


@app.get("/api/pm2")
async def pm2_list():
    raw = await _pm2_jlist()
    results = []
    for p in raw:
        env = p.get("pm2_env", {})
        monit = p.get("monit", {})
        results.append(
            {
                "name": p.get("name"),
                "pm_id": p.get("pm_id"),
                "status": env.get("status"),
                "cpu": monit.get("cpu", 0),
                "memory": monit.get("memory", 0),
                "memory_h": _bytes_human(monit.get("memory", 0)),
                "uptime_ms": env.get("pm_uptime", 0),
                "restart_count": env.get("restart_time", 0),
                "out_log": env.get("pm_out_log_path", ""),
                "err_log": env.get("pm_err_log_path", ""),
            }
        )
    return results


# ---------------------------------------------------------------------------
# /api/alerts
# ---------------------------------------------------------------------------
@app.get("/api/alerts")
async def alerts():
    warnings: list[dict] = []

    # Container alerts
    client = get_docker()
    for c in client.containers.list(all=True):
        c.reload()
        health = c.attrs.get("State", {}).get("Health", {}).get("Status", "none")
        restarts = c.attrs.get("RestartCount", 0)
        if c.status != "running":
            warnings.append(
                {"level": "error", "source": c.name, "type": "container",
                 "message": f"Container is {c.status}"}
            )
        elif health not in ("healthy", "none"):
            warnings.append(
                {"level": "warning", "source": c.name, "type": "container",
                 "message": f"Health: {health}"}
            )
        if restarts > 3:
            warnings.append(
                {"level": "warning", "source": c.name, "type": "container",
                 "message": f"High restart count: {restarts}"}
            )

    # PM2 alerts
    pm2 = await _pm2_jlist()
    for p in pm2:
        env = p.get("pm2_env", {})
        status = env.get("status", "")
        restarts = env.get("restart_time", 0)
        name = p.get("name", "?")
        if status != "online":
            warnings.append(
                {"level": "error", "source": name, "type": "pm2",
                 "message": f"PM2 process is {status}"}
            )
        if restarts > 5:
            warnings.append(
                {"level": "warning", "source": name, "type": "pm2",
                 "message": f"High restart count: {restarts}"}
            )

    # System alerts
    mem = psutil.virtual_memory()
    if mem.percent > 90:
        warnings.append(
            {"level": "warning", "source": "system", "type": "system",
             "message": f"RAM usage at {mem.percent}%"}
        )
    disk = psutil.disk_usage("/")
    if disk.percent > 90:
        warnings.append(
            {"level": "warning", "source": "system", "type": "system",
             "message": f"Disk usage at {disk.percent}%"}
        )

    return warnings


# ---------------------------------------------------------------------------
# /api/logs/container/{name}
# ---------------------------------------------------------------------------
@app.get("/api/logs/container/{name}")
async def container_logs(name: str, lines: int = Query(default=150, le=2000)):
    client = get_docker()
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container '{name}' not found")
    logs = c.logs(tail=lines, timestamps=True).decode(errors="replace")
    return {"name": name, "lines": logs.splitlines()}


# ---------------------------------------------------------------------------
# /api/logs/pm2/{name}
# ---------------------------------------------------------------------------
@app.get("/api/logs/pm2/{name}")
async def pm2_logs(name: str, lines: int = Query(default=150, le=2000), log_type: str = Query(default="out")):
    if log_type not in ("out", "err"):
        raise HTTPException(400, "log_type must be 'out' or 'err'")
    # Sanitize name to prevent path traversal
    safe_name = Path(name).name
    log_file = Path(PM2_HOME) / "logs" / f"{safe_name}-{log_type}.log"
    if not log_file.exists():
        raise HTTPException(404, f"Log file not found for '{safe_name}'")
    # Read last N lines efficiently
    try:
        all_lines = log_file.read_text(errors="replace").splitlines()
        return {"name": safe_name, "lines": all_lines[-lines:]}
    except PermissionError:
        raise HTTPException(403, "Cannot read log file")


# ---------------------------------------------------------------------------
# SSE: /api/stream/container/{name}
# ---------------------------------------------------------------------------
@app.get("/api/stream/container/{name}")
async def stream_container(name: str):
    client = get_docker()
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        raise HTTPException(404, f"Container '{name}' not found")

    async def generate():
        loop = asyncio.get_event_loop()
        log_stream = c.logs(stream=True, follow=True, tail=5, timestamps=True)
        try:
            while True:
                line = await loop.run_in_executor(None, next, log_stream, None)
                if line is None:
                    break
                yield {"data": line.decode(errors="replace").rstrip()}
                await asyncio.sleep(0.05)
        except (GeneratorExit, asyncio.CancelledError):
            pass
        finally:
            log_stream.close()

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# SSE: /api/stream/pm2/{name}
# ---------------------------------------------------------------------------
@app.get("/api/stream/pm2/{name}")
async def stream_pm2(name: str, log_type: str = Query(default="out")):
    if log_type not in ("out", "err"):
        raise HTTPException(400, "log_type must be 'out' or 'err'")
    safe_name = Path(name).name
    log_file = Path(PM2_HOME) / "logs" / f"{safe_name}-{log_type}.log"
    if not log_file.exists():
        raise HTTPException(404, f"Log file not found for '{safe_name}'")

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            "tail", "-n", "5", "-f", str(log_file),
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            while True:
                line = await proc.stdout.readline()  # type: ignore[union-attr]
                if not line:
                    break
                yield {"data": line.decode(errors="replace").rstrip()}
                await asyncio.sleep(0.05)
        except (GeneratorExit, asyncio.CancelledError):
            pass
        finally:
            proc.kill()

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(tz=timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Static files + SPA fallback
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(
    os.environ.get(
        "FRONTEND_DIR",
        str(Path(__file__).resolve().parent / "frontend"),
    )
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse(
            "<html><body><h1>Status Monitor backend is running</h1>"
            "<p>Frontend files were not found inside the container.</p>"
            "</body></html>",
            status_code=503,
        )
    return HTMLResponse(index_file.read_text())
