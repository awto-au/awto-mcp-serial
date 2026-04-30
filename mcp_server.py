#!/usr/bin/env python3
"""
awto-serial MCP server  —  exposes the serial daemon as MCP tools for Copilot.

Runs as a stdio MCP server (VS Code launches it automatically via mcp.json).
Connects to the serial daemon over the Unix socket; the daemon keeps the
serial port open between calls so there is no per-call startup cost.

Tools exposed to Copilot:
  serial_query(command, timeout_ms?)  — send command, return response
  serial_ping()                        — check daemon + serial port are up
"""

import logging
import logging.handlers
import socket
import sys
from pathlib import Path

# allow running from repo root without install
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import os

from protocol import DEFAULT_SOCKET_PATH as _DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, make_ok, send_request

# Allow test harness (and systemd overrides) to redirect the socket path
DEFAULT_SOCKET_PATH = os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)


def _sock_path() -> str:
    """Return socket path, honouring AWTO_SOCKET env var at call time."""
    return os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)

# ---------------------------------------------------------------------------
# Logging  (syslog via /dev/log + stderr fallback)
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_USER,
        )
        syslog.ident = "awto-mcp-server: "
        syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(syslog)
    except OSError:
        pass

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(
        logging.Formatter("awto-mcp-server[%(process)d]: %(levelname)-8s %(name)s: %(message)s")
    )
    root.addHandler(stderr)


_setup_logging()
log = logging.getLogger("mcp")

mcp = FastMCP(
    "awto-serial",
    instructions="Persistent ASCII serial interface for embedded devices.",
)

# ---------------------------------------------------------------------------
# Daemon connection helper
# ---------------------------------------------------------------------------

def _daemon_query(req: dict) -> str:
    """Open a connection to the daemon, send *req*, return the response text.

    Raises RuntimeError on daemon / serial errors so Copilot gets a clear
    error message rather than a raw exception traceback.
    """
    # Read at call time so test/override via os.environ["AWTO_SOCKET"] is honoured
    sock_path = os.environ.get("AWTO_SOCKET", _DEFAULT_SOCKET_PATH)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(sock_path)
            resp = send_request(sock, req)
    except FileNotFoundError:
        raise RuntimeError(
            f"daemon socket not found at {sock_path}. "
            "Start the daemon first:  python serial_daemon.py"
        )
    except ConnectionRefusedError:
        raise RuntimeError(
            "daemon is not running. "
            "Start it with:  python serial_daemon.py"
        )
    except OSError as exc:
        raise RuntimeError(f"socket error: {exc}") from exc

    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "unknown daemon error"))

    return resp.get("response", "")


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def serial_query(
    command: str,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    include_timestamp: bool = False,
    timestamp_format: str = "",
) -> str:
    """Send an ASCII command to the serial device and return its response.

    Args:
        command:    The ASCII command line to send (newline appended automatically).
        timeout_ms: How long to wait for the response in milliseconds (default 500).
        include_timestamp: Include timestamp in output when enabled.
        timestamp_format: Optional one-shot format: 'iso8601', '24hour', 'epoch'.

    Returns:
        The device's ASCII response, stripped of leading/trailing whitespace.
    """
    log.debug("serial_query: %r timeout=%dms", command, timeout_ms)
    req = {
        "cmd": "query",
        "line": command,
        "timeout_ms": timeout_ms,
        "include_timestamp": include_timestamp,
    }
    if timestamp_format:
        req["timestamp_format"] = timestamp_format
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, req)
    except OSError as exc:
        return f"error: {exc}"
    if not resp.get("ok"):
        return f"error: {resp.get('error', 'unknown')}"
    result = resp.get("response", "")
    ts = resp.get("timestamp")
    if ts:
        result = f"[{ts}] {result}"
    log.debug("serial_query response: %r", result[:120])
    return result


@mcp.tool()
def serial_ping() -> str:
    """Check that the serial daemon is running and the port is open.

    Returns:
        'ok' if the daemon responds, or an error message.
    """
    try:
        result = _daemon_query({"cmd": "ping"})
        log.info("ping ok: %s", result)
        return f"ok ({result})"
    except RuntimeError as exc:
        log.warning("ping failed: %s", exc)
        return f"error: {exc}"


@mcp.tool()
def serial_set_baud(baud: int) -> str:
    """Set the serial port baud rate live (e.g. 2480000).

    The change applies to all subsequent queries until changed again or
    the daemon restarts. Use this when you already know the device's baud
    rate; otherwise prefer ``serial_detect_baud``.

    Returns:
        'ok (baud=N)' on success, or an error message.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_baud", "baud": int(baud)})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (baud={resp.get('baud')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_set_eol(eol: str) -> str:
    """Set the line ending used for outgoing commands.

    Args:
        eol: One of 'lf', 'cr', 'crlf' (matches tio --map convention).
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_eol", "eol": eol})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (eol={resp.get('eol')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_detect_baud(probe: str = "?", timeout_ms: int = 200) -> str:
    """Auto-detect the device's baud rate by probing fastest-first.

    The daemon sends ``probe`` at each candidate rate (2_480_000 → 9600)
    and selects the first rate that returns valid printable ASCII.
    On success the daemon's active baud rate is updated.

    Returns:
        'detected baud=N' or an error message.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(
                sock,
                {"cmd": "detect_baud", "probe": probe, "timeout_ms": int(timeout_ms)},
            )
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"detected baud={resp.get('baud')}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_detect_eol(probe: str = "?", timeout_ms: int = 500) -> str:
    """Auto-detect the device's line ending (LF / CR / CRLF).

    On success the daemon's active EOL is updated so subsequent
    ``serial_query`` calls use the correct terminator.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(
                sock,
                {"cmd": "detect_eol", "probe": probe, "timeout_ms": int(timeout_ms)},
            )
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"detected eol={resp.get('eol')}"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_info() -> dict:
    """Return daemon state: port, baud, eol, maps, log_path, log_strip, ts_format, is_open."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "info"})
        if not resp.get("ok"):
            return {"error": resp.get("error", "unknown")}
        return resp.get("info", {})
    except OSError as exc:
        return {"error": str(exc)}


@mcp.tool()
def serial_set_map(maps: str) -> str:
    """Set character mapping (comma-separated). Valid names: INLCRNL, ICRNL, ONLCRNL, ODELBS.

    Empty string clears all maps.
    - ONLCRNL: outgoing NL → CRNL
    - ODELBS:  outgoing DEL → BS
    - ICRNL:   incoming CR → NL
    - INLCRNL: incoming NL → CRNL
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_map", "maps": maps})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (maps={resp.get('maps')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_set_timestamp(format: str = "") -> str:
    """Set timestamp format prepended to log lines: 'iso8601', '24hour', 'epoch', or empty to disable."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "set_timestamp", "format": format})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (ts_format={resp.get('ts_format')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_log_start(path: str, append: bool = True, strip: bool = False) -> str:
    """Start logging all received serial data to a file.

    The file is ALWAYS appended — it is never overwritten or deleted.
    Args:
        path: Absolute path to the log file.
        append: Accepted for compatibility; ignored (always append-only).
        strip: Strip ANSI/control chars before writing log lines.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "log_start", "path": path, "strip": strip})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return f"ok (logging to {resp.get('log_path')}, append-only, strip={resp.get('log_strip')})"
    except OSError as exc:
        return f"error: {exc}"


@mcp.tool()
def serial_log_stop() -> str:
    """Stop logging received serial data (flushes and closes the log file)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(_sock_path())
            resp = send_request(sock, {"cmd": "log_stop"})
        if not resp.get("ok"):
            return f"error: {resp.get('error', 'unknown')}"
        return resp.get("response", "ok")
    except OSError as exc:
        return f"error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
