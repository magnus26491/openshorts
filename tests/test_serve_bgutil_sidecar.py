"""serve.py's bgutil PO-token sidecar: must never block or crash startup,
must defer to an already-set BGUTIL_BASE_URL or a missing server script, and
must actually detect a ready server and flip BGUTIL_BASE_URL over to it.
"""
import http.server
import os
import threading

import pytest

import serve


class _PingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # quiet during tests
        pass


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("BGUTIL_BASE_URL", raising=False)
    monkeypatch.delenv("BGUTIL_SIDECAR_PORT", raising=False)


def test_noop_when_base_url_already_set(clean_env, monkeypatch):
    monkeypatch.setenv("BGUTIL_BASE_URL", "http://somewhere-else:9999")
    monkeypatch.setattr(serve, "BGUTIL_SERVER_SCRIPT", "/does/not/matter")
    popen_calls = []
    monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))

    serve._start_bgutil_sidecar()

    assert popen_calls == []
    assert os.environ["BGUTIL_BASE_URL"] == "http://somewhere-else:9999"


def test_noop_when_server_script_missing(clean_env, monkeypatch, tmp_path):
    missing = tmp_path / "no-such-main.js"
    monkeypatch.setattr(serve, "BGUTIL_SERVER_SCRIPT", str(missing))
    popen_calls = []
    monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: popen_calls.append((a, k)))

    serve._start_bgutil_sidecar()

    assert popen_calls == []
    assert "BGUTIL_BASE_URL" not in os.environ


def test_sets_base_url_once_server_answers_ping(clean_env, monkeypatch, tmp_path):
    fake_script = tmp_path / "main.js"
    fake_script.write_text("// stand-in for the real bgutil server entry point")
    monkeypatch.setattr(serve, "BGUTIL_SERVER_SCRIPT", str(fake_script))

    server = http.server.HTTPServer(("127.0.0.1", 0), _PingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("BGUTIL_SIDECAR_PORT", str(port))
        # The fixture's Popen is never meant to actually start anything here —
        # the fake HTTP server above is already standing in for it.
        monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: None)

        serve._start_bgutil_sidecar()

        assert os.environ["BGUTIL_BASE_URL"] == f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        # _start_bgutil_sidecar sets this on the real os.environ as a direct
        # side effect (that's the point of the function) — monkeypatch only
        # auto-reverts changes made through its own setenv/delenv, so this
        # has to be cleaned up by hand or it leaks into every later test in
        # the same pytest process (ssrf_guard.allowed_hosts() reads this env
        # var, so a leaked 127.0.0.1 here silently defeats its tests).
        os.environ.pop("BGUTIL_BASE_URL", None)


def test_gives_up_after_timeout_when_nothing_listens(clean_env, monkeypatch, tmp_path):
    fake_script = tmp_path / "main.js"
    fake_script.write_text("// stand-in; nothing will ever listen on the port below")
    monkeypatch.setattr(serve, "BGUTIL_SERVER_SCRIPT", str(fake_script))
    monkeypatch.setattr(serve, "BGUTIL_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("BGUTIL_SIDECAR_PORT", "1")  # privileged/unused port: nothing answers
    monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: None)

    serve._start_bgutil_sidecar()

    assert "BGUTIL_BASE_URL" not in os.environ


def test_popen_failure_is_swallowed(clean_env, monkeypatch, tmp_path):
    fake_script = tmp_path / "main.js"
    fake_script.write_text("// stand-in")
    monkeypatch.setattr(serve, "BGUTIL_SERVER_SCRIPT", str(fake_script))

    def _raise(*a, **k):
        raise OSError("node: command not found")

    monkeypatch.setattr(serve.subprocess, "Popen", _raise)

    serve._start_bgutil_sidecar()  # must not raise

    assert "BGUTIL_BASE_URL" not in os.environ
