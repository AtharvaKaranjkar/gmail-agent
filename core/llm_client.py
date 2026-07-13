"""
LLM Client — Interface to NVIDIA NIM API.

Three modes of operation:
  1. Navigation  — DOM state (+ screenshot if vision available) → action JSON
  2. Summarize   — conversation text → summary + category JSON
  3. Raw         — any prompt → text response

Handles tier fallback: vision → reasoning → normal
"""

import json
import aiohttp
from dataclasses import dataclass

import config
from core.dom_extractor import BrowserState


# ── Response types ───────────────────────────────────────────────────

@dataclass
class AgentAction:
    """A single action the agent wants to perform."""
    action: str          # click, type, scroll, wait, go_back, extract, download, ask_human, done
    params: dict         # action-specific parameters


@dataclass
class AgentDecision:
    """Full LLM response for a navigation step."""
    evaluation: str      # LLM's assessment of current state
    memory: str          # anything it wants to remember for later steps
    actions: list[AgentAction]


@dataclass
class SummaryResult:
    """LLM response for conversation summarization."""
    summary: str
    category: str
    urgency: str
    key_entities: dict
    raw: dict            # full parsed response


# ── System prompts ───────────────────────────────────────────────────

NAVIGATION_SYSTEM_PROMPT = """You are a browser automation agent controlling Gmail. You see the page's interactive elements (indexed) and text content. You decide what action to take next.

AVAILABLE ACTIONS (respond with valid JSON only):
- click(index): Click an interactive element by its index number
- type(index, text): Type text into an input field at the given index
- scroll(direction): Scroll the page. direction = "up" or "down"
- wait(seconds): Wait for the page to load. seconds = 1-5
- go_back(): Navigate back in browser history
- download(index): Click a download button/link at the given index
- ask_human(question): Pause and ask the user a question (use for OTP, CAPTCHA, unexpected states)
- done(result): Signal that the current task is complete. result = description of what was accomplished

RESPONSE FORMAT — respond ONLY with this JSON, no markdown, no extra text:
{
    "evaluation": "Brief assessment of what you see on the page right now",
    "memory": "Important info to remember for future steps (or empty string)",
    "actions": [
        {"action": "click", "params": {"index": 5}},
        {"action": "type", "params": {"index": 3, "text": "hello"}}
    ]
}

RULES:
- Maximum 3 actions per response
- Use the element INDEX numbers from the page state, never CSS selectors
- If the page looks unexpected or stuck, use ask_human
- If you see a CAPTCHA or verification prompt, use ask_human
- After typing in a field, usually click a submit/next button in the same response
- Be precise — don't guess element indices, only use ones you see in the state
- If you need to scroll to find elements, scroll first, then act in the next step
"""

SUMMARY_SYSTEM_PROMPT = """You are a trade finance analyst. Analyze email conversations and provide structured analysis.

Respond ONLY with valid JSON, no markdown, no extra text:
{
    "summary": "3-5 sentence summary covering: parties involved, subject matter, key decisions, current status",
    "category": "one of: FRESH_INQUIRY | UNDER_NEGOTIATION | DEAL_FINALIZED | SHIPMENT_IN_PROGRESS | PAYMENT_PENDING | CLOSED | OTHER",
    "urgency": "one of: HIGH | MEDIUM | LOW",
    "key_entities": {
        "buyer": "company name or empty",
        "seller": "company name or empty",
        "product": "what is being traded or empty",
        "value": "monetary value if mentioned or empty",
        "incoterm": "CFR/CIF/FOB etc if mentioned or empty"
    }
}

CATEGORY DEFINITIONS:
- FRESH_INQUIRY: New order/request just opened, initial contact
- UNDER_NEGOTIATION: Terms being discussed, price/quantity/delivery back and forth
- DEAL_FINALIZED: Agreement reached, PO confirmed, awaiting execution
- SHIPMENT_IN_PROGRESS: Goods dispatched, shipping docs in transit
- PAYMENT_PENDING: Awaiting LC/payment/remittance
- CLOSED: Transaction fully completed
- OTHER: Doesn't fit above categories
"""


# ── Core API call ────────────────────────────────────────────────────

async def _call_nim(
    messages: list[dict],
    llm_config: dict,
    temperature: float = 0.1,
    max_tokens: int = 1024,
) -> str:
    """
    Make a raw API call to NVIDIA NIM (OpenAI-compatible endpoint).
    Returns the text content of the response.
    """
    url = f"{llm_config['base_url']}/chat/completions"

    headers = {
        "Authorization": f"Bearer {llm_config['api_key']}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": llm_config["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=payload,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"NIM API error {resp.status}: {error_text}")
            data = await resp.json()

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("NIM API returned no choices")

    return choices[0]["message"]["content"].strip()


def _clean_json(text: str) -> str:
    """Strip markdown fences and extra whitespace from LLM JSON output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ── Navigation decision ─────────────────────────────────────────────

async def get_navigation_action(
    state: BrowserState,
    task: str,
    step_number: int,
    history: list[str] | None = None,
) -> AgentDecision:
    """
    Send the current browser state to the LLM and get back action decisions.

    Uses vision tier if available (sends DOM + screenshot).
    Falls back to normal tier (DOM text only).
    """
    use_vision = config.llm_available("vision")
    llm_cfg = config.resolve_llm("vision", ["normal"])

    if not llm_cfg:
        raise RuntimeError("No LLM configured. Set at least NIM_NORMAL_MODEL in .env")

    # ── Build the page state text ────────────────────────────────
    state_text = (
        f"CURRENT TASK: {task}\n"
        f"STEP: {step_number}\n"
        f"URL: {state.viewport.url}\n"
        f"PAGE TITLE: {state.viewport.title}\n"
        f"INTERACTIVE ELEMENTS: {state.element_count}\n"
        f"\n--- PAGE STATE ---\n"
        f"{state.llm_text}\n"
        f"--- END PAGE STATE ---"
    )

    if history:
        recent = history[-10:]
        history_text = "\n".join(f"  Step {i+1}: {h}" for i, h in enumerate(recent))
        state_text += f"\n\n--- RECENT HISTORY ---\n{history_text}\n--- END HISTORY ---"

    # ── Build messages ───────────────────────────────────────────
    if use_vision and state.screenshot_b64:
        user_content = [
            {"type": "text", "text": state_text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{state.screenshot_b64}",
                },
            },
        ]
    else:
        user_content = state_text

    messages = [
        {"role": "system", "content": NAVIGATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    raw_response = await _call_nim(messages, llm_cfg)

    # ── Parse response ───────────────────────────────────────────
    cleaned = _clean_json(raw_response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return AgentDecision(
            evaluation=f"Failed to parse LLM response: {raw_response[:200]}",
            memory="",
            actions=[AgentAction(
                action="ask_human",
                params={"question": "LLM returned unparseable response. What should I do?"},
            )],
        )

    actions = []
    for act in parsed.get("actions", []):
        actions.append(AgentAction(
            action=act.get("action", "wait"),
            params=act.get("params", {}),
        ))

    actions = actions[:config.MAX_ACTIONS_PER_STEP]

    if not actions:
        actions = [AgentAction(action="wait", params={"seconds": 2})]

    return AgentDecision(
        evaluation=parsed.get("evaluation", ""),
        memory=parsed.get("memory", ""),
        actions=actions,
    )


# ── Summarization ───────────────────────────────────────────────────

async def summarize_conversation(
    conversation_text: str,
    attachment_names: list[str] | None = None,
) -> SummaryResult:
    """
    Send a conversation transcript to the LLM for summarization.

    Uses reasoning tier if available, falls back to normal.
    """
    llm_cfg = config.resolve_llm("reasoning", ["normal"])

    if not llm_cfg:
        raise RuntimeError("No LLM configured for summarization.")

    user_text = f"Analyze this email conversation:\n\n{conversation_text}"

    if attachment_names:
        att_list = "\n".join(f"  - {name}" for name in attachment_names)
        user_text += f"\n\nAttachments found in this thread:\n{att_list}"

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]

    raw_response = await _call_nim(messages, llm_cfg, temperature=0.2, max_tokens=1500)

    cleaned = _clean_json(raw_response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return SummaryResult(
            summary=f"Failed to parse summary. Raw: {raw_response[:500]}",
            category="OTHER",
            urgency="LOW",
            key_entities={},
            raw={"error": "json_parse_failed", "raw": raw_response[:500]},
        )

    return SummaryResult(
        summary=parsed.get("summary", ""),
        category=parsed.get("category", "OTHER"),
        urgency=parsed.get("urgency", "LOW"),
        key_entities=parsed.get("key_entities", {}),
        raw=parsed,
    )


# ── Raw LLM call (utility) ──────────────────────────────────────────

async def ask_llm(prompt: str, tier: str = "normal") -> str:
    """
    Simple text-in → text-out LLM call.
    Useful for one-off questions during agent execution.
    """
    llm_cfg = config.resolve_llm(tier, ["normal"])
    if not llm_cfg:
        raise RuntimeError(f"No LLM configured for tier: {tier}")

    messages = [{"role": "user", "content": prompt}]
    return await _call_nim(messages, llm_cfg)