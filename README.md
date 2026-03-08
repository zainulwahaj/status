# Status Monitor — anosha.online

Windows XP–themed dashboard to monitor all services on **zainhomelab**.

![XP.css](https://img.shields.io/badge/theme-XP.css-blue)
![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688)

## Features

- **System stats** — CPU, RAM, Disk usage (progress bars), temps, uptime, net/disk I/O
- **Container monitoring** — status, health, restart count for all Docker containers
- **PM2 monitoring** — status, CPU, memory for PM2-managed processes
- **Live logs** — SSE-streamed real-time logs from any container or PM2 process
- **Alerts** — automatic warnings when services are down, unhealthy, or restarting
- **Quick links** — one-click access to all project UIs and Portainer

## Deploy on Server

### 1. Clone

```bash
cd ~
git clone <your-repo-url> status-monitor
cd status-monitor
```

### 2. Build & Run

```bash
docker compose up -d --build
```

Verify:

```bash
docker compose ps              # should show status-monitor Up (healthy)
curl http://localhost:4000/api/health   # {"status":"ok",...}
curl http://localhost:4000/api/system   # system stats JSON
curl http://localhost:4000/api/containers  # all containers
```

### 3. Add to Cloudflare Tunnel

Edit `/etc/cloudflared/config.yml` — add this **before** the catch-all rule:

```yaml
  - hostname: status.anosha.online
    service: http://localhost:4000
```

Full file should look like:

```yaml
tunnel: 63880ad4-a40a-40eb-973f-074f7e97bcaf
credentials-file: /home/zain/.cloudflared/63880ad4-a40a-40eb-973f-074f7e97bcaf.json

ingress:
  - hostname: app.anosha.online
    service: http://localhost:3000
  - hostname: api.anosha.online
    service: http://localhost:8000
  - hostname: ml.anosha.online
    service: http://localhost:3001
  - hostname: mlapi.anosha.online
    service: http://localhost:8001
  - hostname: status.anosha.online
    service: http://localhost:4000
  - service: http_status:404
```

Then restart cloudflared:

```bash
sudo systemctl restart cloudflared
```

### 4. Add DNS record

In Cloudflare Dashboard → DNS → add a CNAME record:

| Type  | Name   | Target                                             | Proxy |
|-------|--------|----------------------------------------------------|-------|
| CNAME | status | 63880ad4-a40a-40eb-973f-074f7e97bcaf.cfargotunnel.com | On    |

### 5. Done!

Visit **https://status.anosha.online** 🎉

## Architecture

```
┌─────────────────────────────────────────────┐
│  Docker: status-monitor (port 4000)         │
│  ┌───────────────────────────────────────┐  │
│  │  FastAPI (uvicorn)                    │  │
│  │  ├─ /api/system    (psutil)           │  │
│  │  ├─ /api/containers (docker-py)       │  │
│  │  ├─ /api/pm2       (pm2 jlist)        │  │
│  │  ├─ /api/alerts    (derived)          │  │
│  │  ├─ /api/logs/*    (fetch last N)     │  │
│  │  ├─ /api/stream/*  (SSE live)         │  │
│  │  └─ /              (index.html)       │  │
│  └───────────────────────────────────────┘  │
│  Volumes:                                    │
│   - /var/run/docker.sock  (Docker API)       │
│   - ~/.pm2                (PM2 socket+logs)  │
│   - /sys/class/thermal    (CPU temps)        │
│   - ./frontend            (static HTML)      │
└─────────────────────────────────────────────┘
```

## Local Development

You can't fully test on macOS (no Docker socket access in the same way), but you can verify the frontend:

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 4000 --reload
# open http://localhost:4000 — APIs will fail but the UI loads
```

## Customization

- **Add a new project**: edit `PROJECT_MAP` in `frontend/index.html` and map container/pm2 names
- **Change Portainer link**: update the quick-link href in `frontend/index.html`
- **Adjust polling intervals**: see the `setInterval` calls at the bottom of `frontend/index.html`
