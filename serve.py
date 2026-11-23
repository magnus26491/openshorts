"""Start the API: one process, or an engine plus N front processes.

The image runs this (Dockerfile CMD). API_FRONT_WORKERS=0 (the default) runs
the API as it always ran: one uvicorn process on port 8000. With N > 0
(docs/architecture/multiprocess-api.md):

* the engine (app.py, API_ROLE=engine) listens on ENGINE_SOCKET, a local
  socket: the queue, the jobs, the handover, everything stateful;
* N front processes (front.py, uvicorn --workers N) listen on port 8000,
  serve what needs no engine memory and pass the rest to the engine.

Stopping keeps the deploy handover as it was: SIGTERM goes to the engine
only, which drains (finishes its jobs, reports not-ready so the proxy stops
routing here) while the fronts keep carrying its requests; once the engine
has exited the fronts are stopped. If either side dies on its own, the other
is stopped too and the container exits, so the platform restarts it whole.
"""
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The bgutil PO-token provider has two modes (yt_clients.py:pot_provider_args):
# a cold-start script (BGUTIL_SCRIPT_PATH, generate_once.js — a fresh Node
# process, and a fresh unauthenticated bgutils session, per token) or this
# persistent HTTP server (BGUTIL_BASE_URL), which keeps one warmed session
# across every request (server/src/session_manager.ts upstream). Losing that
# session every call is a plausible contributor to how often the HD attempt
# falls through to "Sign in to confirm you're not a bot" once account cookies
# have rotated — the two problems compound: stale cookies *and* a token minted
# from a cold session each raise the odds of landing on the bot wall. This
# starts the persistent server once, locally, in the same container, so
# BGUTIL_SCRIPT_PATH stays installed purely as the fallback if the server
# doesn't come up (23-nov-2026).
BGUTIL_SERVER_SCRIPT = "/opt/bgutil-provider/server/build/main.js"
BGUTIL_READY_TIMEOUT_SECONDS = 6.0

PORT = os.environ.get("PORT", "8000")
UVICORN_COMMON = ["--proxy-headers", "--forwarded-allow-ips", "*",
                  "--timeout-graceful-shutdown", "15"]
FRONT_STOP_SECONDS = 20
# uvicorn's supervisor kills a worker that does not answer its ping within
# this many seconds and starts another one, dropping that worker's requests.
# Its default (5 s) killed healthy fronts on a host whose CPU other services
# had saturated (load ~200 on 20 cores, 18-nov-2026).
FRONT_HEALTHCHECK_SECONDS = os.environ.get("FRONT_HEALTHCHECK_SECONDS", "30")


def _workers() -> int:
    try:
        return max(0, int(os.environ.get("API_FRONT_WORKERS", "0") or 0))
    except ValueError:
        return 0


def _start_bgutil_sidecar():
    """
    Launch the bgutil PO-token HTTP server as a background process bound to
    127.0.0.1 (its own default — never exposed outside the container), then
    set BGUTIL_BASE_URL so yt_clients.py prefers it over the script fallback.

    Never raises and never blocks startup for long: if BGUTIL_BASE_URL is
    already set (an operator pointed it at an external provider), if the
    server script isn't present, if `node` isn't on PATH, or if it doesn't
    answer /ping within BGUTIL_READY_TIMEOUT_SECONDS, this just logs one line
    and leaves the environment exactly as it was — BGUTIL_SCRIPT_PATH (set in
    the Dockerfile) remains, so the cold-start fallback still works precisely
    as it did before this existed. This is additive, not a replacement.
    """
    if os.environ.get("BGUTIL_BASE_URL"):
        return
    if not os.path.isfile(BGUTIL_SERVER_SCRIPT):
        return

    port = os.environ.get("BGUTIL_SIDECAR_PORT", "4416")
    try:
        subprocess.Popen(["node", BGUTIL_SERVER_SCRIPT, "--port", port],
                          stdout=sys.stdout, stderr=sys.stderr)
    except OSError as e:
        print(f"🔑 bgutil sidecar did not start ({e}); using script-mode PO tokens", flush=True)
        return

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BGUTIL_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/ping", timeout=1.0) as resp:
                if resp.status == 200:
                    os.environ["BGUTIL_BASE_URL"] = base_url
                    print(f"🔑 bgutil PO-token server ready on {base_url}", flush=True)
                    return
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.3)
    print("🔑 bgutil sidecar did not answer /ping in time; using script-mode PO tokens", flush=True)


def _single():
    os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "app:app",
                               "--host", "0.0.0.0", "--port", PORT, *UVICORN_COMMON])


def main():
    _start_bgutil_sidecar()
    workers = _workers()
    if workers == 0:
        _single()
        return
    socket_path = os.environ.setdefault("ENGINE_SOCKET", "/tmp/openshorts-engine.sock")
    # Every process clears its user caches when another one forgets someone
    # (cloud/owner_cache.py): a file on the disk both containers share.
    os.environ.setdefault("CACHE_EPOCH_FILE", os.path.abspath(os.path.join("output", ".cache_epoch")))
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    engine = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--uds", socket_path, *UVICORN_COMMON],
        env={**os.environ, "API_ROLE": "engine"})
    fronts = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "front:app", "--host", "0.0.0.0", "--port", PORT,
         "--workers", str(workers), "--timeout-worker-healthcheck", FRONT_HEALTHCHECK_SECONDS,
         *UVICORN_COMMON],
        env={**os.environ, "API_ROLE": "front"})
    print(f"🚦 API split: engine pid {engine.pid} on {socket_path}, "
          f"{workers} front processes (pid {fronts.pid}) on :{PORT}", flush=True)

    stopping = {"at": None}

    def _stop(signum, _frame):
        if stopping["at"] is None:
            stopping["at"] = time.monotonic()
            print(f"🚦 Signal {signum}: the engine drains, the fronts keep serving it", flush=True)
            _signal(engine, signal.SIGTERM)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    code = 0
    while True:
        engine_code, fronts_code = engine.poll(), fronts.poll()
        if engine_code is not None:
            code = engine_code if stopping["at"] is not None else (engine_code or 1)
            if stopping["at"] is None:
                print(f"⚠️ The engine exited on its own ({engine_code}): stopping the fronts",
                      flush=True)
            _stop_fronts(fronts)
            break
        if fronts_code is not None:
            print(f"⚠️ The front processes exited ({fronts_code}): stopping the engine",
                  flush=True)
            _stop(signal.SIGTERM, None)
            engine.wait()
            code = fronts_code or 1
            break
        time.sleep(0.5)
    sys.exit(code)


def _signal(proc, signum):
    try:
        proc.send_signal(signum)
    except ProcessLookupError:
        pass


def _stop_fronts(fronts):
    if fronts.poll() is not None:
        return
    _signal(fronts, signal.SIGTERM)
    try:
        fronts.wait(timeout=FRONT_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        fronts.kill()
        fronts.wait()


if __name__ == "__main__":
    main()
