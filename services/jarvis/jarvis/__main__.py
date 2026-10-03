"""Entrypoint for the Jarvis core sidecar.

Startup handshake: the core binds an ephemeral loopback port, mints a one-time
bearer token, and prints a single JSON line to **stdout**:

    {"jarvis":"ready","port":54321,"token":"...","pid":1234,"version":"0.2.0"}

The Rust shell reads that line and uses it for every subsequent call. The token
is never written to disk, so it cannot be recovered after the process exits.
All logging goes to stderr, keeping stdout clean for the handshake.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys

import uvicorn

from jarvis.app import VERSION, build_context, create_app
from jarvis.config import paths
from jarvis.config import settings as settings_module
from jarvis.transport.auth import mint_token
from jarvis.util.logging import configure, get_logger


def _reserve_port(host: str, port: int) -> tuple[socket.socket, int]:
    """Bind now so the port in the handshake is the port actually served.

    Asking the OS for a port and then reporting it separately would race: the
    shell could read a port that another process had since taken.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock, sock.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis-core", description="Jarvis core sidecar")
    parser.add_argument("--host", default=None, help="loopback address to bind")
    parser.add_argument("--port", type=int, default=None, help="port, or 0 for ephemeral")
    parser.add_argument("--token", default=None, help="override the generated API token")
    parser.add_argument("--db", default=None, help="database path (':memory:' for ephemeral)")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--print-token", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    settings_module.write_default_config()
    cfg = settings_module.load()

    if args.host:
        cfg.server.host = args.host
    if args.port is not None:
        cfg.server.port = args.port
    if args.log_level:
        cfg.logging.level = args.log_level.upper()

    configure(
        level=cfg.logging.level, to_file=cfg.logging.to_file, json_console=cfg.logging.json_console
    )
    log = get_logger("jarvis.main")

    token = args.token or mint_token()
    sock, port = _reserve_port(cfg.server.host, cfg.server.port)

    ctx = build_context(cfg, token, args.db)
    app = create_app(ctx)

    handshake = {
        "jarvis": "ready",
        "port": port,
        "token": token,
        "pid": __import__("os").getpid(),
        "version": VERSION,
    }
    # The one thing stdout is for.
    sys.stdout.write(json.dumps(handshake) + "\n")
    sys.stdout.flush()

    log.info("listening", host=cfg.server.host, port=port, data_dir=str(paths.data_dir()))

    config = uvicorn.Config(
        app,
        log_config=None,  # structlog owns logging
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
