"""
Thread Extractor — Extract full conversation and attachments from a Gmail thread.

Flow per thread:
  1. Open the thread (already navigated by scanner)
  2. Expand all collapsed messages
  3. Extract each message: sender, date, body text
  4. Find and download attachments (filtered by allowed extensions)
  5. Return structured conversation data
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from playwright.async_api import Page

import config


# ── Data structures ──────────────────────────────────────────────────

@dataclass
class MessageData:
    """A single message within a thread."""
    index: int
    sender: str
    email: str
    date: str
    body: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class ThreadData:
    """Complete extracted data for a thread."""
    thread_id: str
    subject: str
    messages: list[MessageData]
    attachment_paths: list[Path]
    msg_count: int

    @property
    def formatted_conversation(self) -> str:
        """Build the conversation.txt content."""
        date_range = ""
        if self.messages:
            first = self.messages[0].date
            last = self.messages[-1].date
            date_range = f"{first} to {last}"

        lines = [
            "=" * 60,
            f"Thread: {self.subject}",
            f"Messages: {len(self.messages)}",
            f"Date range: {date_range}",
            "=" * 60,
            "",
        ]

        for msg in self.messages:
            lines.append(f"[{msg.index}] From: {msg.sender} <{msg.email}>")
            lines.append(f"    Date: {msg.date}")
            lines.append(f"    {'─' * 40}")
            lines.append(f"    {msg.body}")
            if msg.attachments:
                att_list = ", ".join(msg.attachments)
                lines.append("")
                lines.append(f"    📎 Attachments: {att_list}")
            lines.append("")
            lines.append("")

        return "\n".join(lines)


# ── JS: Expand all collapsed messages ────────────────────────────────

EXPAND_ALL_JS = """
(async () => {
    let expanded = 0;

    // Method 1: Click the "show N more" expander
    const expanders = document.querySelectorAll(
        'span.adx, span[role="link"][class*="adx"], div.gE'
    );
    for (const exp of expanders) {
        const text = (exp.textContent || '').trim();
        if (text.match(/\\d+\\s*(more|message)/i) || text === '…') {
            exp.click();
            expanded++;
            await new Promise(r => setTimeout(r, 500));
        }
    }

    // Method 2: Click all collapsed message headers
    const collapsed = document.querySelectorAll(
        'div.kv, div.kx, tr.zA[class*="collapsed"], div[role="listitem"][data-collapsed="true"]'
    );
    for (const el of collapsed) {
        el.click();
        expanded++;
        await new Promise(r => setTimeout(r, 300));
    }

    // Method 3: Expand quoted/trimmed content
    const quotedExpanders = document.querySelectorAll(
        'div.ajR, span[class*="ajR"], div[aria-label="Show trimmed content"]'
    );
    for (const qe of quotedExpanders) {
        qe.click();
        expanded++;
        await new Promise(r => setTimeout(r, 200));
    }

    await new Promise(r => setTimeout(r, 1000));
    return { expanded: expanded };
})()
"""


# ── JS: Extract all messages from an open thread ────────────────────

EXTRACT_MESSAGES_JS = """
(() => {
    const messages = [];

    const msgContainers = document.querySelectorAll(
        'div.gs, div[class*="adn"], div[data-message-id], table.cf'
    );

    const containers = msgContainers.length > 0
        ? msgContainers
        : document.querySelectorAll('div[role="listitem"], div.nH div.nH');

    let idx = 0;
    for (const container of containers) {
        const msg = { index: idx + 1, sender: '', email: '', date: '', body: '', attachments: [] };

        // Sender
        const senderEl = container.querySelector('span.gD, span[email], h3.iw span, span.go');
        if (senderEl) {
            msg.sender = (senderEl.textContent || '').trim();
            msg.email = senderEl.getAttribute('email')
                || senderEl.getAttribute('data-hovercard-id') || '';
        }
        if (!msg.sender) {
            const headerEl = container.querySelector('td.gF, div.gE, div[class*="header"]');
            if (headerEl) msg.sender = (headerEl.textContent || '').trim().substring(0, 80);
        }

        // Date
        const dateEl = container.querySelector('span.g3, span[title][alt], td.gH span, abbr, span[data-tooltip]');
        if (dateEl) {
            msg.date = dateEl.getAttribute('title')
                || dateEl.getAttribute('alt')
                || dateEl.getAttribute('data-tooltip')
                || (dateEl.textContent || '').trim();
        }

        // Body
        const bodyEl = container.querySelector('div.a3s, div[class*="a3s"], div[dir="ltr"], div.gmail_default');
        if (bodyEl) {
            msg.body = (bodyEl.innerText || '').trim().substring(0, 5000);
        }
        if (!msg.body || msg.body.length < 5) {
            const textEls = container.querySelectorAll('div[dir], p, span.im');
            const texts = [];
            for (const te of textEls) {
                const t = (te.innerText || '').trim();
                if (t.length > 10) texts.push(t);
            }
            if (texts.length) msg.body = texts.join('\\n\\n').substring(0, 5000);
        }

        // Attachments
        const attEls = container.querySelectorAll(
            'div.aZo a.aQy, span.aZo, div[class*="aZo"], a[download], div.aV3'
        );
        for (const att of attEls) {
            const name = att.getAttribute('download')
                || att.getAttribute('title')
                || att.getAttribute('aria-label')
                || (att.textContent || '').trim();
            if (name && name.length > 1 && name.length < 200) {
                msg.attachments.push(name);
            }
        }

        if (msg.body || msg.sender || msg.attachments.length > 0) {
            idx++;
            msg.index = idx;
            messages.push(msg);
        }
    }

    let subject = '';
    const subjectEl = document.querySelector('h2.hP, div.ha h2, h2[data-thread-perm-id]');
    if (subjectEl) subject = (subjectEl.textContent || '').trim();

    return { subject, messages, count: messages.length };
})()
"""


# ── JS: Find downloadable attachments ───────────────────────────────

FIND_ATTACHMENTS_JS = """
(() => {
    const attachments = [];
    const attElements = document.querySelectorAll(
        'div.aQH a[download], div.aZo a.aQy, a[aria-label*="Download"], div.aV3 span[download]'
    );

    for (let i = 0; i < attElements.length; i++) {
        const el = attElements[i];
        const name = el.getAttribute('download')
            || el.getAttribute('title')
            || el.getAttribute('aria-label')
            || (el.textContent || '').trim();

        const ext = name.includes('.') ? '.' + name.split('.').pop().toLowerCase() : '';

        attachments.push({
            index: i,
            name: name,
            ext: ext,
            selector: el.getAttribute('download')
                ? 'a[download="' + el.getAttribute('download').replace(/"/g, '\\\\"') + '"]'
                : ''
        });
    }
    return attachments;
})()
"""


# ── Main extraction function ────────────────────────────────────────

async def extract_thread(page: Page, thread_id: str, subject: str) -> ThreadData:
    """
    Extract all conversation data and attachments from the currently open thread.
    Assumes the thread page is already loaded.
    """
    print(f"\n   📨 Extracting: {subject[:60]}...")

    # Step 1: Expand all collapsed messages
    print(f"      Expanding messages...")
    try:
        result = await page.evaluate(EXPAND_ALL_JS)
        count = result.get("expanded", 0) if result else 0
        if count > 0:
            print(f"      Expanded {count} sections")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"      ⚠ Expand attempt: {e}")

    # Second pass expansion
    try:
        await page.evaluate(EXPAND_ALL_JS)
        await asyncio.sleep(0.5)
    except Exception:
        pass

    # Step 2: Extract messages
    print(f"      Extracting messages...")
    try:
        raw = await page.evaluate(EXTRACT_MESSAGES_JS)
    except Exception as e:
        print(f"      ✗ Extraction failed: {e}")
        return ThreadData(thread_id=thread_id, subject=subject,
                          messages=[], attachment_paths=[], msg_count=0)

    extracted_subject = raw.get("subject", subject) or subject
    raw_messages = raw.get("messages", [])
    print(f"      Found {len(raw_messages)} messages")

    messages = []
    for rm in raw_messages:
        messages.append(MessageData(
            index=rm.get("index", 0),
            sender=rm.get("sender", "Unknown"),
            email=rm.get("email", ""),
            date=rm.get("date", ""),
            body=rm.get("body", ""),
            attachments=rm.get("attachments", []),
        ))

    # Step 3: Download attachments
    attachment_paths = await _download_attachments(page, thread_id)

    return ThreadData(
        thread_id=thread_id,
        subject=extracted_subject,
        messages=messages,
        attachment_paths=attachment_paths,
        msg_count=len(messages),
    )


# ── Attachment downloader ────────────────────────────────────────────

async def _download_attachments(page: Page, thread_id: str) -> list[Path]:
    """Find and download all allowed attachments from the current thread."""
    print(f"      Scanning for attachments...")

    try:
        raw_attachments = await page.evaluate(FIND_ATTACHMENTS_JS)
    except Exception as e:
        print(f"      ⚠ Attachment scan failed: {e}")
        return []

    if not raw_attachments:
        print(f"      No attachments found")
        return []

    # Filter by allowed extensions
    allowed = []
    for att in raw_attachments:
        ext = att.get("ext", "").lower()
        name = att.get("name", "")
        if ext in config.ALLOWED_EXTENSIONS:
            allowed.append(att)
        elif name:
            print(f"      Skipped: {name} ({ext})")

    if not allowed:
        print(f"      No attachments with allowed extensions")
        return []

    print(f"      Downloading {len(allowed)} attachments...")
    downloaded: list[Path] = []

    for att in allowed:
        name = att.get("name", "attachment")
        selector = att.get("selector", "")

        try:
            el = None

            # Try by selector
            if selector:
                el = await page.query_selector(selector)

            # Fallback: by download attribute
            if not el and name:
                safe = name.replace('"', '\\"')
                el = await page.query_selector(f'a[download="{safe}"]')

            # Fallback: by aria-label
            if not el and name:
                short = name[:30].replace('"', '\\"')
                el = await page.query_selector(
                    f'a[aria-label*="Download"][aria-label*="{short}"]'
                )

            if not el:
                print(f"      ⚠ Could not locate: {name}")
                continue

            # Download
            async with page.expect_download(timeout=30000) as dl_info:
                await el.click()

            download = await dl_info.value
            filename = download.suggested_filename or name
            save_path = config.DOWNLOADS_DIR / f"{thread_id}_{filename}"
            await download.save_as(str(save_path))

            downloaded.append(save_path)
            print(f"      ✓ {filename}")

        except Exception as e:
            print(f"      ✗ {name}: {str(e)[:80]}")

        await asyncio.sleep(0.5)

    return downloaded