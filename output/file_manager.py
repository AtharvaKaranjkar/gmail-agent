"""
File Manager — Save extracted data to the target/ folder structure.

Creates:
  target/
    email_001_subject_snippet/
      conversation.txt
      summary.json
      attachments/
        file1.pdf
        file2.xlsx
    index.json
"""

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config
from database import upsert_thread, delete_thread_folder, get_all_threads
from gmail.extractor import ThreadData
from core.llm_client import SummaryResult


# ── Folder naming ────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 50) -> str:
    """Convert a subject line into a safe folder name."""
    # Lowercase
    slug = text.lower().strip()
    # Replace common separators with underscore
    slug = re.sub(r'[\s\-:;/\\]+', '_', slug)
    # Remove non-alphanumeric (keep underscores)
    slug = re.sub(r'[^a-z0-9_]', '', slug)
    # Collapse multiple underscores
    slug = re.sub(r'_+', '_', slug)
    # Trim
    slug = slug.strip('_')[:max_len]
    return slug or "no_subject"


def _make_folder_name(index: int, subject: str) -> str:
    """Generate folder name like email_001_nitin_spinners_po."""
    slug = _slugify(subject)
    return f"email_{index:03d}_{slug}"


# ── Save a single thread ────────────────────────────────────────────

def save_thread(
    thread_data: ThreadData,
    summary: SummaryResult,
    folder_index: int,
) -> str:
    """
    Save all data for a single thread to disk.

    Args:
        thread_data: Extracted conversation + attachments
        summary: AI-generated summary and categorization
        folder_index: Sequential number for folder naming

    Returns:
        folder_name (for DB storage)
    """
    folder_name = _make_folder_name(folder_index, thread_data.subject)
    folder_path = config.TARGET_DIR / folder_name

    # If re-processing, wipe old folder first
    if folder_path.exists():
        shutil.rmtree(folder_path)

    # Create folder structure
    folder_path.mkdir(parents=True, exist_ok=True)
    attachments_dir = folder_path / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    # ── Save conversation.txt ────────────────────────────────────
    convo_path = folder_path / "conversation.txt"
    convo_path.write_text(thread_data.formatted_conversation, encoding="utf-8")

    # ── Save summary.json ────────────────────────────────────────
    summary_data = {
        "thread_id": thread_data.thread_id,
        "thread_subject": thread_data.subject,
        "message_count": thread_data.msg_count,
        "date_range": _get_date_range(thread_data),
        "parties": _get_parties(thread_data),
        "summary": summary.summary,
        "category": summary.category,
        "urgency": summary.urgency,
        "key_entities": summary.key_entities,
        "attachments": [
            {"filename": p.name, "original_path": str(p)}
            for p in thread_data.attachment_paths
        ],
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    summary_path = folder_path / "summary.json"
    summary_path.write_text(
        json.dumps(summary_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Move attachments ─────────────────────────────────────────
    for src_path in thread_data.attachment_paths:
        if src_path.exists():
            # Strip thread_id prefix from filename for cleaner names
            clean_name = src_path.name
            tid_prefix = f"{thread_data.thread_id}_"
            if clean_name.startswith(tid_prefix):
                clean_name = clean_name[len(tid_prefix):]

            dest_path = attachments_dir / clean_name
            # Handle duplicates
            counter = 1
            while dest_path.exists():
                stem = dest_path.stem
                suffix = dest_path.suffix
                dest_path = attachments_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            shutil.move(str(src_path), str(dest_path))

    # Remove attachments dir if empty
    if not any(attachments_dir.iterdir()):
        attachments_dir.rmdir()

    # ── Update database ──────────────────────────────────────────
    upsert_thread(
        thread_id=thread_data.thread_id,
        subject=thread_data.subject,
        msg_count=thread_data.msg_count,
        category=summary.category,
        urgency=summary.urgency,
        folder_name=folder_name,
    )

    print(f"      💾 Saved to: {folder_name}/")
    return folder_name


# ── Build index.json ─────────────────────────────────────────────────

def build_index(scan_stats: dict | None = None):
    """
    Build/rebuild the master index.json from the database.

    Args:
        scan_stats: Optional dict with scan metadata
            (total_scanned, qualifying, etc.)
    """
    all_threads = get_all_threads()

    # Count categories
    categories: dict[str, int] = {}
    for t in all_threads:
        cat = t.get("category", "OTHER")
        categories[cat] = categories.get(cat, 0) + 1

    index_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_processed": len(all_threads),
        "categories": categories,
        "scan_stats": scan_stats or {},
        "emails": [
            {
                "folder": t["folder_name"],
                "subject": t["subject"],
                "category": t["category"],
                "urgency": t["urgency"],
                "msg_count": t["last_msg_count"],
                "processed_at": t["processed_at"],
            }
            for t in all_threads
        ],
    }

    index_path = config.TARGET_DIR / "index.json"
    index_path.write_text(
        json.dumps(index_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n📋 Index saved: {index_path}")
    print(f"   Total threads: {len(all_threads)}")
    for cat, count in sorted(categories.items()):
        print(f"   {cat}: {count}")


# ── Helpers ──────────────────────────────────────────────────────────

def _get_date_range(thread_data: ThreadData) -> list[str]:
    """Get [first_date, last_date] from messages."""
    if not thread_data.messages:
        return ["", ""]
    first = thread_data.messages[0].date or ""
    last = thread_data.messages[-1].date or ""
    return [first, last]


def _get_parties(thread_data: ThreadData) -> list[str]:
    """Get unique sender names from the thread."""
    parties = set()
    for msg in thread_data.messages:
        name = msg.sender.strip()
        if name:
            parties.add(name)
    return sorted(parties)
