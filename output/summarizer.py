"""
Summarizer — Generate AI summaries and categorize email threads.

Uses the reasoning LLM tier (falls back to normal).
Trade-finance-aware categorization built into the prompt.
"""

from core.llm_client import summarize_conversation, SummaryResult
from gmail.extractor import ThreadData


async def summarize_thread(thread_data: ThreadData) -> SummaryResult:
    """
    Generate a summary and categorization for a fully extracted thread.

    Args:
        thread_data: Extracted conversation + attachment info

    Returns:
        SummaryResult with summary, category, urgency, key entities
    """
    print(f"      🧠 Generating summary...")

    # Build conversation text for the LLM
    conversation_text = thread_data.formatted_conversation

    # Collect attachment names for context
    attachment_names = []
    for msg in thread_data.messages:
        attachment_names.extend(msg.attachments)

    # Also include downloaded file names
    for path in thread_data.attachment_paths:
        if path.name not in attachment_names:
            attachment_names.append(path.name)

    # Deduplicate
    attachment_names = list(set(attachment_names))

    try:
        result = await summarize_conversation(
            conversation_text=conversation_text,
            attachment_names=attachment_names if attachment_names else None,
        )
        print(f"      ✓ Category: {result.category} | Urgency: {result.urgency}")
        return result

    except Exception as e:
        print(f"      ✗ Summary failed: {e}")
        return SummaryResult(
            summary=f"Summary generation failed: {str(e)[:200]}",
            category="OTHER",
            urgency="LOW",
            key_entities={},
            raw={"error": str(e)[:200]},
        )
