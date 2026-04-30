#!/usr/bin/env python3
"""
ttu  —  CLI client for the awto serial daemon.

Subcommands (chosen to mirror tio(1) where applicable):

    ttu_cli.py query "status"            send command, print response
    ttu_cli.py ping                      health-check
    ttu_cli.py info                      show port/baud/eol
    ttu_cli.py set-baud 2480000          change baud rate live
    ttu_cli.py set-eol crlf              set line ending (lf|cr|crlf)
    ttu_cli.py detect-baud               auto-detect baud (fastest first)
    ttu_cli.py detect-eol                auto-detect line ending

Backwards-compat shorthand:
    ttu_cli.py "status"                  ≡ ttu_cli.py query status

Stdin pipe:
    echo status | ttu_cli.py query       reads command from stdin

Requires the free-threaded (no-GIL) CPython build: python3.14t
"""

import argparse
import logging
import logging.handlers
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from protocol import DEFAULT_SOCKET_PATH, DEFAULT_TIMEOUT_MS, EOL_BYTES, send_request

# ---------------------------------------------------------------------------
# Logging  (syslog + stderr)
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(level)

    try:
        syslog = logging.handlers.SysLogHandler(
            address="/dev/log",
            facility=logging.handlers.SysLogHandler.LOG_USER,
        )
        syslog.ident = "awto-ttu: "
        syslog.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(syslog)
    except OSError:
        pass

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(
        logging.Formatter("ttu[%(process)d]: %(levelname)-8s %(message)s")
    )
    root.addHandler(stderr)


log = logging.getLogger("cli")

# ---------------------------------------------------------------------------
# Daemon communication
# ---------------------------------------------------------------------------

def _call(req: dict) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(DEFAULT_SOCKET_PATH)
            return send_request(sock, req)
    except FileNotFoundError:
        print(
            f"error: daemon socket not found at {DEFAULT_SOCKET_PATH}\n"
            "       Start the daemon first:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)
    except ConnectionRefusedError:
        print(
            "error: daemon is not running\n"
            "       Start it with:  python serial_daemon.py",
            file=sys.stderr,
        )
        sys.exit(1)


def _print_response(resp: dict) -> None:
    """Pretty-print the most useful field for each response type."""
    if "response" in resp:
        if "timestamp" in resp and resp["timestamp"]:
            print(f"[{resp['timestamp']}] {resp['response']}")
        else:
            print(resp["response"])
    elif "info" in resp:
        info = resp["info"]
        for k, v in info.items():
            print(f"{k}: {v}")
    elif "baud" in resp:
        print(f"baud={resp['baud']}")
    elif "eol" in resp:
        print(f"eol={resp['eol']}")
    else:
        print(resp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ttu",
        description="Send ASCII commands and control to the awto serial daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    ap.add_argument("--timeout", "-t", type=int, default=DEFAULT_TIMEOUT_MS,
                    metavar="MS",
                    help=f"response timeout in ms (default {DEFAULT_TIMEOUT_MS})")

    sub = ap.add_subparsers(dest="subcmd", metavar="SUBCMD")

    sp_query = sub.add_parser("query", help="send command, print response")
    sp_query.add_argument("text", nargs="?", default=None,
                          help="command text (or omit to read from stdin)")
    sp_query.add_argument("--timestamp", choices=["iso8601", "24hour", "epoch"],
                          help="include timestamp in the response")

    sub.add_parser("ping", help="health-check the daemon")
    sub.add_parser("info", help="show current port / baud / eol")

    sp_baud = sub.add_parser("set-baud", help="set baud rate live")
    sp_baud.add_argument("baud", type=int)

    sp_eol = sub.add_parser("set-eol", help="set line ending")
    sp_eol.add_argument("eol", choices=list(EOL_BYTES.keys()))

    sp_db = sub.add_parser("detect-baud", help="auto-detect baud rate (fastest first)")
    sp_db.add_argument("--probe", default="?", help="probe string (default '?')")

    sp_de = sub.add_parser("detect-eol", help="auto-detect line ending")
    sp_de.add_argument("--probe", default="?", help="probe string (default '?')")

    sp_sm = sub.add_parser("set-map", help="set character mapping (tio -m style)")
    sp_sm.add_argument("maps", help="comma-separated: INLCRNL,ICRNL,ONLCRNL,ODELBS (empty clears)")

    sp_ls = sub.add_parser("log-start", help="start logging RX data to file (always appended)")
    sp_ls.add_argument("path", help="log file path")
    sp_ls.add_argument("--strip", action="store_true",
                       help="strip ANSI/control chars before logging")
    sp_ls.add_argument("--timestamp", choices=["iso8601", "24hour", "epoch"],
                       help="also set timestamp format")

    sub.add_parser("log-stop", help="stop logging RX data")

    sp_ts = sub.add_parser("set-timestamp", help="set log timestamp format")
    sp_ts.add_argument("format", nargs="?", default="",
                       choices=["iso8601", "24hour", "epoch", ""],
                       help="timestamp format (empty to disable)")

    # Back-compat: ``ttu_cli.py "status"`` with no subcommand → query
    args, leftover = ap.parse_known_args()
    if args.subcmd is None and leftover:
        args.subcmd = "query"
        args.text = " ".join(leftover)
    elif args.subcmd is None:
        ap.print_help()
        sys.exit(2)

    _setup_logging(args.verbose)

    if args.subcmd == "ping":
        resp = _call({"cmd": "ping"})
    elif args.subcmd == "info":
        resp = _call({"cmd": "info"})
    elif args.subcmd == "set-baud":
        resp = _call({"cmd": "set_baud", "baud": args.baud})
    elif args.subcmd == "set-eol":
        resp = _call({"cmd": "set_eol", "eol": args.eol})
    elif args.subcmd == "detect-baud":
        resp = _call({"cmd": "detect_baud", "probe": args.probe,
                      "timeout_ms": args.timeout})
    elif args.subcmd == "detect-eol":
        resp = _call({"cmd": "detect_eol", "probe": args.probe,
                      "timeout_ms": args.timeout})
    elif args.subcmd == "set-map":
        resp = _call({"cmd": "set_map", "maps": args.maps})
    elif args.subcmd == "log-start":
        resp = _call({"cmd": "log_start", "path": args.path, "strip": args.strip})
        if resp.get("ok") and getattr(args, "timestamp", None):
            ts_resp = _call({"cmd": "set_timestamp", "format": args.timestamp})
            if not ts_resp.get("ok"):
                print(f"error: {ts_resp.get('error', 'unknown')}", file=sys.stderr)
                sys.exit(1)
    elif args.subcmd == "log-stop":
        resp = _call({"cmd": "log_stop"})
    elif args.subcmd == "set-timestamp":
        resp = _call({"cmd": "set_timestamp", "format": args.format})
    elif args.subcmd == "query":
        text = args.text
        if text is None:
            if sys.stdin.isatty():
                print("error: no command given (provide arg or pipe to stdin)",
                      file=sys.stderr)
                sys.exit(2)
            text = sys.stdin.read().strip()
        log.debug("query: %r timeout=%dms", text, args.timeout)
        req = {"cmd": "query", "line": text, "timeout_ms": args.timeout}
        if getattr(args, "timestamp", None):
            req["include_timestamp"] = True
            req["timestamp_format"] = args.timestamp
        resp = _call(req)
    else:
        ap.print_help()
        sys.exit(2)

    if not resp.get("ok"):
        print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)

    _print_response(resp)


if __name__ == "__main__":
    main()
