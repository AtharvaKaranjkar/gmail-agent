"""
Actions — Execute agent decisions on the browser.

Each action function takes the Playwright page + params,
performs the action, and returns a human-readable result string.
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import Page

import config
from core.dom_extractor import ElementInfo, highlight_element, get_element_handle


# ── Action result ────────────────────────────────────────────────────

class ActionResult:
    def __init__(self, success: bool, description: str, is_done: bool = False, extracted: str = ""):
        self.success = success
        self.description = description
        self.is_done = is_done
        self.extracted = extracted

    def __str__(self):
        status = "✓" if self.success else "✗"
        return f"[{status}] {self.description}"


# ── Action executor (dispatcher) ────────────────────────────────────

async def execute_action(
    page: Page,
    action: str,
    params: dict,
    selector_map: dict[int, ElementInfo],
) -> ActionResult:
    """
    Dispatch an action by name to the correct handler.
    Returns an ActionResult describing what happened.
    """
    handlers = {
        "click": _do_click,
        "type": _do_type,
        "scroll": _do_scroll,
        "wait": _do_wait,
        "go_back": _do_go_back,
        "download": _do_download,
        "ask_human": _do_ask_human,
        "done": _do_done,
        "extract": _do_extract,
    }

    handler = handlers.get(action)
    if not handler:
        return ActionResult(False, f"Unknown action: {action}")

    try:
        return await handler(page, params, selector_map)
    except Exception as e:
        return ActionResult(False, f"Action '{action}' failed: {str(e)[:200]}")


# ── Individual action handlers ──────────────────────────────────────

async def _do_click(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    index = params.get("index")
    if index is None:
        return ActionResult(False, "click: missing 'index' param")

    index = int(index)
    if index not in selector_map:
        return ActionResult(False, f"click: index {index} not found in selector map")

    element = selector_map[index]

    # Visual feedback
    await highlight_element(page, index, selector_map)
    await asyncio.sleep(0.2)

    # Get element handle and click
    handle = await get_element_handle(page, element)
    if not handle:
        return ActionResult(False, f"click: could not locate element {index} ({element.tag})")

    # Scroll into view first
    await handle.scroll_into_view_if_needed(timeout=3000)
    await asyncio.sleep(0.1)
    await handle.click(timeout=5000)
    await asyncio.sleep(config.WAIT_AFTER_ACTION)

    desc = f"click: [{index}] <{element.tag}> {element.text[:60]}"
    return ActionResult(True, desc)


async def _do_type(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    index = params.get("index")
    text = params.get("text", "")
    if index is None:
        return ActionResult(False, "type: missing 'index' param")

    index = int(index)
    if index not in selector_map:
        return ActionResult(False, f"type: index {index} not found in selector map")

    element = selector_map[index]

    await highlight_element(page, index, selector_map)

    handle = await get_element_handle(page, element)
    if not handle:
        return ActionResult(False, f"type: could not locate element {index}")

    await handle.scroll_into_view_if_needed(timeout=3000)

    # Clear existing content first, then type
    await handle.click(timeout=3000)
    await asyncio.sleep(0.1)
    await handle.fill(text, timeout=5000)
    await asyncio.sleep(config.WAIT_AFTER_ACTION)

    # Mask passwords in log
    display_text = "****" if "password" in element.attributes.get("type", "").lower() else text
    desc = f"type: [{index}] <{element.tag}> '{display_text}'"
    return ActionResult(True, desc)


async def _do_scroll(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    direction = params.get("direction", "down")
    amount = params.get("amount", 500)

    if direction == "down":
        await page.mouse.wheel(0, int(amount))
    elif direction == "up":
        await page.mouse.wheel(0, -int(amount))
    else:
        return ActionResult(False, f"scroll: unknown direction '{direction}'")

    await asyncio.sleep(config.WAIT_AFTER_ACTION)
    return ActionResult(True, f"scroll: {direction} {amount}px")


async def _do_wait(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    seconds = min(int(params.get("seconds", 2)), 10)  # cap at 10
    await asyncio.sleep(seconds)
    return ActionResult(True, f"wait: {seconds}s")


async def _do_go_back(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    await page.go_back(timeout=10000)
    await asyncio.sleep(config.WAIT_AFTER_ACTION)
    return ActionResult(True, "go_back: navigated back")


async def _do_download(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    index = params.get("index")
    if index is None:
        return ActionResult(False, "download: missing 'index' param")

    index = int(index)
    if index not in selector_map:
        return ActionResult(False, f"download: index {index} not found")

    element = selector_map[index]

    await highlight_element(page, index, selector_map)

    handle = await get_element_handle(page, element)
    if not handle:
        return ActionResult(False, f"download: could not locate element {index}")

    # Start waiting for download BEFORE clicking
    try:
        async with page.expect_download(timeout=30000) as download_info:
            await handle.click(timeout=5000)
        download = await download_info.value

        # Save to downloads dir
        filename = download.suggested_filename
        save_path = config.DOWNLOADS_DIR / filename
        await download.save_as(str(save_path))

        return ActionResult(True, f"download: saved '{filename}' to downloads", extracted=str(save_path))

    except Exception as e:
        # Might not trigger a download event — just click it
        await handle.click(timeout=5000)
        await asyncio.sleep(2)
        return ActionResult(True, f"download: clicked [{index}], download may have started (no event captured)")


async def _do_ask_human(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    question = params.get("question", "Agent needs your help. What should it do?")

    print("\n" + "=" * 50)
    print(f"  🤖 AGENT NEEDS INPUT")
    print(f"  {question}")
    print("=" * 50)

    # Run input in a thread to not block the event loop
    loop = asyncio.get_event_loop()
    answer = await loop.run_in_executor(None, lambda: input("  Your response: "))

    print("=" * 50 + "\n")

    # If the answer looks like an OTP / code, type it into the focused element
    if answer.strip().isdigit() or (len(answer.strip()) <= 10 and answer.strip()):
        # Try to find a focused input and type the answer
        try:
            await page.keyboard.type(answer.strip(), delay=50)
            await asyncio.sleep(0.5)
            return ActionResult(True, f"ask_human: typed user response into page", extracted=answer.strip())
        except Exception:
            pass

    return ActionResult(True, f"ask_human: user responded with '{answer.strip()}'", extracted=answer.strip())


async def _do_done(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    result = params.get("result", "Task completed")
    return ActionResult(True, f"done: {result}", is_done=True, extracted=result)


async def _do_extract(page: Page, params: dict, selector_map: dict[int, ElementInfo]) -> ActionResult:
    """Extract text content from the current page or a specific area."""
    query = params.get("query", "")

    # Get all visible text from the page body
    text = await page.evaluate("""
        () => {
            const body = document.body;
            if (!body) return '';
            // Get text content, collapse whitespace
            return body.innerText.substring(0, 10000);
        }
    """)

    return ActionResult(True, f"extract: got {len(text)} chars", extracted=text)
