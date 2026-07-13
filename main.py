"""
Gmail Agent — Main Entry Point
Orchestrates: Login → Scan → Filter → Extract → Summarize → Save → Index
"""

import asyncio
import sys

import config
from database import init_db, should_process, delete_thread_folder, get_thread
from gmail.login import launch_browser, login_to_gmail
from gmail.scanner import scan_inbox, open_thread, go_back_to_inbox
from gmail.extractor import extract_thread
from output.summarizer import summarize_thread
from output.file_manager import save_thread, build_index


async def main():
    print("=" * 55)
    print("  📧  Gmail Agent")
    print("=" * 55)
    print(f"  Email:       {config.GMAIL_EMAIL}")
    print(f"  Threshold:   >{config.THREAD_COUNT_THRESHOLD} messages")
    print(f"  Output:      {config.TARGET_DIR}")
    print(f"  Headless:    {config.HEADLESS}")
    print(f"  Vision LLM:  {'ON' if config.llm_available('vision') else 'OFF'}")
    print(f"  Reason LLM:  {'ON' if config.llm_available('reasoning') else 'OFF'}")
    print(f"  Normal LLM:  {'ON' if config.llm_available('normal') else 'OFF'}")
    print("=" * 55)

    # Sanity check
    if not config.GMAIL_EMAIL or not config.GMAIL_PASSWORD:
        print("\n✗ GMAIL_EMAIL and GMAIL_PASSWORD must be set in .env")
        sys.exit(1)

    if not config.llm_available("normal") and not config.llm_available("vision"):
        print("\n✗ At least one LLM model must be configured in .env")
        sys.exit(1)

    init_db()

    # ── Phase 1: Launch browser & login ──────────────────────────
    print("\n── Phase 1: Login ─────────────────────────────────")
    pw, browser, page = await launch_browser()

    try:
        logged_in = await login_to_gmail(page)
        if not logged_in:
            print("\n✗ Could not log into Gmail. Exiting.")
            await browser.close()
            await pw.stop()
            sys.exit(1)

        # ── Phase 2: Scan inbox ──────────────────────────────────
        print("\n── Phase 2: Scan Inbox ────────────────────────────")
        qualifying_threads = await scan_inbox(page)

        if not qualifying_threads:
            print("\n✓ No threads to process. Everything is up to date!")
            await browser.close()
            await pw.stop()
            build_index({"total_scanned": 0, "qualifying": 0, "processed": 0})
            return

        # ── Phase 3-4: Extract + Summarize each thread ───────────
        print(f"\n── Phase 3-4: Extract & Summarize ({len(qualifying_threads)} threads) ──")

        processed = 0
        failed = 0

        for i, thread_info in enumerate(qualifying_threads, 1):
            print(f"\n{'━' * 55}")
            print(f"  Thread {i}/{len(qualifying_threads)}: {thread_info.subject[:50]}")
            print(f"  Messages: {thread_info.msg_count} | Status: {thread_info.status}")
            print(f"{'━' * 55}")

            # If updated, wipe old folder
            if thread_info.status == "updated":
                old = get_thread(thread_info.thread_id)
                if old and old.get("folder_name"):
                    print(f"   🗑  Deleting old folder: {old['folder_name']}")
                    delete_thread_folder(old["folder_name"])

            # Open the thread
            opened = await open_thread(page, thread_info)
            if not opened:
                print(f"   ✗ Could not open thread, skipping")
                failed += 1
                await go_back_to_inbox(page)
                continue

            # Extract conversation + attachments
            thread_data = await extract_thread(
                page,
                thread_id=thread_info.thread_id,
                subject=thread_info.subject,
            )

            if not thread_data.messages:
                print(f"   ⚠ No messages extracted, skipping")
                failed += 1
                await go_back_to_inbox(page)
                continue

            # Summarize
            summary = await summarize_thread(thread_data)

            # Save to disk + update DB
            folder_name = save_thread(
                thread_data=thread_data,
                summary=summary,
                folder_index=i,
            )

            processed += 1

            # Go back to inbox for next thread
            await go_back_to_inbox(page)

        # ── Phase 5: Build index ─────────────────────────────────
        print(f"\n── Phase 5: Build Index ───────────────────────────")
        build_index({
            "total_qualifying": len(qualifying_threads),
            "processed": processed,
            "failed": failed,
        })

        # ── Done ─────────────────────────────────────────────────
        print(f"\n{'=' * 55}")
        print(f"  ✓ COMPLETE")
        print(f"    Processed: {processed}")
        print(f"    Failed:    {failed}")
        print(f"    Output:    {config.TARGET_DIR}")
        print(f"{'=' * 55}")

    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user. Saving progress...")
        build_index({"note": "interrupted"})

    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\n🔒 Closing browser...")
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())