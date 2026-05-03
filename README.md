# deepseek-cli

Headless CLI for [DeepSeek Chat](https://chat.deepseek.com). Works on Linux, macOS, and Windows.

**Two modes:**
- **TUI mode** — Interactive chat with slash commands, header bar, formatted output
- **Raw mode** (`--headless`) — Scripting / piping, plain text output

---

## Install

```bash
pip install -r requirements.txt
playwright install chromium
```

Or manually:
```bash
pip install playwright rich nest_asyncio
playwright install chromium
```

---

## First Login

```bash
python deepseek_cli.py -e you@email.com -p "password" -n
```

Your session is saved to `~/.config/deepseek-cli/session.json`. You won't need to enter credentials again until the session expires.

---

## Usage

### TUI Mode (interactive)

```bash
python deepseek_cli.py -n                    # New chat
python deepseek_cli.py -n --expert --think   # Expert + DeepThink
python deepseek_cli.py --chat CHAT_ID        # Continue a chat
python deepseek_cli.py                       # Resume last session
```

### Raw Mode (scripting)

```bash
python deepseek_cli.py --headless -n -m "What is 2+2?"
python deepseek_cli.py --headless -n --think --search -m "Latest AI news"
python deepseek_cli.py --headless --chat ID -m "Continue"
```

---

## Slash Commands (TUI)

| Command | Description |
|---------|-------------|
| `/new` | New chat |
| `/think` | Toggle DeepThink |
| `/search` | Toggle Search |
| `/mode` | Toggle Instant/Expert |
| `/list` | List your chats (full IDs) |
| `/open <id>` | Open a chat |
| `/file <path>` | Attach a file |
| `/clean` | Clear screen |
| `/?` | Help |
| `/quit` | Exit |

---

## Options

| Flag | Description |
|------|-------------|
| `-e, --email` | Email (or set `DEEPSEEK_EMAIL`) |
| `-p, --password` | Password (or set `DEEPSEEK_PASSWORD`) |
| `-n, --new` | Start a new chat |
| `--chat ID` | Continue a chat |
| `--expert` | Expert mode |
| `--think` | Enable DeepThink |
| `--search` | Enable Search |
| `--headless` | Raw text mode (no TUI) |
| `-m, --message` | Message to send (raw mode) |
| `-f, --file` | File to attach (raw mode) |
| `--show-browser` | Show the browser window |

---

## Linux: Run from anywhere

```bash
chmod +x deepseek_cli.py
mkdir -p ~/.local/bin
ln -s "$(pwd)/deepseek_cli.py" ~/.local/bin/deepseek-cli
# Now use: deepseek-cli -n
```

---

## Windows: Run from anywhere

Create `deepseek-cli.bat` in a folder on your PATH:

```bat
@echo off
python "C:\path\to\deepseek_cli.py" %*
```

---

## License

MIT
