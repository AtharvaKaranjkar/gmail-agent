"""
Inbox Scanner — Scan Gmail inbox and find qualifying threads.

Flow:
  1. Ensure we're on the inbox page
  2. Extract thread rows from current view (subject, msg count, thread ID)
  3. Paginate through all pages (click "Older →")
  4. Filter threads by message count threshold
  5. Check each against SQLite DB → new / updated / skip

Uses JS injection for data extraction + agent loop for pagination.
"""

import asyncio
import re
from dataclasses import dataclass
from playwright.async_api import Page

import config
from database import should_process


# ── Thread info ──────────────────────────────────────────────────────

@dataclass
class ThreadInfo:
    thread_id: str         # Gmail thread ID (from URL hash)
    subject: str           # Email subject line
    msg_count: int         # Number of messages in thread
    snippet: str           # Preview text
    status: str            # 'new' | 'updated' (skip is filtered out)
    row_index: int = -1    # Index of the row element (for clicking into it)


# ── JS to extract thread rows from inbox ─────────────────────────────

EXTRACT_THREADS_JS = """
(() => {
    const threads = [];

    // Gmail renders inbox rows as <tr> elements
    // Each row contains: checkbox, sender, subject, snippet, date
    // Message count appears as a small number in parentheses or a badge

    const rows = document.querySelectorAll('tr.zA, tr.zE');

    for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        const info = { index: i, subject: '', msgCount: 1, snippet: '', threadId: '' };

        // ── Extract thread ID from the row's link ──
        // Gmail thread links look like #inbox/18f1a2b3c4d5e6f7
        const links = row.querySelectorAll('a[href]');
        for (const link of links) {
            const href = link.getAttribute('href') || '';
            // Match patterns like #inbox/FMfcgz... or /mail/u/0/#inbox/FMfcgz...
            const match = href.match(/[#/]inbox\\/([A-Za-z0-9_-]+)/);
            if (match) {
                info.threadId = match[1];
                break;
            }
        }

        // Fallback: try data attributes
        if (!info.threadId) {
            const dataThreadId = row.getAttribute('data-thread-id')
                || row.querySelector('[data-thread-id]')?.getAttribute('data-thread-id')
                || '';
            if (dataThreadId) info.threadId = dataThreadId;
        }

        // If still no thread ID, generate one from row content hash
        if (!info.threadId) {
            const rowText = row.innerText || '';
            let hash = 0;
            for (let c = 0; c < rowText.length; c++) {
                hash = ((hash << 5) - hash) + rowText.charCodeAt(c);
                hash |= 0;
            }
            info.threadId = 'row_' + Math.abs(hash).toString(36);
        }

        // ── Extract subject and message count ──
        // Gmail shows count in parentheses like "Subject (5)"
        // or in a separate span element

        // Try to find subject spans
        const subjectSpans = row.querySelectorAll(
            'span.bog span, span.bqe, span[data-thread-id], td span.y2'
        );

        let rawSubject = '';
        for (const span of subjectSpans) {
            const text = (span.textContent || '').trim();
            if (text.length > 2 && text.length < 300) {
                rawSubject = text;
                break;
            }
        }

        // Fallback: get subject from the row's broader text
        if (!rawSubject) {
            // The subject is usually in the second or third cell
            const cells = row.querySelectorAll('td');
            for (const cell of cells) {
                const cellText = (cell.textContent || '').trim();
                // Skip very short (checkbox) or very long (full row) cells
                if (cellText.length > 5 && cellText.length < 500) {
                    rawSubject = cellText.substring(0, 200);
                    break;
                }
            }
        }

        // ── Parse message count from subject or separate element ──
        // Pattern 1: "Subject (7)" — count in parentheses
        const countMatch = rawSubject.match(/\\((\\d+)\\)\\s*$/);
        if (countMatch) {
            info.msgCount = parseInt(countMatch[1], 10);
            info.subject = rawSubject.replace(/\\s*\\(\\d+\\)\\s*$/, '').trim();
        } else {
            info.subject = rawSubject;
        }

        // Pattern 2: separate count badge element
        if (info.msgCount <= 1) {
            const badges = row.querySelectorAll('span.bx9, span[class*="count"]');
            for (const badge of badges) {
                const badgeText = (badge.textContent || '').trim();
                const num = parseInt(badgeText, 10);
                if (!isNaN(num) && num > 1) {
                    info.msgCount = num;
                    break;
                }
            }
        }

        // Pattern 3: look for any small standalone number that looks like a count
        if (info.msgCount <= 1) {
            const allSpans = row.querySelectorAll('span');
            for (const span of allSpans) {
                const text = (span.textContent || '').trim();
                // Must be just a number, 2-999, in a small element
                if (/^\\d+$/.test(text)) {
                    const num = parseInt(text, 10);
                    const rect = span.getBoundingClientRect();
                    if (num >= 2 && num < 1000 && rect.width < 40 && rect.height < 30) {
                        info.msgCount = num;
                        break;
                    }
                }
            }
        }

        // ── Snippet ──
        const snippetEl = row.querySelector('span.y2, span.bog + span');
        if (snippetEl) {
            info.snippet = (snippetEl.textContent || '').trim().substring(0, 150);
        }

        threads.push(info);
    }

    return threads;
})()
"""

# ── JS to check pagination state ────────────────────────────────────

CHECK_PAGINATION_JS = """
(() => {
    // Gmail pagination: "1-50 of 342" and Older/Newer buttons
    const result = { hasNext: false, rangeText: '' };

    // Look for the "Older" / next page button
    const buttons = document.querySelectorAll(
        'div[aria-label="Older"], div[aria-label="Next page"], span[aria-label="Older"]'
    );
    for (const btn of buttons) {
        // Check if it's not disabled
        const disabled = btn.getAttribute('aria-disabled');
        if (disabled !== 'true') {
            result.hasNext = true;
            break;
        }
    }

    // Get range text like "1-50 of 342"
    const rangeSpans = document.querySelectorAll('span.Dj span, span[class*="ts"]');
    for (const span of rangeSpans) {
        const text = (span.textContent || '').trim();
        if (text.match(/\\d+[–-]\\d+\\s+(of|out of)\\s+\\d+/i)) {
            result.rangeText = text;
            break;
        }
    }

    return result;
})()
"""


# ── Main scanner ─────────────────────────────────────────────────────

async def scan_inbox(page: Page) -> list[ThreadInfo]:
    """
    Scan the entire Gmail inbox and return threads that qualify for processing.

    Returns only threads where:
      - msg_count > THREAD_COUNT_THRESHOLD
      - DB status is 'new' or 'updated' (not 'skip')
    """
    print("\n🔍 Scanning inbox...")

    # Make sure we're on the inbox
    current_url = page.url
    if "#inbox" not in current_url and "/mail/" not in current_url:
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(3)

    all_threads: list[ThreadInfo] = []
    seen_ids: set[str] = set()
    page_num = 0

    while True:
        page_num += 1
        print(f"\n   Page {page_num}...")
        await asyncio.sleep(2)  # let the page settle

        # Extract threads from current view
        try:
            raw_threads = await page.evaluate(EXTRACT_THREADS_JS)
        except Exception as e:
            print(f"   ✗ Thread extraction failed: {e}")
            break

        if not raw_threads:
            print(f"   No threads found on this page.")
            break

        new_on_page = 0
        for t in raw_threads:
            tid = t.get("threadId", "")
            if tid in seen_ids:
                continue
            seen_ids.add(tid)

            msg_count = t.get("msgCount", 1)
            subject = t.get("subject", "(no subject)")

            # Filter by threshold
            if msg_count <= config.THREAD_COUNT_THRESHOLD:
                continue

            # Check against DB
            status = should_process(tid, msg_count)
            if status == "skip":
                continue

            thread_info = ThreadInfo(
                thread_id=tid,
                subject=subject,
                msg_count=msg_count,
                snippet=t.get("snippet", ""),
                status=status,
                row_index=t.get("index", -1),
            )
            all_threads.append(thread_info)
            new_on_page += 1

            label = "NEW" if status == "new" else "UPDATED"
            print(f"   [{label}] {subject[:50]} ({msg_count} msgs)")

        print(f"   Found {new_on_page} qualifying threads on page {page_num}")

        # Check if there's a next page
        try:
            pagination = await page.evaluate(CHECK_PAGINATION_JS)
        except Exception:
            pagination = {"hasNext": False, "rangeText": ""}

        if pagination.get("rangeText"):
            print(f"   Pagination: {pagination['rangeText']}")

        if not pagination.get("hasNext"):
            print(f"   No more pages.")
            break

        # Click "Older" to go to next page
        clicked = await _click_next_page(page)
        if not clicked:
            print(f"   Could not navigate to next page.")
            break

    # Summary
    new_count = sum(1 for t in all_threads if t.status == "new")
    updated_count = sum(1 for t in all_threads if t.status == "updated")
    print(f"\n   ✓ Scan complete: {len(all_threads)} threads to process")
    print(f"     New: {new_count} | Updated: {updated_count}")

    return all_threads


# ── Pagination helper ────────────────────────────────────────────────

async def _click_next_page(page: Page) -> bool:
    """Click the 'Older' / next page button in Gmail."""
    selectors = [
        'div[aria-label="Older"]',
        'div[aria-label="Next page"]',
        'span[aria-label="Older"]',
    ]

    for sel in selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                disabled = await btn.get_attribute("aria-disabled")
                if disabled == "true":
                    continue
                await btn.click()
                await asyncio.sleep(2)
                return True
        except Exception:
            continue

    return False


# ── Navigate to a specific thread ────────────────────────────────────

async def open_thread(page: Page, thread: ThreadInfo) -> bool:
    """
    Navigate into a specific email thread from the inbox.
    Returns True if the thread opened successfully.
    """
    # Try direct URL navigation first
    thread_url = f"https://mail.google.com/mail/u/0/#inbox/{thread.thread_id}"
    try:
        await page.goto(thread_url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # Verify we're in a thread view (not still in inbox list)
        url = page.url
        if thread.thread_id in url:
            print(f"   ✓ Opened thread: {thread.subject[:50]}")
            return True
    except Exception as e:
        print(f"   ⚠ Direct navigation failed: {e}")

    # Fallback: navigate to inbox and try to find/click the thread
    await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Search for the thread by subject
    print(f"   Searching for thread by subject...")
    try:
        # Use Gmail search
        search_btn = await page.query_selector('button[aria-label="Search mail"]')
        if search_btn:
            await search_btn.click()
            await asyncio.sleep(0.5)

        search_input = await page.query_selector('input[aria-label="Search mail"]')
        if search_input:
            # Search by subject (sanitize quotes)
            clean_subject = thread.subject.replace('"', '').replace("'", "")[:60]
            await search_input.fill(f'subject:"{clean_subject}"')
            await page.keyboard.press("Enter")
            await asyncio.sleep(3)

            # Click the first result
            first_row = await page.query_selector("tr.zA, tr.zE")
            if first_row:
                await first_row.click()
                await asyncio.sleep(2)
                print(f"   ✓ Found and opened thread via search")
                return True
    except Exception as e:
        print(f"   ✗ Search fallback failed: {e}")

    return False


async def go_back_to_inbox(page: Page):
    """Navigate back to the inbox from a thread view."""
    try:
        await page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded")
        await asyncio.sleep(2)
    except Exception:
        await page.go_back()
        await asyncio.sleep(2)
