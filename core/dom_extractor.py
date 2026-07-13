"""
DOM Extractor — Python wrapper around the JS injection.

Responsibilities:
  1. Inject extract_dom.js into the current page
  2. Capture a screenshot (resized for LLM)
  3. Package everything into a BrowserState dataclass
  4. Provide helper to highlight an element by index (visual feedback)
"""

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path
from io import BytesIO

from playwright.async_api import Page
from PIL import Image

import config


# ── Load the JS once at module level ─────────────────────────────────
_JS_PATH = Path(__file__).parent / "extract_dom.js"
_EXTRACT_JS = _JS_PATH.read_text(encoding="utf-8")


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class ElementInfo:
    """A single interactive element extracted from the DOM."""
    index: int
    xpath: str
    css_selector: str
    tag: str
    text: str
    attributes: dict = field(default_factory=dict)


@dataclass
class ViewportInfo:
    url: str = ""
    title: str = ""
    scroll_top: int = 0
    scroll_height: int = 0
    viewport_height: int = 0
    viewport_width: int = 0


@dataclass
class BrowserState:
    """Complete snapshot of the browser state for one agent step."""
    viewport: ViewportInfo
    element_count: int
    selector_map: dict[int, ElementInfo]   # index -> ElementInfo
    llm_text: str                          # text representation for LLM
    screenshot_b64: str | None = None      # base64-encoded PNG (resized)


# ── Core extraction function ─────────────────────────────────────────

async def extract_state(page: Page, take_screenshot: bool = True) -> BrowserState:
    """
    Extract the current browser state:
      - Inject JS to walk DOM and get interactive elements
      - Optionally take a screenshot
      - Return a BrowserState ready for the LLM

    Args:
        page: The Playwright page object
        take_screenshot: Whether to capture a screenshot (requires vision LLM)
    """
    # Wait briefly for any pending renders
    await asyncio.sleep(0.3)

    # Inject and execute the DOM extraction JS
    raw = await page.evaluate(_EXTRACT_JS)

    # Parse viewport info
    vp_data = raw.get("viewport", {})
    viewport = ViewportInfo(
        url=vp_data.get("url", ""),
        title=vp_data.get("title", ""),
        scroll_top=vp_data.get("scrollTop", 0),
        scroll_height=vp_data.get("scrollHeight", 0),
        viewport_height=vp_data.get("viewportHeight", 0),
        viewport_width=vp_data.get("viewportWidth", 0),
    )

    # Parse selector map
    selector_map: dict[int, ElementInfo] = {}
    raw_map = raw.get("selectorMap", {})
    for idx_str, info in raw_map.items():
        idx = int(idx_str)
        selector_map[idx] = ElementInfo(
            index=idx,
            xpath=info.get("xpath", ""),
            css_selector=info.get("cssSelector", ""),
            tag=info.get("tag", ""),
            text=info.get("text", ""),
            attributes=info.get("attributes", {}),
        )

    # Screenshot
    screenshot_b64 = None
    if take_screenshot:
        screenshot_b64 = await _capture_screenshot(page)

    return BrowserState(
        viewport=viewport,
        element_count=raw.get("elementCount", 0),
        selector_map=selector_map,
        llm_text=raw.get("llmText", ""),
        screenshot_b64=screenshot_b64,
    )


# ── Screenshot helper ────────────────────────────────────────────────

async def _capture_screenshot(page: Page, max_width: int = 1024) -> str:
    """Take a screenshot, resize for LLM, return base64 string."""
    raw_bytes = await page.screenshot(type="png", full_page=False)

    # Resize to save tokens
    img = Image.open(BytesIO(raw_bytes))
    if img.width > max_width:
        ratio = max_width / img.width
        new_size = (max_width, int(img.height * ratio))
        img = img.resize(new_size, Image.LANCZOS)

    buffer = BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ── Element interaction helpers ──────────────────────────────────────

async def highlight_element(page: Page, index: int, selector_map: dict[int, ElementInfo]):
    """
    Briefly highlight an element on the page (visual feedback).
    Used before clicking/typing so the user can see what the agent is doing.
    """
    if index not in selector_map:
        return

    el = selector_map[index]
    js = f"""
    (() => {{
        try {{
            const el = document.evaluate(
                `{el.xpath}`, document, null,
                XPathResult.FIRST_ORDERED_NODE_TYPE, null
            ).singleNodeValue;
            if (!el) return;
            const orig = el.style.outline;
            el.style.outline = '3px solid red';
            el.style.outlineOffset = '2px';
            setTimeout(() => {{
                el.style.outline = orig;
                el.style.outlineOffset = '';
            }}, 1500);
        }} catch(e) {{}}
    }})()
    """
    await page.evaluate(js)


async def get_element_handle(page: Page, element: ElementInfo):
    """
    Get a Playwright ElementHandle for an extracted element.
    Tries XPath first, falls back to CSS selector.
    """
    try:
        handle = await page.wait_for_selector(
            f"xpath={element.xpath}", timeout=3000
        )
        if handle:
            return handle
    except Exception:
        pass

    try:
        handle = await page.wait_for_selector(
            element.css_selector, timeout=3000
        )
        if handle:
            return handle
    except Exception:
        pass

    return None
