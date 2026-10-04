"""The stdout handshake contract with the Rust shell.

This spawns the real process. If it breaks, the desktop app cannot find or
authenticate to the core, so it is worth the few seconds it costs.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import httpx
import pytest


@pytest.fixture
def core(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "JARVIS_DATA_DIR": str(tmp_path / "data"),
        "JARVIS_CONFIG_DIR": str(tmp_path / "config"),
    }
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "jarvis", "--db", str(tmp_path / "t.db"), "--log-level", "ERROR"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        assert line, f"core produced no handshake; stderr={proc.stderr.read()[:400]}"  # type: ignore[union-attr]
        yield proc, json.loads(line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_handshake_shape(core) -> None:
    _proc, hs = core
    assert hs["jarvis"] == "ready"
    assert isinstance(hs["port"], int) and 1024 < hs["port"] < 65536
    assert isinstance(hs["token"], str) and len(hs["token"]) >= 32
    assert hs["pid"] > 0
    assert hs["version"]


def test_handshake_is_exactly_one_json_line(core) -> None:
    """Logs go to stderr so stdout stays parseable by the shell."""
    _proc, hs = core
    assert set(hs) == {"jarvis", "port", "token", "pid", "version"}


def test_the_reported_port_actually_serves(core) -> None:
    _proc, hs = core
    base = f"http://127.0.0.1:{hs['port']}"
    deadline = time.time() + 15
    last: Exception | None = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/health", timeout=2)
            if r.status_code == 200:
                assert r.json()["status"] == "ok"
                break
        except httpx.HTTPError as exc:  # server still starting
            last = exc
            time.sleep(0.2)
    else:
        pytest.fail(f"core never served on the port it reported: {last}")


def test_the_reported_token_authenticates(core) -> None:
    _proc, hs = core
    base = f"http://127.0.0.1:{hs['port']}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get(f"{base}/health", timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(0.2)

    assert httpx.get(f"{base}/providers", timeout=5).status_code == 401
    ok = httpx.get(
        f"{base}/providers",
        headers={"authorization": f"Bearer {hs['token']}"},
        timeout=5,
    )
    assert ok.status_code == 200


def test_binds_loopback_only(core) -> None:
    """A routable bind would hand computer control to the local network."""
    import socket

    _proc, hs = core
    s = socket.socket()
    s.settimeout(2)
    # Connecting from a non-loopback local address must fail.
    try:
        hostname_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        pytest.skip("no routable address on this host")
    if hostname_ip.startswith("127."):
        pytest.skip("host only has loopback addresses")
    with pytest.raises((ConnectionRefusedError, TimeoutError, OSError)):
        s.connect((hostname_ip, hs["port"]))
    s.close()
