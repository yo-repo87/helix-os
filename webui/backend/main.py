from contextlib import asynccontextmanager
import asyncio
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from routers import system, array, docker, shares, drives, network, apps, notifications, settings, users, wireguard, tailscale, pia, vms, terminal, ups, gpu, diagnostics, scheduler, checkmk, files, mover, hardware, apikeys, ddns, ftp, updates, vfio, ssl, auth, vmconsole
from routers import backup, zfs, vnstat, proxy, audit, login, wol, scanner, cpu_oc, osupdate, pi_oc, mcp
from services.drive_health import poll_loop
from services.drive_history import history_poll_loop
from routers.audit import AuditMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    task1 = asyncio.create_task(poll_loop())
    task2 = asyncio.create_task(history_poll_loop())
    yield
    task1.cancel()
    task2.cancel()


def _helix_version() -> str:
    """Installed OS version (stamped into /etc/helix-version at build time)."""
    try:
        return open("/etc/helix-version").read().strip() or "unknown"
    except OSError:
        return "unknown"


app = FastAPI(title="Helix OS", version=_helix_version(), docs_url="/api/docs", lifespan=lifespan)

app.add_middleware(AuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(system.router)
app.include_router(array.router)
app.include_router(docker.router)
app.include_router(shares.router)
app.include_router(drives.router)
app.include_router(network.router)
app.include_router(apps.router)
app.include_router(notifications.router)
app.include_router(settings.router)
app.include_router(users.router)
app.include_router(wireguard.router)
app.include_router(tailscale.router)
app.include_router(pia.router)
app.include_router(vms.router)
app.include_router(vmconsole.router)
app.include_router(terminal.router)
app.include_router(ups.router)
app.include_router(gpu.router)
app.include_router(diagnostics.router)
app.include_router(scheduler.router)
app.include_router(checkmk.router)
app.include_router(files.router)
app.include_router(mover.router)
app.include_router(hardware.router)
app.include_router(apikeys.router)
app.include_router(ddns.router)
app.include_router(ftp.router)
app.include_router(updates.router)
app.include_router(vfio.router)
app.include_router(ssl.router)
app.include_router(auth.router)
app.include_router(backup.router)
app.include_router(zfs.router)
app.include_router(vnstat.router)
app.include_router(proxy.router)
app.include_router(audit.router)
app.include_router(login.router)
app.include_router(wol.router)
app.include_router(scanner.router)
app.include_router(cpu_oc.router)
app.include_router(osupdate.router)
app.include_router(pi_oc.router)
app.include_router(mcp.router)

# Serve frontend
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONTEND):
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    if os.path.isdir("/usr/share/novnc"):          # browser VM console client (Debian package novnc)
        app.mount("/novnc", StaticFiles(directory="/usr/share/novnc"), name="novnc")

    @app.get("/", include_in_schema=False)
    def index():
        # no-cache = "revalidate every load" (cheap 304 via the ETag). Without it
        # browsers heuristically cache the page, so after an OS update they kept
        # showing the previous version's UI (e.g. a stale "Demo mode" banner).
        return FileResponse(os.path.join(FRONTEND, "index.html"),
                            headers={"Cache-Control": "no-cache"})

@app.get("/api/health")
def health():
    return {"status": "ok", "version": _helix_version()}


from fastapi.responses import JSONResponse

@app.get("/manifest.json", include_in_schema=False)
def pwa_manifest():
    return JSONResponse({
        "name": "Helix OS",
        "short_name": "Helix",
        "description": "Open-source NAS management",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#141414",
        "theme_color": "#141414",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })
