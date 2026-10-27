#!/usr/bin/env python3
"""
Claude remote launcher — a minimal webhook listener.

On an authenticated POST /launch it starts a detached `screen` session running
`claude --remote-control <name>`, so the session registers for Remote Control and
can be driven from the Claude app on a phone.

Security model:
  * Binds to 127.0.0.1 only. It is reachable ONLY through the Cloudflare tunnel,
    never directly from the internet or the LAN.
  * Every mutating request must present the shared secret as `Authorization:
    Bearer <secret>`. The compare is constant-time.
  * It takes NO caller-controlled input that reaches a shell. The session name is
    generated server-side and the child is spawned with an argument list (no shell).

Configuration comes from the environment, or from a KEY=VALUE config file
(LAUNCHER_CONFIG, default ~/.config/claude-launcher/config.env) for values not
already set in the environment:
  LAUNCHER_SECRET   (required) shared bearer secret
  LAUNCHER_HOST     bind address            (default 127.0.0.1)
  LAUNCHER_PORT     bind port               (default 8787)
  LAUNCH_WORKDIR    cwd for the claude session (default $HOME)
  SESSION_PREFIX    session-name prefix     (default: hostname)
  CLAUDE_BIN        path to the claude binary (default: resolved from PATH)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("claude-launcher")

# Default config path, overridable with LAUNCHER_CONFIG. Under systemd the unit
# supplies the environment directly via EnvironmentFile, so this is mainly for
# running the listener by hand (plain host, Docker, a different init system).
DEFAULT_CONFIG = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "claude-launcher",
    "config.env",
)


def _load_config_file() -> None:
    """Load KEY=VALUE lines from the config file into the environment.

    Existing environment variables win, so systemd's EnvironmentFile and any
    explicit exports are never overridden.
    """
    path = os.environ.get("LAUNCHER_CONFIG", DEFAULT_CONFIG)
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _require_secret() -> str:
    secret = os.environ.get("LAUNCHER_SECRET", "").strip()
    if not secret:
        sys.exit("FATAL: LAUNCHER_SECRET is not set")
    if len(secret) < 16:
        sys.exit("FATAL: LAUNCHER_SECRET is too short (use >=32 random chars)")
    return secret


_load_config_file()

HOST = os.environ.get("LAUNCHER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LAUNCHER_PORT", "8787"))
WORKDIR = os.path.expanduser(os.environ.get("LAUNCH_WORKDIR", os.path.expanduser("~")))
PREFIX = os.environ.get("SESSION_PREFIX", socket.gethostname().split(".")[0])
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or ""
SECRET = _require_secret()

if not CLAUDE_BIN or not os.path.exists(CLAUDE_BIN):
    sys.exit("FATAL: claude binary not found; set CLAUDE_BIN to its absolute path")
SCREEN_BIN = shutil.which("screen")
if not SCREEN_BIN:
    sys.exit("FATAL: screen not found on PATH")


def _list_sessions() -> list[str]:
    """Return names of live screen sessions started by this launcher."""
    try:
        out = subprocess.run(
            [SCREEN_BIN, "-ls"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception as e:  # noqa: BLE001
        log.warning("screen -ls failed: %s", e)
        return []
    names = []
    for line in out.splitlines():
        line = line.strip()
        # lines look like: "12345.sessionname\t(Detached)"
        if "." in line and ("Detached" in line or "Attached" in line):
            tag = line.split()[0]  # pid.name
            name = tag.split(".", 1)[1]
            if name.startswith(PREFIX):
                names.append(name)
    return names


def _launch() -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{PREFIX}-{stamp}"
    cmd = [SCREEN_BIN, "-dmS", name, CLAUDE_BIN, "--remote-control", name]
    log.info("launching session %s in %s", name, WORKDIR)
    subprocess.run(cmd, cwd=WORKDIR, check=True, timeout=30, env=os.environ.copy())
    return {"status": "launched", "session": name, "workdir": WORKDIR}


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-launcher"
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), SECRET)

    def log_message(self, fmt, *args):  # route through logging, drop client noise
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if self.path == "/sessions":
            if not self._authed():
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, {"sessions": _list_sessions()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/launch":
            self._send(404, {"error": "not found"})
            return
        if not self._authed():
            log.warning("rejected unauthenticated /launch from %s", self.address_string())
            self._send(401, {"error": "unauthorized"})
            return
        # Drain any request body so the connection can be reused/closed cleanly.
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        try:
            result = _launch()
            self._send(200, result)
        except subprocess.CalledProcessError as e:
            log.error("launch failed: %s", e)
            self._send(500, {"error": "launch failed", "detail": str(e)})
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected error")
            self._send(500, {"error": "internal", "detail": str(e)})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info(
        "starting on %s:%d  prefix=%s workdir=%s claude=%s",
        HOST, PORT, PREFIX, WORKDIR, CLAUDE_BIN,
    )
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
