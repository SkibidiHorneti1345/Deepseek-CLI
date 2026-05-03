#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    debug: bool = False


# ---------------------------------------------------------------------------
# Self-Healing Element Finder
# ---------------------------------------------------------------------------
class SmartFinder:
    """Finds DOM elements using multiple fallback strategies.
    When UI changes, only the broken strategy needs updating.
    """

    def __init__(self, page: Page):
        self.page = page
        self.log: List[Tuple[str, str, bool]] = []  # (name, strategy, success)

    async def find(self, name: str, strategies: List[Tuple[str, str]], timeout: int = 3000) -> Optional[Any]:
        """Try strategies until one works. Returns the element or None."""
        for strategy_name, selector in strategies:
            try:
                # For text-based selectors (has-text, xpath contains)
                if selector.startswith("xpath="):
                    el = await self.page.query_selector(selector)
                elif ":has-text(" in selector or ":has(" in selector:
                    el = await self.page.query_selector(selector)
                else:
                    el = await self.page.query_selector(selector)

                if el is not None:
                    visible = await el.is_visible()
                    if visible:
                        self.log.append((name, strategy_name, True))
                        return el
                    else:
                        self.log.append((name, f"{strategy_name} (hidden)", False))
                else:
                    self.log.append((name, strategy_name, False))
            except Exception as e:
                self.log.append((name, f"{strategy_name} ({e.__class__.__name__})", False))
                continue

        # All strategies failed
        return None

    async def find_email_input(self) -> Optional[Any]:
        """Find email/username input field."""
        el = await self.find("email_input", [
            ('type=email', 'input[type="email"]'),
            ('type=text', 'input[type="text"]'),
            ('placeholder email', 'input[placeholder*="mail" i]'),
            ('placeholder username', 'input[placeholder*="user" i]'),
            ('first text input', 'input[type="text"]:visible'),
            ('any email-like', 'input[type="email"], input[type="text"]'),
        ])
        if el:
            return el
        # Fallback: first visible text input in top half of page
        return await self._find_input_by_position("text")

    async def find_password_input(self) -> Optional[Any]:
        """Find password input field."""
        el = await self.find("password_input", [
            ('type=password', 'input[type="password"]'),
            ('password class', 'input[type="password"].ds-input__input'),
            ('placeholder password', 'input[type="password"][placeholder*="pass" i]'),
            ('any password', 'input[type="password"]:visible'),
        ])
        if el:
            return el
        # Fallback: first visible password input anywhere
        return await self._find_input_by_position("password")

    async def _find_input_by_position(self, input_type: str) -> Optional[Any]:
        """Auto-discover input by position and type."""
        try:
            if input_type == "password":
                candidates = await self.page.query_selector_all('input[type="password"]:visible')
            else:
                candidates = await self.page.query_selector_all('input[type="text"]:visible, input[type="email"]:visible')
            if not candidates:
                return None
            best = None
            best_y = float('inf')
            for c in candidates:
                box = await c.bounding_box()
                if box and box["y"] < best_y:
                    best_y = box["y"]
                    best = c
            if best:
                self.log.append((f"{input_type}_input_auto", f"position-based (y={best_y:.0f})", True))
            return best
        except Exception:
            return None

    async def find_login_button(self) -> Optional[Any]:
        """Find login/submit button."""
        el = await self.find("login_button", [
            ('text Log in', 'button:has-text("Log in")'),
            ('text 登录', 'button:has-text("登录")'),
            ('primary class', '.ds-basic-button--primary'),
            ('submit type', 'button[type="submit"]'),
            ('login class', 'button[class*="login" i]'),
            ('first button in form', 'form button'),
        ])
        if el:
            return el
        # Fallback: first visible button in bottom half of viewport
        return await self._find_button_by_position()

    async def _find_button_by_position(self) -> Optional[Any]:
        """Auto-discover a submit/login button by position."""
        try:
            buttons = await self.page.query_selector_all('button:visible')
            if not buttons:
                return None
            vp = await self.page.viewport_size()
            vph = vp.get("height", 900) if vp else 900
            mid_y = vph * 0.4
            best = None
            best_y = 0
            for b in buttons:
                box = await b.bounding_box()
                if not box:
                    continue
                # Prefer buttons in bottom half, relatively wide (not tiny icons)
                if box["y"] > mid_y and box["width"] > 60:
                    if box["y"] > best_y:
                        best_y = box["y"]
                        best = b
            if best:
                self.log.append(("login_button_auto", f"position-based (y={best_y:.0f}, w>60)", True))
            return best
        except Exception:
            return None

    async def find_textarea(self) -> Optional[Any]:
        """Find chat message input."""
        # Strategy 1: Predefined selectors (fast)
        el = await self.find("textarea", [
            ('tag textarea', 'textarea'),
            ('role textbox', 'textarea[role="textbox"]'),
            ('placeholder message', 'textarea[placeholder*="message" i]'),
            ('placeholder send', 'textarea[placeholder*="send" i]'),
            ('class input', '.ds-input__textarea'),
        ])
        if el:
            return el
        # Strategy 2: Position-based auto-discovery
        return await self._find_by_position("textarea")

    async def _find_by_position(self, tag: str) -> Optional[Any]:
        """Auto-discover element by position (bottom of page)."""
        try:
            candidates = await self.page.query_selector_all(f"{tag}:visible")
            if not candidates:
                return None
            best = None
            best_y = 0
            for c in candidates:
                box = await c.bounding_box()
                if box and box["y"] > best_y:
                    best_y = box["y"]
                    best = c
            if best:
                self.log.append((f"{tag}_auto", f"position-based (y={best_y:.0f})", True))
            return best
        except Exception:
            return None

    async def _find_link_by_position(self) -> Optional[Any]:
        """Auto-discover a New Chat link in left sidebar area."""
        try:
            links = await self.page.query_selector_all('a:visible')
            if not links:
                return None
            vp = await self.page.viewport_size()
            vpw = vp.get("width", 1280) if vp else 1280
            mid_x = vpw * 0.35
            best = None
            best_y = float('inf')
            for a in links:
                box = await a.bounding_box()
                if not box:
                    continue
                # Sidebar links are in left 35% of screen, near top
                if box["x"] < mid_x and box["y"] < 200 and box["width"] > 40:
                    if box["y"] < best_y:
                        best_y = box["y"]
                        best = a
            if best:
                self.log.append(("new_chat_auto", f"position-based (x<{mid_x:.0f}, y={best_y:.0f})", True))
            return best
        except Exception:
            return None

    async def find_new_chat_button(self) -> Optional[Any]:
        """Find New Chat button/link."""
        el = await self.find("new_chat", [
            ('href /', 'a[href="/"]'),
            ('text New Chat', 'a:has-text("New Chat")'),
            ('text New chat', 'a:has-text("New chat")'),
            ('text 开启新对话', 'a:has-text("开启新对话")'),
            ('sidebar link', 'aside a, nav a'),
        ])
        if el:
            return el
        # Fallback: first visible link in left sidebar area
        return await self._find_link_by_position()

    async def find_mode_indicator(self) -> Optional[str]:
        """Find current mode text (Instant/Expert). Returns text, not element."""
        for strategy_name, selector in [
            ('icon sibling', '.ds-icon + span'),
            ('model badge', '[class*="model"] span, [class*="mode"] span'),
            ('text Expert', 'text=/Expert/'),
            ('text Instant', 'text=/Instant/'),
            ('button Expert', 'button:has-text("Expert")'),
            ('button Instant', 'button:has-text("Instant")'),
        ]:
            try:
                el = await self.page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text in ("Instant", "Expert", "快速", "专家"):
                        self.log.append(("mode_indicator", strategy_name, True))
                        return text
            except Exception:
                continue
        return None

    async def find_toggle(self, label_text: str) -> Optional[Any]:
        """Find a toggle button by its label."""
        strategies = [
            (f'text {label_text}', f'.ds-toggle-button:has-text("{label_text}")'),
            (f'contains {label_text}', f'*:has-text("{label_text}"):has(.ds-toggle-button)'),
            (f'xpath {label_text}', f'xpath=//*[contains(text(), "{label_text}")]/ancestor::button'),
        ]
        return await self.find(f"toggle_{label_text}", strategies)

    def report(self) -> str:
        """Generate a debug report of what worked/failed."""
        lines = ["\n--- SmartFinder Report ---"]
        for name, strategy, success in self.log:
            status = "OK" if success else "FAIL"
            lines.append(f"  [{status}] {name}: {strategy}")
        lines.append("---")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
class DeepSeekCLI:
    BASE_URL = "https://chat.deepseek.com"

    # Legacy selectors kept for fast-path checks
    SEL_SOCIAL_BTNS = ".ds-sign-in-form__social-button"
    SEL_LOGIN_BTN = ".ds-basic-button--primary"
    SEL_NEW_CHAT = 'a[href="/"]'
    SEL_TEXTAREA = "textarea"
    SEL_TOGGLE = ".ds-toggle-button"
    SEL_TOGGLE_SELECTED = "ds-toggle-button--selected"

    def __init__(self, cfg: Cfg):
        self.cfg = cfg
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.chat_id: str = ""
        self._pw = None
        self.finder: Optional[SmartFinder] = None

    # -- Safe navigation --
    async def _safe_goto(self, url: str, timeout: int = 30_000) -> bool:
        try:
            await self.page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass
            return True
        except PWTimeout:
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
        self.finder = SmartFinder(self.page)

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
    # Debug helpers
    # ------------------------------------------------------------------
    async def _diagnose(self, reason: str):
        """Save screenshot + DOM when something breaks."""
        ts = int(time.time())
        ss = os.path.join(SESSION_DIR, f"debug-{ts}.png")
        dom = os.path.join(SESSION_DIR, f"debug-{ts}.html")
        try:
            await self.page.screenshot(path=ss, full_page=True)
            html = await self.page.content()
            with open(dom, "w", encoding="utf-8") as f:
                f.write(html[:100000])
            pprint(f"[DIAGNOSE] {reason}")
            pprint(f"[DIAGNOSE] Screenshot: {ss}")
            pprint(f"[DIAGNOSE] DOM dump: {dom}")
            if self.cfg.debug:
                pprint(self.finder.report())
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
            ta = await self.finder.find_textarea()
            return ta is not None
        except Exception:
            return False

    async def _do_login(self) -> bool:
        pprint("[LOGIN] Opening sign-in page...")
        if not await self._safe_goto(f"{self.BASE_URL}/sign_in", timeout=30_000):
            pprint("[ERROR] Sign-in page load timed out")
            return False
        await asyncio.sleep(5)

        # Find password input using SmartFinder
        pw = await self.finder.find_password_input()

        if pw is None:
            for btn in await self.page.query_selector_all(self.SEL_SOCIAL_BTNS):
                try:
                    await btn.evaluate("el => el.click()")
                except Exception:
                    pass
                await asyncio.sleep(2)
                pw = await self.finder.find_password_input()
                if pw:
                    break

        if pw is None:
            await self._diagnose("Password input not found after toggling")
            pprint("[ERROR] Password input not found. Run with --debug for details.")
            return False

        email = await self.finder.find_email_input()
        if email is None:
            await self._diagnose("Email input not found")
            pprint("[ERROR] Email input not found")
            return False

        await email.fill(self.cfg.email)
        await pw.fill(self.cfg.password)

        btn = await self.finder.find_login_button()
        if btn is None:
            await self._diagnose("Login button not found")
            pprint("[ERROR] Login button not found")
            return False
        try:
            await btn.click()
        except Exception:
            await btn.evaluate("el => el.click()")

        pprint("[LOGIN] Authenticating...")
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
        """Switch between Instant and Expert using text-based button detection."""
        try:
            buttons = await self.page.query_selector_all('button')
            instant_btn = None
            expert_btn = None
            for btn in buttons:
                text = (await btn.inner_text()).strip()
                if text in ("Instant", "快速"):
                    instant_btn = btn
                elif text in ("Expert", "专家"):
                    expert_btn = btn
            if not instant_btn or not expert_btn:
                return

            current = await self.finder.find_mode_indicator()
            if mode == "expert" and current in ("Instant", "快速"):
                await expert_btn.click()
                await asyncio.sleep(0.5)
                pprint("[MODE] Expert")
            elif mode == "default" and current in ("Expert", "专家"):
                await instant_btn.click()
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
            # Mode from indicator badge (no obfuscated classes)
            mode = "unknown"
            badge = await self.finder.find_mode_indicator()
            if badge in ("Instant", "快速"):
                mode = "default"
            elif badge in ("Expert", "专家"):
                mode = "expert"

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
            el = await self.finder.find_new_chat_button()
            if el:
                await el.click()
            else:
                await self._safe_goto(self.BASE_URL)
            try:
                await self.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
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
        await asyncio.sleep(3)
        # DeepSeek uses client-side routing — URL may not contain CID
        # Check if page actually loaded by finding textarea
        try:
            ta = await self.finder.find_textarea()
            if ta is not None:
                self.chat_id = cid
                return True
        except Exception:
            pass
        # Fallback: check if we're still on sign_in
        if "sign_in" in self.page.url:
            return False
        # If URL is chat domain and textarea exists, we're good
        if "chat.deepseek.com" in self.page.url:
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
            ta = await self.finder.find_textarea()
            if ta is None:
                await self._diagnose("Textarea not found — UI may have changed")
                return {"ok": False, "error": "Textarea not found. Run with --debug for details."}
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
        start = time.time()
        last_resp = ""
        last_count = 0
        stable = 0

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
                    ft = final.get("texts", [])
                    fn = ft[initial_count:] if len(ft) > initial_count else ft
                    return {
                        "ok": True,
                        "response": self._pick_best_block(fn, final.get("thinking", "")) or resp,
                        "thinking": final.get("thinking", thinking),
                    }
                except Exception:
                    return {"ok": True, "response": resp, "thinking": thinking}

        return {"ok": False, "error": "Timeout", "response": last_resp}

    def _pick_best_block(self, texts: List[str], thinking: str = "") -> str:
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
    p.add_argument("--debug", action="store_true",
                    help="Debug mode: save screenshots + DOM dumps + strategy reports")
    return p


async def _run(args) -> int:
    cfg = Cfg(
        email=args.email,
        password=args.password,
        headless=not args.show_browser,
        model_type="expert" if args.expert else "default",
        thinking=args.think,
        search=args.search,
        debug=args.debug,
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

        # Debug mode: dump current page state and exit
        if cfg.debug:
            await cli._diagnose("Debug mode requested")
            pprint("[DEBUG] Inspect the saved files, then update selectors.")
            return 0

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
