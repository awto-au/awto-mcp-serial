# awto-mcp-serial

MCP serial daemon for VS Code Copilot — baud/EOL auto-detection, Unix socket IPC, FastMCP tools.

Lets GitHub Copilot (or any MCP client) send commands to a serial device and read responses, with automatic baud-rate and line-ending detection.

---

## Architecture

```
VS Code Copilot
      │  stdio (MCP protocol)
      ▼
 mcp_server.py          ← FastMCP stdio server, one tool per operation
      │  AF_UNIX socket  (/tmp/awto-serial.sock)
      ▼
 serial_daemon.py        ← owns the serial port, multiplexes clients
      │  pyserial
      ▼
 /dev/ttyACM0  (or any serial port)
```

---

## Quick Start

### Requirements

- Python 3.13+ (Python 3.14 free-threaded recommended)
- Fedora: `sudo dnf install python3.14-freethreading`
- `uv` for virtual environment management

### Install

```bash
git clone https://github.com/awto-au/awto-mcp-serial
cd awto-mcp-serial
uv venv --python python3.14t .venv-ft
uv pip install -e . --python .venv-ft/bin/python
```

> **Note:** This project uses a uv-managed venv — use `uv pip` not `pip` directly.
> Activate with `source .venv-ft/bin/activate` for interactive use.

### Run the daemon

```bash
.venv-ft/bin/python serial_daemon.py --port /dev/ttyACM0 --baud 2480000 --eol lf
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `/dev/ttyACM0` | Serial device path |
| `--baud` | `2000000` | Initial baud rate |
| `--eol` | `lf` | Line ending: `lf`, `cr`, or `crlf` |
| `--socket` | `/tmp/awto-serial.sock` | Unix socket path |

### VS Code MCP integration

`.vscode/mcp.json` is already configured. Once the daemon is running, open VS Code in this folder and the `awto-serial` MCP server will be available to Copilot automatically.

---

## MCP Tools

| Tool | Arguments | Description |
|------|-----------|-------------|
| `serial_ping` | — | Check daemon is alive |
| `serial_query` | `command`, `timeout_ms`, `include_timestamp`, `timestamp_format` | Send a command, read response (optional timestamp) |
| `serial_info` | — | Show port, baud, eol, is_open |
| `serial_set_baud` | `baud` | Change baud rate live |
| `serial_set_eol` | `eol` | Change line ending (`lf`/`cr`/`crlf`) |
| `serial_detect_baud` | `probe`, `timeout_ms` | Auto-detect baud (fastest-first: 2480000→9600) |
| `serial_detect_eol` | `probe`, `timeout_ms` | Auto-detect line ending |
| `serial_set_timestamp` | `format` | Set timestamp format: `iso8601` / `24hour` / `epoch` |
| `serial_log_start` | `path`, `strip` | Start append-only RX logging (optional ANSI/control stripping) |
| `serial_log_stop` | — | Stop RX logging |

Example Copilot prompts:
```
ping the serial daemon
detect the baud rate
send "status" to the serial port
```

---

## CLI Tool (`ttu_cli.py`)

```bash
.venv-ft/bin/python ttu_cli.py ping
.venv-ft/bin/python ttu_cli.py info
.venv-ft/bin/python ttu_cli.py query "status"
.venv-ft/bin/python ttu_cli.py query "status" --timestamp epoch
.venv-ft/bin/python ttu_cli.py set-baud 2480000
.venv-ft/bin/python ttu_cli.py set-eol crlf
.venv-ft/bin/python ttu_cli.py detect-baud --probe "?"
.venv-ft/bin/python ttu_cli.py detect-eol
.venv-ft/bin/python ttu_cli.py set-timestamp iso8601
.venv-ft/bin/python ttu_cli.py log-start /tmp/awto-rx.log --strip --timestamp 24hour
.venv-ft/bin/python ttu_cli.py log-stop
echo "status" | .venv-ft/bin/python ttu_cli.py query   # pipe from stdin
```

---

## Tests

```bash
.venv-ft/bin/python test_harness.py -v
```

44 tests across 5 layers — no hardware required (serial port is mocked).

---

## Baud Rate Detection

The daemon probes candidate rates fastest-first and selects the first that returns ≥80% printable ASCII with ≥4 bytes:

```
2_480_000 → 2_000_000 → 1_500_000 → 1_152_000 → 1_000_000
→ 921_600 → 576_000 → 500_000 → 460_800 → 230_400
→ 115_200 → 57_600 → 38_400 → 19_200 → 9_600
```

---

## Code Style

See [CODING_STYLE.md](CODING_STYLE.md) for conventions used across all awto-au Python repositories.

---

## License

MIT
