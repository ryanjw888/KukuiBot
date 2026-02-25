"""Claude Bridge Pool Manager — manages per-worker Claude Code bridge subprocesses."""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse

from config import KUKUIBOT_HOME

logger = logging.getLogger("kukuibot.bridge")

router = APIRouter()

# =============================================
# CLAUDE BRIDGE POOL MANAGER
# =============================================

CLAUDE_BRIDGE_BASE_PORT = int(os.environ.get("KUKUIBOT_CLAUDE_BRIDGE_PORT", 9085))
_BRIDGE_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "bridge"
_BRIDGE_SCRIPT = _BRIDGE_DIR / "claude-code-bridge.py"
_BRIDGE_WORKERS_DIR = Path(os.environ.get("KUKUIBOT_HOME", os.path.expanduser("~/.kukuibot"))) / "workers"

_BRIDGE_WORKER_PORTS: dict[str, int] = {}
_BRIDGE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_BRIDGE_CLIENTS: dict[str, httpx.AsyncClient] = {}
_BRIDGE_SPAWN_LOCKS: dict[str, asyncio.Lock] = {}
_BRIDGE_POOL_LOCK = asyncio.Lock()


def _discover_workers() -> list[str]:
    workers = []
    if _BRIDGE_WORKERS_DIR.is_dir():
        for f in sorted(_BRIDGE_WORKERS_DIR.glob("*.md")):
            workers.append(f.stem)
    if not workers:
        workers = ["developer"]
    return workers


def _init_worker_ports():
    global _BRIDGE_WORKER_PORTS
    workers = _discover_workers()
    _BRIDGE_WORKER_PORTS = {}
    for i, w in enumerate(workers):
        _BRIDGE_WORKER_PORTS[w] = CLAUDE_BRIDGE_BASE_PORT + i
    logger.info(f"Claude bridge worker ports: {_BRIDGE_WORKER_PORTS}")


def _worker_port(worker: str) -> int:
    if worker in _BRIDGE_WORKER_PORTS:
        return _BRIDGE_WORKER_PORTS[worker]
    used = set(_BRIDGE_WORKER_PORTS.values())
    port = CLAUDE_BRIDGE_BASE_PORT
    while port in used:
        port += 1
    _BRIDGE_WORKER_PORTS[worker] = port
    return port


async def _spawn_bridge(worker: str) -> bool:
    port = _worker_port(worker)
    if worker in _BRIDGE_PROCESSES:
        proc = _BRIDGE_PROCESSES[worker]
        if proc.returncode is None:
            return True

    logger.info(f"Spawning Claude bridge: worker={worker} port={port}")
    try:
        env = dict(os.environ)
        env["KUKUIBOT_HOME"] = str(Path(os.environ.get("KUKUIBOT_HOME", os.path.expanduser("~/.kukuibot"))))

        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(_BRIDGE_SCRIPT),
            "--port", str(port),
            "--worker", worker,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _BRIDGE_PROCESSES[worker] = proc

        for attempt in range(30):
            await asyncio.sleep(0.5)
            if proc.returncode is not None:
                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.error(f"Bridge {worker} exited immediately (rc={proc.returncode}): {stderr.decode()[:500]}")
                del _BRIDGE_PROCESSES[worker]
                return False
            try:
                client = await _get_bridge_client(worker)
                resp = await client.get("/health", timeout=3.0)
                if resp.status_code == 200:
                    logger.info(f"Bridge {worker} healthy on port {port} (attempt {attempt+1})")
                    return True
            except Exception:
                pass

        logger.error(f"Bridge {worker} failed to become healthy after 15s")
        return False
    except Exception as e:
        logger.error(f"Failed to spawn bridge {worker}: {e}", exc_info=True)
        return False


async def _check_bridge_external(worker: str) -> bool:
    try:
        client = await _get_bridge_client(worker)
        resp = await client.get("/health", timeout=3.0)
        if resp.status_code == 200:
            logger.info(f"Bridge {worker} already running externally on port {_worker_port(worker)}")
            return True
    except Exception:
        pass
    return False


async def _ensure_bridge(worker: str) -> bool:
    if worker in _BRIDGE_PROCESSES:
        proc = _BRIDGE_PROCESSES[worker]
        if proc.returncode is None:
            return True

    if await _check_bridge_external(worker):
        return True

    if worker not in _BRIDGE_SPAWN_LOCKS:
        _BRIDGE_SPAWN_LOCKS[worker] = asyncio.Lock()

    async with _BRIDGE_SPAWN_LOCKS[worker]:
        if worker in _BRIDGE_PROCESSES:
            proc = _BRIDGE_PROCESSES[worker]
            if proc.returncode is None:
                return True
        if await _check_bridge_external(worker):
            return True
        return await _spawn_bridge(worker)


async def _get_bridge_client(worker: str) -> httpx.AsyncClient:
    if worker in _BRIDGE_CLIENTS:
        client = _BRIDGE_CLIENTS[worker]
        if not client.is_closed:
            return client

    port = _worker_port(worker)
    client = httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{port}",
        verify=False,
        timeout=httpx.Timeout(connect=5.0, read=1800.0, write=30.0, pool=10.0),
    )
    _BRIDGE_CLIENTS[worker] = client
    return client


async def _stop_bridge(worker: str):
    proc = _BRIDGE_PROCESSES.pop(worker, None)
    if proc and proc.returncode is None:
        logger.info(f"Stopping bridge: worker={worker}")
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
    client = _BRIDGE_CLIENTS.pop(worker, None)
    if client:
        await client.aclose()


def _active_bridges() -> list[dict]:
    result = []
    for worker, proc in _BRIDGE_PROCESSES.items():
        result.append({
            "worker": worker, "port": _worker_port(worker),
            "pid": proc.pid, "running": proc.returncode is None,
        })
    return result


def _extract_worker(request: Request, body: bytes | None = None) -> str:
    worker = request.query_params.get("worker", "").strip()
    if worker:
        return worker
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("worker"):
                return str(data["worker"]).strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return "developer"


async def _proxy_to_bridge(request: Request, bridge_path: str):
    """Proxy a request to the correct worker's Claude bridge."""
    method = request.method.upper()
    body = None
    if method in ("POST", "PUT", "PATCH"):
        body = await request.body()

    worker = _extract_worker(request, body)

    if not await _ensure_bridge(worker):
        return JSONResponse({"error": f"Claude bridge for worker '{worker}' failed to start"}, status_code=502)

    client = await _get_bridge_client(worker)

    query_params = dict(request.query_params)
    query_params.pop("worker", None)
    query = "&".join(f"{k}={v}" for k, v in query_params.items())
    target = f"{bridge_path}?{query}" if query else bridge_path

    headers = dict(request.headers)
    for h in ("host", "connection", "transfer-encoding"):
        headers.pop(h, None)

    try:
        req = client.build_request(method, target, headers=headers, content=body)
        bridge_resp = await client.send(req, stream=True)

        async def stream_body():
            try:
                async for chunk in bridge_resp.aiter_bytes():
                    yield chunk
            finally:
                await bridge_resp.aclose()

        resp_headers = dict(bridge_resp.headers)
        for h in ("content-encoding", "transfer-encoding", "content-length"):
            resp_headers.pop(h, None)

        return StreamingResponse(
            stream_body(),
            status_code=bridge_resp.status_code,
            headers=resp_headers,
        )
    except httpx.ConnectError:
        return JSONResponse({"error": f"Claude bridge for worker '{worker}' is not running"}, status_code=502)
    except httpx.ReadTimeout:
        return JSONResponse({"error": f"Claude bridge for worker '{worker}' timed out"}, status_code=504)
    except Exception as e:
        logger.error(f"Claude bridge proxy error (worker={worker}): {e}", exc_info=True)
        return JSONResponse({"error": f"Bridge proxy error: {str(e)}"}, status_code=502)


async def shutdown_bridges():
    """Cleanly stop all bridge processes and close httpx clients."""
    for worker in list(_BRIDGE_PROCESSES.keys()):
        await _stop_bridge(worker)
    # Close any remaining bridge httpx clients
    for worker in list(_BRIDGE_CLIENTS.keys()):
        client = _BRIDGE_CLIENTS.pop(worker, None)
        if client and not client.is_closed:
            await client.aclose()


# --- Route handlers ---

@router.api_route("/claude/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def claude_bridge_proxy(path: str, request: Request):
    """Proxy /claude/api/* to the worker-specific Claude bridge."""
    return await _proxy_to_bridge(request, f"/{path}")


@router.get("/claude/api-pool/status")
async def claude_pool_status():
    """Return status of all active bridge workers."""
    bridges = _active_bridges()
    workers = _discover_workers()
    return JSONResponse({
        "active_bridges": bridges,
        "available_workers": workers,
        "worker_ports": _BRIDGE_WORKER_PORTS,
    })


@router.get("/claude/")
async def serve_claude_page():
    page = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "claude-chat.html")
    if os.path.isfile(page):
        return FileResponse(page)
    return HTMLResponse("<h1>Claude Chat</h1><p>claude-chat.html not found.</p>", status_code=200)


@router.get("/claude")
async def redirect_claude():
    return RedirectResponse("/claude/", status_code=302)


# --- Root CA download (for trusting HTTPS on new devices) ---
ROOT_CA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "certs", "rootCA.pem")

@router.get("/api/cert")
async def download_root_ca():
    """Download the mkcert root CA so devices can trust KukuiBot's HTTPS."""
    if not os.path.isfile(ROOT_CA_PATH):
        return {"error": "Root CA not found"}
    return FileResponse(
        ROOT_CA_PATH,
        media_type="application/x-pem-file",
        filename="KukuiBot-RootCA.pem",
    )
