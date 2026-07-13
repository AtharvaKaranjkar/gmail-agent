"""
Gmail Login — Handle the full login flow.

Steps:
  1. Launch persistent browser (reuse session if cookies exist)
  2. Navigate to Gmail
  3. If already logged in → done
  4. If login page → type email → type password → handle 2FA if needed
  5. Wait until inbox is fully loaded

Uses the agent loop for flexibility — the LLM handles
unexpected UI states (security prompts, "choose account", etc.)
"""

import asyncio
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import config
from core.agent_loop import run_agent, AgentResult


# ── Browser launcher ─────────────────────────────────────────────────

async def launch_browser() -> tuple[Browser, BrowserContext, Page]:
    """
    Launch a persistent Chromium browser.
    Reuses browser_data dir so cookies survive across runs.
    """
    pw = await async_playwright().start()

    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=str(config.BROWSER_DATA_DIR),
        headless=config.HEADLESS,
        viewport={"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT},
        accept_downloads=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )

    # launch_persistent_context returns a BrowserContext directly
    # Get existing page or create new one
    if browser.pages:
        page = browser.pages[0]
    else:
        page = await browser.new_page()

    # Set download path
    await page.context.set_default_timeout(15000)

    return pw, browser, page


# ── Login flow ───────────────────────────────────────────────────────

async def login_to_gmail(page: Page) -> bool:
    """
    Navigate to Gmail and ensure we're logged in.

    Returns True if inbox is ready, False on failure.
    """
    print("\n📧 Navigating to Gmail...")

    # Go to Gmail
    await page.goto("https://mail.google.com", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Check if we're already logged in (cookies from previous run)
    current_url = page.url
    if _is_inbox(current_url):
        print("   ✓ Already logged in (session restored)")
        await _wait_for_inbox_ready(page)
        return True

    print("   Login required. Starting login flow...")

    # Build a detailed task for the agent
    task = f"""Login to Gmail with these credentials:
Email: {config.GMAIL_EMAIL}
Password: {config.GMAIL_PASSWORD}

STEP-BY-STEP INSTRUCTIONS:
1. If you see a Google sign-in page with an email input field, type the email address and click Next.
2. If you see a "Choose an account" page, click on the account matching the email, or click "Use another account".
3. If you see a password field, type the password and click Next.
4. If you see a 2FA/OTP/verification prompt (phone verification, authenticator code, etc.), use ask_human to get the code from the user.
5. If you see any "trust this browser" or "not now" prompt, click "Not now" or skip it.
6. Once you see the Gmail inbox (list of emails, compose button visible), call done.

IMPORTANT:
- If anything unexpected appears, use ask_human to ask the user what to do.
- Do NOT click on any emails — just get to the inbox and stop.
- The inbox is ready when you see email rows/threads listed on the page.
"""

    result: AgentResult = await run_agent(page, task, max_steps=25)

    if result.success:
        print("   ✓ Login successful")
        await _wait_for_inbox_ready(page)
        return True
    else:
        print("   ✗ Login failed")
        # One more check — maybe the agent didn't call done but we're in the inbox
        if _is_inbox(page.url):
            print("   ✓ Actually in inbox (agent didn't signal done)")
            await _wait_for_inbox_ready(page)
            return True
        return False


# ── Helpers ──────────────────────────────────────────────────────────

def _is_inbox(url: str) -> bool:
    """Check if the URL looks like Gmail inbox."""
    inbox_patterns = [
        "mail.google.com/mail",
        "#inbox",
        "#all",
        "/mail/u/0/",
    ]
    return any(p in url for p in inbox_patterns)


async def _wait_for_inbox_ready(page: Page, timeout: int = 15):
    """
    Wait until Gmail's inbox is fully loaded.
    Looks for telltale signs: compose button, email rows, etc.
    """
    print("   Waiting for inbox to fully load...")

    checks = [
        # Compose button
        'div[gh="cm"]',
        'div[role="button"][aria-label*="Compose"]',
        # Email rows
        'tr.zA',
        'div[role="main"] table tr',
        # Inbox label
        'a[aria-label*="Inbox"]',
    ]

    for _ in range(timeout):
        for selector in checks:
            try:
                el = await page.query_selector(selector)
                if el:
                    print(f"   ✓ Inbox loaded (found: {selector[:40]})")
                    await asyncio.sleep(1)  # extra settle time
                    return
            except Exception:
                pass
        await asyncio.sleep(1)

    print("   ⚠ Inbox load timeout — proceeding anyway")
