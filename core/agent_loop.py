"""
Agent Loop — The core step loop.

Cycle:
  1. Extract DOM state (+ screenshot)
  2. Send to LLM → get action decision
  3. Execute each action
  4. Check if done or error
  5. Repeat

This is the equivalent of browser-use's agent/service.py
but stripped to only what Gmail needs.
"""

import asyncio
from dataclasses import dataclass, field
from playwright.async_api import Page

import config
from core.dom_extractor import extract_state, BrowserState
from core.llm_client import get_navigation_action, AgentDecision, AgentAction
from core.actions import execute_action, ActionResult


# ── Step record ──────────────────────────────────────────────────────

@dataclass
class StepRecord:
    step_number: int
    url: str
    evaluation: str
    actions_taken: list[str]
    success: bool
    is_done: bool = False
    extracted: str = ""


@dataclass
class AgentResult:
    """Final result after the agent loop completes."""
    success: bool
    steps: list[StepRecord]
    total_steps: int
    extracted_content: list[str]  # all extracted text across steps
    final_message: str = ""

    @property
    def history_lines(self) -> list[str]:
        """Flat list of action descriptions for LLM context."""
        lines = []
        for step in self.steps:
            for action_desc in step.actions_taken:
                lines.append(action_desc)
        return lines


# ── The loop ─────────────────────────────────────────────────────────

async def run_agent(
    page: Page,
    task: str,
    max_steps: int | None = None,
    on_step: callable = None,
) -> AgentResult:
    """
    Run the agent loop until the task is done or max_steps is reached.

    Args:
        page: Playwright page to operate on
        task: Natural language description of what the agent should accomplish
        max_steps: Override for config.MAX_STEPS
        on_step: Optional callback(step_record) called after each step

    Returns:
        AgentResult with full execution history
    """
    max_steps = max_steps or config.MAX_STEPS
    steps: list[StepRecord] = []
    extracted_content: list[str] = []
    consecutive_failures = 0
    use_vision = config.llm_available("vision")

    print(f"\n🤖 Agent starting: {task}")
    print(f"   Max steps: {max_steps} | Vision: {'ON' if use_vision else 'OFF'}\n")

    for step_num in range(1, max_steps + 1):
        print(f"── Step {step_num} {'─' * 40}")

        # ── 1. Extract DOM state ─────────────────────────────────
        try:
            state: BrowserState = await extract_state(
                page, take_screenshot=use_vision
            )
            print(f"   URL: {state.viewport.url[:80]}")
            print(f"   Elements: {state.element_count}")
        except Exception as e:
            print(f"   ✗ DOM extraction failed: {e}")
            consecutive_failures += 1
            if consecutive_failures >= config.MAX_FAILURES_PER_STEP:
                print(f"   ✗ Too many consecutive failures, stopping.")
                break
            await asyncio.sleep(2)
            continue

        # ── 2. Ask LLM for action ───────────────────────────────
        try:
            history_lines = []
            for s in steps[-10:]:  # last 10 steps for context
                for a in s.actions_taken:
                    history_lines.append(a)

            decision: AgentDecision = await get_navigation_action(
                state=state,
                task=task,
                step_number=step_num,
                history=history_lines,
            )
            print(f"   LLM: {decision.evaluation[:100]}")
        except Exception as e:
            print(f"   ✗ LLM call failed: {e}")
            consecutive_failures += 1
            if consecutive_failures >= config.MAX_FAILURES_PER_STEP:
                break
            await asyncio.sleep(2)
            continue

        # ── 3. Execute actions ───────────────────────────────────
        step_actions: list[str] = []
        step_success = True
        step_done = False
        step_extracted = ""

        for i, agent_action in enumerate(decision.actions):
            print(f"   Action {i+1}/{len(decision.actions)}: {agent_action.action} {agent_action.params}")

            result: ActionResult = await execute_action(
                page=page,
                action=agent_action.action,
                params=agent_action.params,
                selector_map=state.selector_map,
            )

            print(f"   {result}")
            step_actions.append(str(result))

            if result.extracted:
                extracted_content.append(result.extracted)
                step_extracted = result.extracted

            if not result.success:
                step_success = False
                consecutive_failures += 1
                break  # stop executing remaining actions in this step

            if result.is_done:
                step_done = True
                break

            # Brief pause between batched actions
            if i < len(decision.actions) - 1:
                await asyncio.sleep(0.3)

        # ── 4. Record step ───────────────────────────────────────
        record = StepRecord(
            step_number=step_num,
            url=state.viewport.url,
            evaluation=decision.evaluation,
            actions_taken=step_actions,
            success=step_success,
            is_done=step_done,
            extracted=step_extracted,
        )
        steps.append(record)

        if on_step:
            on_step(record)

        # Reset failure counter on success
        if step_success:
            consecutive_failures = 0

        # ── 5. Check if done ─────────────────────────────────────
        if step_done:
            print(f"\n✓ Agent completed in {step_num} steps.")
            return AgentResult(
                success=True,
                steps=steps,
                total_steps=step_num,
                extracted_content=extracted_content,
                final_message=step_extracted or decision.evaluation,
            )

        # Check consecutive failures
        if consecutive_failures >= config.MAX_FAILURES_PER_STEP:
            print(f"\n✗ Too many failures ({consecutive_failures}), stopping.")
            break

        # Small delay between steps
        await asyncio.sleep(0.3)

    # Reached max steps without completing
    print(f"\n⚠ Agent stopped after {len(steps)} steps (max reached or failures).")
    return AgentResult(
        success=False,
        steps=steps,
        total_steps=len(steps),
        extracted_content=extracted_content,
        final_message="Agent did not complete the task within step limit.",
    )


# ── Subtask runner (convenience) ─────────────────────────────────────

async def run_subtask(page: Page, task: str, max_steps: int = 30) -> AgentResult:
    """
    Run a smaller subtask with a tighter step limit.
    Used for focused operations like 'expand all messages in this thread'.
    """
    return await run_agent(page, task, max_steps=max_steps)
