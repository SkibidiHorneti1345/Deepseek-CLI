#!/usr/bin/env python3
"""
deepseek-cli
~~~~~~~~~~~~

Headless CLI for DeepSeek Chat (chat.deepseek.com).

  TUI mode:      deepseek-cli -n
  Script mode:   deepseek-cli --headless -m "What is 2+2?"

Install: pip install playwright rich nest_asyncio && playwright install chromium

Repo:    https://github.com/YOURNAME/deepseek-cli
License: MIT
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

if platform.system() == "Windows":
    import nest_asyncio
    nest_asyncio.apply()

try:
    from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
except ImportError:
    print("pip install playwright rich nest_asyncio")
    print("playwright install chromium")
    sys.exit(1)

console = Console(highlight=False)


def pprint(text: str = ""):
    console.out(text)


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------
SESSION_DIR = os.path.expanduser("~/.config/deepseek-cli")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")


def load_session() -> Dict[str, str]:
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_session(token: str, email: str = ""):
    try:
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump({"token": token, "email": email, "saved_at": time.time()}, f)
    except Exception:
        pass


def clear_session():
    try:
        os.remove(SESSION_FILE)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Cfg:
    email: str = ""
    password: str = ""
    headless: bool = True
    model_type: str = "default"
    thinking: bool = False
    search: bool = False


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
class DeepSeekCLI:
    BASE_URL = "https://chat.deepseek.com"

    # Login page
    SEL_SOCIAL_BTNS = ".ds-sign-in-form__social-button"
    SEL_LOGIN_BTN = ".ds-basic-button--primary"

    # Chat page
    SEL_NEW_CHAT = 'a[href="/"]'
    SEL_TEXTAREA = "textarea"
    SEL_TOGGLE = ".ds-toggle-button"
    SEL_TOGGLE_SELECTED = "ds-toggle-button--selected"
    SEL_MODE_INSTANT = "div[class*='_9f2341b'][class*='_7ac2123']"

    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.chat_id: str = ""
        self._pw = None

    # -- Safe navigation: catches timeouts and all Playwright errors --
    async def _safe_goto(self, url: str, timeout: int = 30_000) -> bool:
        """Navigate with error handling. Returns True if page reached."""
        try:
            await self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass  # networkidle is best-effort
            return True
        except PWTimeout:
            # Even on timeout, check current URL
            try:
                if url.rstrip("/") in self.page.url.rstrip("/"):
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    async def start(self):
        self._pw = await async_playwright().start()
        profile = os.path.join(SESSION_DIR, "chromium-profile")
        os.makedirs(profile, exist_ok=True)

        args = [
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
        ]
        if platform.system() == "Linux":
            args += ["--no-sandbox", "--disable-setuid-sandbox"]

        ctx = await self._pw.chromium.launch_persistent_context(
            profile,
            headless=self.cfg.headless,
            args=args,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        self.context = ctx
        pages = ctx.pages
        self.page = pages[0] if pages else await ctx.new_page()

        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)

    async def stop(self):
        try:
            if self.context:
                await self.context.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    async def ensure_logged_in(self) -> bool:
        session = load_session()
        token = session.get("token", "")

        if token:
            try:
                if await self._inject_token(token):
                    return True
            except Exception as e:
                pprint(f"[WARN] Session injection failed: {e}")
            clear_session()

        if not self.cfg.email or not self.cfg.password:
            pprint("[ERROR] No saved session. Login with: -e EMAIL -p PASSWORD")
            return False

        try:
            if await self._do_login():
                try:
                    tok = await self.page.evaluate("() => localStorage.getItem('userToken') || ''")
                    if tok:
                        save_session(tok, self.cfg.email)
                        pprint("[AUTH] Session saved")
                except Exception:
                    pass
                return True
        except Exception as e:
            pprint(f"[ERROR] Login exception: {e}")
        return False

    async def _inject_token(self, token: str) -> bool:
        pprint("[AUTH] Trying saved session...")
        if not await self._safe_goto(self.BASE_URL, timeout=25_000):
            pprint("[AUTH] Page load timed out")
            return False
        await asyncio.sleep(3)

        try:
            await self.page.evaluate(f"() => {{ localStorage.setItem('userToken', '{token}'); }}")
        except Exception:
            return False

        if not await self._safe_goto(self.BASE_URL, timeout=25_000):
            return False
        await asyncio.sleep(3)

        try:
            return await self.page.query_selector(self.SEL_TEXTAREA) is not None
        except Exception:
            return False

    async def _do_login(self) -> bool:
        pprint("[LOGIN] Opening sign-in page...")
        if not await self._safe_goto(f"{self.BASE_URL}/sign_in", timeout=30_000):
            pprint("[ERROR] Sign-in page load timed out")
            return False
        await asyncio.sleep(5)

        # Find password input
        pw = None
        for sel in ('input[type="password"]', 'input[type="password"].ds-input__input'):
            try:
                pw = await self.page.wait_for_selector(sel, timeout=5000)
                break
            except Exception:
                continue

        if pw is None:
            for btn in await self.page.query_selector_all(self.SEL_SOCIAL_BTNS):
                try:
                    await btn.evaluate("el => el.click()")
                except Exception:
                    pass
                await asyncio.sleep(2)
                try:
                    pw = await self.page.wait_for_selector('input[type="password"]', timeout=5000)
                except Exception:
                    pass
                if pw:
                    break

        if pw is None:
            pprint("[ERROR] Password input not found")
            return False

        email = await self.page.query_selector('input[type="text"]') or \
                await self.page.query_selector('input[type="email"]')
        if email is None:
            pprint("[ERROR] Email input not found")
            return False

        await email.fill(self.cfg.email)
        await pw.fill(self.cfg.password)

        btn = await self.page.query_selector(self.SEL_LOGIN_BTN) or \
              await self.page.query_selector('button[type="submit"]')
        if btn is None:
            pprint("[ERROR] Login button not found")
            return False
        try:
            await btn.click()
        except Exception:
            await btn.evaluate("el => el.click()")

        pprint("[LOGIN] Authenticating...")

        # Wait for redirect (best-effort)
        try:
            await self.page.wait_for_url("https://chat.deepseek.com/", timeout=25_000)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(3)

        url = self.page.url
        if "chat.deepseek.com" in url and "sign_in" not in url:
            pprint("[LOGIN] Success!")
            return True
        pprint(f"[ERROR] Login failed (URL: {url})")
        return False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    async def apply_settings(self):
        await self.set_mode(self.cfg.model_type)
        if await self._toggle_state(0) != self.cfg.thinking:
            await self.toggle_deepthink()
        if await self._toggle_state(1) != self.cfg.search:
            await self.toggle_search()

    async def set_mode(self, mode: str):
        try:
            modes = await self.page.query_selector_all(self.SEL_MODE_INSTANT)
            if len(modes) < 2:
                return
            cls = await modes[0].get_attribute("class") or ""
            is_instant = "_31a22b0" in cls
            if mode == "expert" and is_instant:
                await modes[1].click()
                await asyncio.sleep(0.5)
                pprint("[MODE] Expert")
            elif mode == "default" and not is_instant:
                await modes[0].click()
                await asyncio.sleep(0.5)
                pprint("[MODE] Instant")
        except Exception:
            pass

    async def toggle_deepthink(self) -> bool:
        return await self._toggle(0, "DeepThink")

    async def toggle_search(self) -> bool:
        return await self._toggle(1, "Search")

    async def _toggle(self, idx: int, name: str) -> bool:
        try:
            toggles = await self.page.query_selector_all(self.SEL_TOGGLE)
            if idx >= len(toggles):
                return False
            await toggles[idx].click()
            await asyncio.sleep(0.5)
            state = await self._toggle_state(idx)
            pprint(f"[TOGGLE] {name} -> {'ON' if state else 'OFF'}")
            return state
        except Exception:
            return False

    async def _toggle_state(self, idx: int) -> bool:
        try:
            toggles = await self.page.query_selector_all(self.SEL_TOGGLE)
            if idx >= len(toggles):
                return False
            cls = await toggles[idx].get_attribute("class") or ""
            return self.SEL_TOGGLE_SELECTED in cls
        except Exception:
            return False

    async def state(self) -> Dict[str, Any]:
        try:
            # Method 1: Mode toggle buttons (visible on new chats)
            modes = await self.page.query_selector_all(self.SEL_MODE_INSTANT)
            mode = "unknown"
            if len(modes) >= 2:
                cls = await modes[0].get_attribute("class") or ""
                mode = "default" if "_31a22b0" in cls else "expert"

            # Method 2: Model indicator icon+span (visible on all chats)
            # Structure: <div><div class="ds-icon ...">...</div><span>Instant</span></div>
            if mode == "unknown":
                for icon in await self.page.query_selector_all(".ds-icon"):
                    try:
                        # Get next sibling span
                        span = await icon.evaluate_handle("el => el.nextElementSibling")
                        if span:
                            text = (await span.inner_text()).strip()
                            if text in ("Instant", "快速"):
                                mode = "default"
                                break
                            elif text in ("Expert", "专家"):
                                mode = "expert"
                                break
                    except Exception:
                        continue

            return {
                "mode": mode,
                "deepthink": await self._toggle_state(0),
                "search": await self._toggle_state(1),
                "chat_id": self.chat_id,
                "url": self.page.url,
            }
        except Exception:
            return {"mode": "unknown", "deepthink": False, "search": False, "chat_id": "", "url": ""}

    # ------------------------------------------------------------------
    # Chat operations
    # ------------------------------------------------------------------
    async def new_chat(self):
        try:
            el = await self.page.query_selector(self.SEL_NEW_CHAT)
            if el:
                await el.click()
            else:
                await self._safe_goto(self.BASE_URL)
            await self.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)
        self.chat_id = ""
        await self.apply_settings()
        pprint("[CHAT] Ready!")

    async def open_chat(self, cid: str) -> bool:
        ok = await self._safe_goto(f"{self.BASE_URL}/a/chat/s/{cid}", timeout=20_000)
        if not ok:
            return False
        await asyncio.sleep(2)
        if cid in self.page.url:
            self.chat_id = cid
            return True
        return False

    async def list_chats(self) -> List[Dict[str, str]]:
        try:
            links = await self.page.query_selector_all('a[href*="/chat/s/"]')
        except Exception:
            return []
        out: List[Dict[str, str]] = []
        seen = set()
        for link in links:
            try:
                href = await link.get_attribute("href")
                if not href or "/chat/s/" not in href:
                    continue
                cid = href.split("/chat/s/")[-1].split("/")[0]
                if len(cid) < 30 or cid in seen:
                    continue
                title = (await link.inner_text()).strip().split("\n")[0].strip()
                if title in ("", "Today", "7 Days", "30 Days"):
                    continue
                seen.add(cid)
                out.append({"id": cid, "title": title})
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------
    # Send / receive
    # ------------------------------------------------------------------
    async def send(self, text: str, file_path: Optional[str] = None) -> Dict[str, Any]:
        if not text.strip():
            return {"ok": False, "error": "Empty message"}
        if file_path:
            if not await self._attach(file_path):
                return {"ok": False, "error": f"Attach failed: {file_path}"}
        try:
            ta = await self.page.query_selector(self.SEL_TEXTAREA)
            if ta is None:
                return {"ok": False, "error": "Textarea not found"}
            await ta.fill(text)
            await asyncio.sleep(0.5)
            await ta.press("Enter")
            return await self._recv()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _attach(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            inp = await self.page.query_selector('input[type="file"]')
            if inp:
                await inp.set_input_files(path)
            else:
                async with self.page.expect_file_chooser(timeout=5000) as fc:
                    for b in (await self.page.query_selector_all("button"))[-4:]:
                        cls = await b.get_attribute("class") or ""
                        if "disabled" not in cls and "toggle" not in cls:
                            await b.click()
                            break
                    chooser = await fc.value
                    await chooser.set_files(path)
            await asyncio.sleep(2)
            return True
        except Exception:
            return False

    async def _recv(self, timeout: int = 60) -> Dict[str, Any]:
        """Wait for the AI response. Handles streaming, DeepThink, and Search."""
        start = time.time()
        last_resp = ""
        last_count = 0
        stable = 0

        # CRITICAL: Count how many markdown blocks exist BEFORE we start waiting.
        # Old blocks from previous messages stay in the DOM. We only want NEW ones.
        try:
            initial_count = await self.page.evaluate(
                "() => document.querySelectorAll('.ds-markdown').length"
            )
        except Exception:
            initial_count = 0

        while time.time() - start < timeout:
            await asyncio.sleep(1)

            try:
                result = await self.page.evaluate(
                    """() => {
                        const md = document.querySelectorAll(".ds-markdown");
                        const texts = [];
                        for (const m of md) {
                            const t = m.textContent.trim();
                            if (t) texts.push(t);
                        }
                        const th = document.querySelectorAll(".ds-think-content");
                        const parts = [];
                        for (const el of th) {
                            const t = el.textContent.trim();
                            if (t) parts.push(t);
                        }
                        return { texts, count: md.length, thinking: parts.join("\\n") };
                    }"""
                )
            except Exception:
                continue

            texts = result.get("texts", [])
            count = result.get("count", 0)
            thinking = result.get("thinking", "")

            # Slice to only NEW blocks (after initial_count)
            # This prevents picking up old responses from previous messages
            new_texts = texts[initial_count:] if len(texts) > initial_count else texts

            if not new_texts:
                continue

            resp = self._pick_best_block(new_texts, thinking)

            if resp != last_resp or count != last_count:
                last_resp = resp
                last_count = count
                stable = 0
                continue

            stable += 1
            if stable >= 3:
                await asyncio.sleep(1)
                try:
                    final = await self.page.evaluate(
                        """() => {
                            const md = document.querySelectorAll(".ds-markdown");
                            const texts = [];
                            for (const m of md) {
                                const t = m.textContent.trim();
                                if (t) texts.push(t);
                            }
                            const th = document.querySelectorAll(".ds-think-content");
                            const parts = [];
                            for (const el of th) {
                                const t = el.textContent.trim();
                                if (t) parts.push(t);
                            }
                            return { texts, thinking: parts.join("\\n") };
                        }"""
                    )
                    final_texts = final.get("texts", [])
                    final_new = final_texts[initial_count:] if len(final_texts) > initial_count else final_texts
                    return {
                        "ok": True,
                        "response": self._pick_best_block(final_new, final.get("thinking", "")) or resp,
                        "thinking": final.get("thinking", thinking),
                    }
                except Exception:
                    return {"ok": True, "response": resp, "thinking": thinking}

        return {"ok": False, "error": "Timeout", "response": last_resp}

    def _pick_best_block(self, texts: List[str], thinking: str = "") -> str:
        """Filter out planning/thinking blocks, return the best answer block."""
        if not texts:
            return ""
        planning_phrases = (
            "i need to search", "i will search", "i should search",
            "i need to look", "i will look", "let me search",
            "the user is asking", "i need to provide",
            "i will provide", "i need to find", "let me look",
            "i'll search", "i need to check", "i will check",
            "the search results show", "i need to open",
        )
        candidates = []
        think_lower = thinking.lower()
        for t in texts:
            lower = t.lower()
            if len(t) < 15:
                continue
            if any(p in lower for p in planning_phrases):
                continue
            if think_lower and (lower in think_lower or think_lower in lower):
                continue
            candidates.append(t)
        if candidates:
            return max(candidates, key=len)
        return texts[-1] if texts else ""


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def print_result(result: Dict[str, Any]):
    if not result.get("ok"):
        pprint(f"[ERROR] {result.get('error', 'Unknown')}")
        return
    if result.get("thinking"):
        console.print(Panel(result["thinking"], title="DeepThink", border_style="yellow"))
    if result.get("response"):
        console.print(Panel(Markdown(result["response"]), title="DeepSeek", border_style="green"))


def print_chats(sessions: List[Dict[str, str]]):
    if not sessions:
        pprint("No chats found")
        return
    pprint("\n  --- Your Chats ---")
    for i, s in enumerate(sessions, 1):
        pprint(f"  {i}. [{s['id']}] {s['title']}")
    pprint()


def clear_screen():
    os.system("cls" if platform.system() == "Windows" else "clear")


async def draw_header_once(cli: DeepSeekCLI, cfg: Cfg):
    try:
        st = await cli.state()
        mode = st.get("mode", cfg.model_type).upper()
        cid = cli.chat_id[:16] if cli.chat_id else "New Chat"
        dt = "ON" if st.get("deepthink", cfg.thinking) else "OFF"
        se = "ON" if st.get("search", cfg.search) else "OFF"
        hdr = f"[bold white]Mode:[/bold white] {mode:7}  [bold white]Chat:[/bold white] {cid:20}  |  [bold white]Think:[/bold white] {dt:3}  |  [bold white]Search:[/bold white] {se:3}"
        console.rule(hdr, style="blue")
        pprint()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Interactive (TUI)
# ---------------------------------------------------------------------------
async def interactive(cli: DeepSeekCLI, cfg: Cfg):
    pending_file: Optional[str] = None

    await draw_header_once(cli, cfg)
    pprint("  Type /? for help, /quit to exit.\n")

    while True:
        try:
            user = console.input("[bold blue]> [/bold blue]").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not user:
            continue

        if user == "/quit":
            break
        if user in ("/?", "/help"):
            pprint("\n  Commands:")
            pprint("    /new              New chat")
            pprint("    /clean            Clear screen")
            pprint("    /think            Toggle DeepThink")
            pprint("    /search           Toggle Search")
            pprint("    /mode             Toggle Instant/Expert")
            pprint("    /list             List your chats")
            pprint("    /open <id>        Open a chat")
            pprint("    /file <path>      Attach file")
            pprint("    /?                This help")
            pprint("    /quit             Exit\n")
            continue
        if user == "/new":
            try:
                await cli.new_chat()
                pending_file = None
                clear_screen()
                await draw_header_once(cli, cfg)
                console.print("\n  [bold]New chat[/bold]\n")
            except Exception as e:
                pprint(f"  [ERROR] {e}\n")
            continue
        if user == "/clean":
            clear_screen()
            await draw_header_once(cli, cfg)
            console.print("\n  [bold]Cleared[/bold]\n")
            continue
        if user == "/think":
            try:
                s = await cli.toggle_deepthink()
                pprint(f"  DeepThink: {'ON' if s else 'OFF'}\n")
            except Exception as e:
                pprint(f"  [ERROR] {e}\n")
            continue
        if user == "/search":
            try:
                s = await cli.toggle_search()
                pprint(f"  Search: {'ON' if s else 'OFF'}\n")
            except Exception as e:
                pprint(f"  [ERROR] {e}\n")
            continue
        if user == "/mode":
            cfg.model_type = "expert" if cfg.model_type == "default" else "default"
            pprint(f"  Mode: {cfg.model_type.upper()} (next /new)\n")
            continue
        if user == "/list":
            try:
                print_chats(await cli.list_chats())
            except Exception as e:
                pprint(f"  [ERROR] {e}\n")
            continue
        if user.startswith("/open "):
            cid = user[6:].strip()
            try:
                if await cli.open_chat(cid):
                    pending_file = None
                    clear_screen()
                    await draw_header_once(cli, cfg)
                    console.print(f"\n  [bold]Chat {cid[:16]}[/bold]\n")
                else:
                    pprint(f"  [ERROR] Could not open {cid}\n")
            except Exception as e:
                pprint(f"  [ERROR] {e}\n")
            continue
        if user.startswith("/file "):
            p = user[6:].strip().strip('"\'')
            p = os.path.abspath(os.path.expanduser(p))
            if os.path.exists(p):
                pending_file = p
                pprint(f"  [FILE] {p}\n")
            else:
                pprint(f"  [ERROR] Not found: {p}\n")
            continue
        if user.startswith("/"):
            pprint("  Unknown command. Type /? for help.\n")
            continue

        # Send message
        try:
            result = await cli.send(user, pending_file)
            pending_file = None
            if result.get("ok"):
                if result.get("thinking"):
                    console.print(Panel(result["thinking"], title="DeepThink", border_style="yellow"))
                if result.get("response"):
                    console.print(Panel(Markdown(result["response"]), title="DeepSeek", border_style="green"))
            else:
                pprint(f"[ERROR] {result.get('error', 'Failed')}")
            pprint()
        except Exception as e:
            pprint(f"[ERROR] {e}\n")

    pprint("Goodbye!")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="DeepSeek Chat CLI")
    p.add_argument("-e", "--email", default=os.environ.get("DEEPSEEK_EMAIL", ""))
    p.add_argument("-p", "--password", default=os.environ.get("DEEPSEEK_PASSWORD", ""))
    p.add_argument("-n", "--new", action="store_true", help="Start a new chat")
    p.add_argument("--chat", metavar="ID", help="Continue an existing chat")
    p.add_argument("--expert", action="store_true", help="Expert mode")
    p.add_argument("--think", action="store_true", help="Enable DeepThink")
    p.add_argument("--search", action="store_true", help="Enable Search")
    p.add_argument("--headless", action="store_true",
                    help="Raw text mode (no TUI). Requires -m.")
    p.add_argument("-m", "--message", help="Message to send (--headless mode)")
    p.add_argument("-f", "--file", help="File to attach (--headless mode)")
    p.add_argument("--show-browser", action="store_true",
                    help="Show the browser window")
    return p


async def _run(args) -> int:
    cfg = Cfg(
        email=args.email,
        password=args.password,
        headless=not args.show_browser,
        model_type="expert" if args.expert else "default",
        thinking=args.think,
        search=args.search,
    )
    cli = DeepSeekCLI(cfg)

    try:
        await cli.start()
    except Exception as e:
        pprint(f"[ERROR] Browser failed to start: {e}")
        return 1

    try:
        if not await cli.ensure_logged_in():
            return 1

        # Raw text mode
        if args.headless:
            if not args.message:
                pprint("[ERROR] --headless requires -m")
                return 1
            if args.new:
                await cli.new_chat()
            elif args.chat:
                if not await cli.open_chat(args.chat):
                    return 1
            else:
                await cli.apply_settings()
            result = await cli.send(args.message, args.file)
            if result.get("ok"):
                if result.get("thinking"):
                    pprint(f"[DeepThink]\n{result['thinking']}\n")
                pprint(result.get("response", ""))
            else:
                pprint(f"[ERROR] {result.get('error', 'Failed')}")
                return 1
            return 0

        # TUI mode
        await asyncio.sleep(2)
        clear_screen()
        if args.chat:
            if not await cli.open_chat(args.chat):
                return 1
        elif args.new:
            await cli.new_chat()
        else:
            await cli.apply_settings()
        await interactive(cli, cfg)
        return 0

    finally:
        await cli.stop()
        await asyncio.sleep(0.3)


def main():
    parser = build_parser()
    args = parser.parse_args()

    has_session = bool(load_session().get("token"))
    if not has_session and (not args.email or not args.password):
        pprint("[ERROR] No saved session. Login with: -e EMAIL -p PASSWORD")
        sys.exit(1)

    try:
        if platform.system() == "Windows":
            loop = asyncio.get_event_loop()
            sys.exit(loop.run_until_complete(_run(args)))
        else:
            sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
