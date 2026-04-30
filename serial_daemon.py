#!/usr/bin/env python3
"""
awto-serial-daemon  —  owns the serial port, multiplexes it over a Unix socket.

Usage:
    python serial_daemon.py [--port /dev/ttyACM0] [--baud 2000000]
                             [--socket /tmp/awto-serial.sock]

Clients connect to the Unix socket and exchange JSON-lines (see protocol.py).
The daemon serialises all serial access through a threading.Lock so multiple
clients (CLI, MCP server, test scripts) can coexist safely.

Requires the free-threaded (no-GIL) CPython build: python3.13t
"""

import argparse
import datetime
import json
import logging
import logging.handlers
import os
import re
import socket
import sys
import threading
import time
import serial

from protocol import (
    CANDIDATE_BAUDS,
    DEFAULT_BAUD,
    DEFAULT_EOL,
    DEFAULT_PORT,
    DEFAULT_SOCKET_PATH,
    EOL_BYTES,
    make_err,
    make_ok,
)

log = logging.getLogger("daemon")

_VALID_MAPS: frozenset[str] = frozenset({"INLCRNL", "ICRNL", "ONLCRNL", "ODELBS"})
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(ident: str, level_name: str) -> None:
    """Configure syslog + stderr logging.

    Syslog entries appear in journald / /var/log/syslog as:
        awto-serial-daemon[PID]: LEVEL daemon: message
    """
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # --- syslog handler (journald / /dev/log) ---
    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_DAEMON,
        )
        syslog.ident = f"{ident}: "          # prepended to every message
        # Use syslog priority mapping so journald assigns correct severity
        syslog.mapPriority = logging.handlers.SysLogHandler.mapPriority  # type: ignore[method-assign]
        syslog_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        syslog.setFormatter(syslog_fmt)
        root.addHandler(syslog)
    except OSError:
        pass  # /dev/log absent (e.g. minimal container) — fall through to stderr only

    # --- stderr handler (interactive / systemd ExecStart journal fallback) ---
    stderr = logging.StreamHandler(sys.stderr)
    stderr_fmt = logging.Formatter(
        f"{ident}[%(process)d]: %(levelname)-8s %(name)s: %(message)s"
    )
    stderr.setFormatter(stderr_fmt)
    root.addHandler(stderr)


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------

class SerialWorker:
    """Owns the serial port and exposes a thread-safe query() method."""

    def __init__(self, port: str, baud: int, eol: str = DEFAULT_EOL) -> None:
        self._port = port
        self._baud = baud
        self._eol = eol
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self._maps: frozenset[str] = frozenset()
        self._log_path: str | None = None
        self._log_file = None
        self._log_lock = threading.Lock()
        self._log_strip = False
        self._ts_format: str | None = None

    # ------------------------------------------------------------------
    @property
    def baud(self) -> int:
        return self._baud

    @property
    def eol(self) -> str:
        return self._eol

    @property
    def port(self) -> str:
        return self._port

    # ------------------------------------------------------------------
    def open(self) -> None:
        self._ser = serial.Serial(
            self._port,
            baudrate=self._baud,
            timeout=0.01,       # non-blocking short reads
            write_timeout=0.2,
        )
        log.info("serial open: %s @ %d (eol=%s)", self._port, self._baud, self._eol)

    def close(self) -> None:
        self.log_stop()
        if self._ser and self._ser.is_open:
            self._ser.close()
        self._ser = None

    # ------------------------------------------------------------------
    def set_baud(self, baud: int) -> None:
        """Change baud rate live. Raises SerialException if driver rejects it."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            # pyserial setter does the platform-specific reconfigure
            self._ser.baudrate = baud
            self._baud = baud
            log.info("baud changed: %d", baud)

    def set_eol(self, eol: str) -> None:
        if eol not in EOL_BYTES:
            raise ValueError(f"eol must be one of {list(EOL_BYTES)}, got {eol!r}")
        with self._lock:
            self._eol = eol
            log.info("eol changed: %s", eol)

    def info(self) -> dict:
        is_open = bool(self._ser and self._ser.is_open)
        return {
            "port": self._port,
            "baud": self._baud,
            "eol": self._eol,
            "is_open": is_open,
            "maps": sorted(self._maps),
            "log_path": self._log_path,
            "log_strip": self._log_strip,
            "ts_format": self._ts_format,
        }

    # ------------------------------------------------------------------
    def query(self, line: str, timeout_ms: int) -> str:
        """Send *line* terminated by current EOL and collect the response.

        Returns as soon as a newline (\\n or \\r) is seen in the response,
        or when the deadline expires — whichever comes first.
        """
        with self._lock:
            return self._query_locked(line, timeout_ms)

    def query_with_timestamp(self, line: str, timeout_ms: int, ts_format: str | None) -> dict:
        """Send query and optionally include a timestamp in the response payload."""
        with self._lock:
            response = self._query_locked(line, timeout_ms)
            fmt = self._normalize_ts_format(ts_format) if ts_format is not None else self._ts_format
            ts = self._format_ts_for(fmt)
            out = {"response": response}
            if ts:
                out["timestamp"] = ts
            return out

    def _query_locked(self, line: str, timeout_ms: int) -> str:
        if self._ser is None or not self._ser.is_open:
            raise IOError("serial port not open")

        terminator = EOL_BYTES[self._eol]
        payload = self._apply_output_map(line.encode() + terminator)
        self._ser.reset_input_buffer()
        self._ser.write(payload)
        self._ser.flush()

        deadline = time.monotonic() + timeout_ms / 1000.0
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf.extend(chunk)
                # stop as soon as we have any complete line (CR or LF)
                if b"\n" in chunk or b"\r" in chunk:
                    break

        result = self._apply_input_map(bytes(buf)).decode(errors="replace").strip()
        self._log_line(result)
        return result

    # ------------------------------------------------------------------
    def detect_baud(
        self,
        probe: str = "?",
        timeout_ms: int = 200,
        candidates: tuple[int, ...] | None = None,
    ) -> int:
        """Probe candidate baud rates fastest-first; return the one that yields valid ASCII.

        Scoring: response must contain >=4 bytes and >=80 % printable ASCII.
        """
        rates = candidates or CANDIDATE_BAUDS
        with self._lock:
            if self._ser is None:
                raise IOError("serial port not open")
            original = self._baud
            best_baud = None
            best_score = 0.0
            for rate in rates:
                try:
                    self._ser.baudrate = rate
                except (serial.SerialException, OSError) as exc:
                    log.debug("baud %d not supported by driver: %s", rate, exc)
                    continue
                self._baud = rate
                try:
                    resp = self._query_locked(probe, timeout_ms)
                except IOError:
                    continue
                score = _ascii_score(resp)
                log.debug("probe %d → %r (score=%.2f)", rate, resp[:40], score)
                if score >= 0.8 and len(resp) >= 4:
                    log.info("detect_baud: %d (score=%.2f, resp=%r)", rate, score, resp[:40])
                    return rate
                if score > best_score:
                    best_score = score
                    best_baud = rate
            # Nothing matched cleanly — restore original and fail
            self._ser.baudrate = original
            self._baud = original
            raise IOError(
                f"baud detect failed (best={best_baud} score={best_score:.2f}); "
                "device may be silent or use binary protocol"
            )

    def detect_eol(self, probe: str = "?", timeout_ms: int = 500) -> str:
        """Send a probe and infer line ending from response. Sets self._eol on success."""
        with self._lock:
            if self._ser is None or not self._ser.is_open:
                raise IOError("serial port not open")
            # Send with bare LF so we don't bias the result
            self._ser.reset_input_buffer()
            self._ser.write(probe.encode() + b"\n")
            self._ser.flush()

            deadline = time.monotonic() + timeout_ms / 1000.0
            buf = bytearray()
            while time.monotonic() < deadline:
                chunk = self._ser.read(4096)
                if chunk:
                    buf.extend(chunk)
                    # wait long enough to get a definitive terminator pair
                    if b"\r\n" in buf or buf.count(b"\n") >= 2 or buf.count(b"\r") >= 2:
                        break

            data = bytes(buf)
            if not data:
                raise IOError("detect_eol: no response from device")
            if b"\r\n" in data:
                detected = "crlf"
            elif b"\n" in data and b"\r" not in data:
                detected = "lf"
            elif b"\r" in data and b"\n" not in data:
                detected = "cr"
            else:
                # mixed / ambiguous — prefer crlf as it's the safe superset
                detected = "crlf"
            self._eol = detected
            log.info("detect_eol: %s (sample=%r)", detected, data[:40])
            return detected

    # ------------------------------------------------------------------
    def set_map(self, maps_str: str) -> frozenset[str]:
        """Set character mapping. maps_str is comma-separated names, or empty to clear."""
        if not maps_str.strip():
            with self._lock:
                self._maps = frozenset()
            return self._maps
        names = frozenset(m.strip().upper() for m in maps_str.split(",") if m.strip())
        invalid = names - _VALID_MAPS
        if invalid:
            raise ValueError(f"unknown maps: {sorted(invalid)}; valid: {sorted(_VALID_MAPS)}")
        with self._lock:
            self._maps = names
        log.info("maps set: %s", sorted(names))
        return names

    def set_timestamp(self, fmt: str | None) -> None:
        """Set timestamp format: 'iso8601', '24hour', 'epoch', or None/empty to disable."""
        fmt = self._normalize_ts_format(fmt)
        with self._lock:
            self._ts_format = fmt
        log.info("timestamp format: %s", self._ts_format)

    def _normalize_ts_format(self, fmt: str | None) -> str | None:
        if fmt in (None, ""):
            return None
        if fmt not in ("iso8601", "24hour", "epoch"):
            raise ValueError("timestamp format must be iso8601, 24hour, epoch or empty")
        return fmt

    def log_start(self, path: str) -> None:
        """Open log file in append mode. The file is never overwritten or deleted."""
        with self._log_lock:
            if self._log_file is not None:
                self._log_file.flush()
                self._log_file.close()
            self._log_path = path
            self._log_file = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115
            log.info("log started: %s", path)

    def set_log_strip(self, enabled: bool) -> None:
        """Enable or disable ANSI/control-character stripping for log writes."""
        with self._log_lock:
            self._log_strip = enabled
        log.info("log strip: %s", enabled)

    def log_stop(self) -> None:
        """Flush and close the log file."""
        with self._log_lock:
            if self._log_file is not None:
                self._log_file.flush()
                self._log_file.close()
                self._log_file = None
                log.info("log stopped: %s", self._log_path)

    def _apply_output_map(self, data: bytes) -> bytes:
        maps = self._maps
        if not maps:
            return data
        if "ONLCRNL" in maps:
            data = data.replace(b"\n", b"\r\n")
        if "ODELBS" in maps:
            data = data.replace(b"\x7f", b"\x08")
        return data

    def _apply_input_map(self, data: bytes) -> bytes:
        maps = self._maps
        if not maps:
            return data
        if "ICRNL" in maps:
            data = data.replace(b"\r", b"\n")
        if "INLCRNL" in maps:
            data = data.replace(b"\n", b"\r\n")
        return data

    def _format_ts(self) -> str:
        return self._format_ts_for(self._ts_format)

    def _format_ts_for(self, fmt: str | None) -> str:
        if not fmt:
            return ""
        now = datetime.datetime.now()
        if fmt == "epoch":
            return f"{now.timestamp():.3f}"
        elif fmt == "iso8601":
            return now.isoformat(timespec="milliseconds")
        else:  # 24hour
            return now.strftime("%H:%M:%S.%f")[:12]

    def _strip_for_log(self, text: str) -> str:
        """Drop ANSI escapes and non-printable control chars except tab."""
        text = _ANSI_RE.sub("", text)
        return "".join(ch for ch in text if ch == "\t" or ch >= " " )

    def _log_line(self, line: str) -> None:
        """Write a received line to the log file (no-op if log not active)."""
        with self._log_lock:
            if self._log_file is not None:
                try:
                    payload = self._strip_for_log(line) if self._log_strip else line
                    ts = self._format_ts()
                    prefix = f"[{ts}] " if ts else ""
                    self._log_file.write(prefix + payload + "\n")
                    self._log_file.flush()
                except OSError as exc:
                    log.warning("log write failed: %s", exc)

    def ping(self) -> bool:
        if self._ser is None:
            return False
        return self._ser.is_open


def _ascii_score(s: str) -> float:
    """Fraction of characters in *s* that are printable ASCII or whitespace."""
    if not s:
        return 0.0
    good = sum(1 for c in s if 32 <= ord(c) < 127 or c in "\r\n\t")
    return good / len(s)


# ---------------------------------------------------------------------------
# Client connection handler
# ---------------------------------------------------------------------------

def handle_client(conn: socket.socket, addr: str, worker: SerialWorker) -> None:
    log.debug("client connected: %s", addr)
    buf = bytearray()
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)

            # process all complete lines in the buffer
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                if not raw.strip():
                    continue
                try:
                    req = json.loads(raw.decode())
                except json.JSONDecodeError as exc:
                    _send(conn, make_err(f"bad JSON: {exc}"))
                    continue

                cmd = req.get("cmd", "")

                if cmd == "ping":
                    _send(conn, make_ok("pong"))

                elif cmd == "query":
                    line_str = req.get("line", "")
                    timeout_ms = int(req.get("timeout_ms", 500))
                    include_ts = bool(req.get("include_timestamp", False))
                    ts_fmt = req.get("timestamp_format")
                    try:
                        if include_ts:
                            out = worker.query_with_timestamp(line_str, timeout_ms, ts_fmt)
                            _send(conn, {"ok": True, **out})
                        else:
                            resp = worker.query(line_str, timeout_ms)
                            _send(conn, make_ok(resp))
                    except (IOError, ValueError) as exc:
                        _send(conn, make_err(str(exc)))

                elif cmd == "set_baud":
                    try:
                        worker.set_baud(int(req["baud"]))
                        _send(conn, {"ok": True, "baud": worker.baud})
                    except (KeyError, ValueError, IOError, serial.SerialException) as exc:
                        _send(conn, make_err(f"set_baud: {exc}"))

                elif cmd == "set_eol":
                    try:
                        worker.set_eol(req["eol"])
                        _send(conn, {"ok": True, "eol": worker.eol})
                    except (KeyError, ValueError) as exc:
                        _send(conn, make_err(f"set_eol: {exc}"))

                elif cmd == "detect_baud":
                    probe = req.get("probe", "?")
                    timeout_ms = int(req.get("timeout_ms", 200))
                    cands = req.get("candidates")
                    cands_t = tuple(int(x) for x in cands) if cands else None
                    try:
                        baud = worker.detect_baud(probe, timeout_ms, cands_t)
                        _send(conn, {"ok": True, "baud": baud})
                    except (IOError, serial.SerialException) as exc:
                        _send(conn, make_err(f"detect_baud: {exc}"))

                elif cmd == "detect_eol":
                    probe = req.get("probe", "?")
                    timeout_ms = int(req.get("timeout_ms", 500))
                    try:
                        eol = worker.detect_eol(probe, timeout_ms)
                        _send(conn, {"ok": True, "eol": eol})
                    except IOError as exc:
                        _send(conn, make_err(f"detect_eol: {exc}"))

                elif cmd == "info":
                    _send(conn, {"ok": True, "info": worker.info()})

                elif cmd == "set_map":
                    try:
                        maps = worker.set_map(req.get("maps", ""))
                        _send(conn, {"ok": True, "maps": sorted(maps)})
                    except ValueError as exc:
                        _send(conn, make_err(f"set_map: {exc}"))

                elif cmd == "set_timestamp":
                    try:
                        worker.set_timestamp(req.get("format"))
                        _send(conn, {"ok": True, "ts_format": worker._ts_format})
                    except ValueError as exc:
                        _send(conn, make_err(f"set_timestamp: {exc}"))

                elif cmd == "log_start":
                    path = req.get("path", "")
                    strip = bool(req.get("strip", False))
                    if not path:
                        _send(conn, make_err("log_start: path required"))
                    else:
                        try:
                            worker.set_log_strip(strip)
                            worker.log_start(path)
                            _send(conn, {"ok": True, "log_path": path, "log_strip": strip})
                        except OSError as exc:
                            _send(conn, make_err(f"log_start: {exc}"))

                elif cmd == "log_stop":
                    worker.log_stop()
                    _send(conn, make_ok("log stopped"))

                else:
                    _send(conn, make_err(f"unknown cmd: {cmd!r}"))

    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        conn.close()
        log.debug("client disconnected: %s", addr)


def _send(conn: socket.socket, obj: dict) -> None:
    try:
        conn.sendall((json.dumps(obj) + "\n").encode())
    except (BrokenPipeError, OSError):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="awto serial daemon")
    ap.add_argument("--port",   default=DEFAULT_PORT,        help="serial device")
    ap.add_argument("--baud",   default=DEFAULT_BAUD, type=int, help="baud rate")
    ap.add_argument("--eol",    default=DEFAULT_EOL,
                    choices=list(EOL_BYTES.keys()),
                    help="line ending used for outgoing query() calls")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH, help="Unix socket path")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--map", default="", metavar="MAPS",
                    help="comma-separated char maps: INLCRNL,ICRNL,ONLCRNL,ODELBS")
    ap.add_argument("--log-file", default=None, metavar="PATH",
                    help="log all RX data to this file (always appended, never deleted)")
    ap.add_argument("--log-strip", action="store_true",
                    help="strip ANSI/control chars before writing log lines")
    ap.add_argument("--timestamp", default=None, choices=["iso8601", "24hour", "epoch"],
                    help="prepend timestamp to log lines")
    args = ap.parse_args()

    _setup_logging("awto-serial-daemon", args.log_level)

    worker = SerialWorker(args.port, args.baud, eol=args.eol)
    if args.map:
        worker.set_map(args.map)
    if args.log_file:
        worker.set_log_strip(args.log_strip)
        worker.log_start(args.log_file)
    if args.timestamp:
        worker.set_timestamp(args.timestamp)
    try:
        worker.open()
    except serial.SerialException as exc:
        log.error("cannot open serial port: %s", exc)
        sys.exit(1)

    # remove stale socket
    if os.path.exists(args.socket):
        os.unlink(args.socket)

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(args.socket)
    os.chmod(args.socket, 0o600)
    server_sock.listen(8)

    log.info("listening on %s  (ctrl-c to stop)", args.socket)

    try:
        while True:
            conn, _ = server_sock.accept()
            addr = conn.fileno()
            t = threading.Thread(
                target=handle_client,
                args=(conn, addr, worker),
                daemon=True,
            )
            t.start()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server_sock.close()
        if os.path.exists(args.socket):
            os.unlink(args.socket)
        worker.close()


if __name__ == "__main__":
    main()
