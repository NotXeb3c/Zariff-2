"""Dynamic sliding context window for long-running AI agent tasks.

This module provides utilities to prevent token overflow by intelligently
compressing the middle of a conversation history while preserving the
system prompt, initial goal, and the most recent N interactions.
"""

import logging
from typing import Any

import tiktoken

logger = logging.getLogger("pilot.memory.sliding_window")

try:
    _ENCODING = tiktoken.get_encoding("cl100k_base")
except Exception as _e:
    logger.warning(f"Failed to load tiktoken encoding: {_e}")
    _ENCODING = None


def get_token_count(text: str) -> int:
    """Return the number of tokens in a string using cl100k_base encoding."""
    if _ENCODING is not None:
        try:
            return len(_ENCODING.encode(text))
        except Exception as e:
            logger.warning(f"Token counting failed: {e}. Falling back to character count heuristic.")
    return len(text) // 4


def count_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Calculate the total token count for a list of messages."""
    total = 0
    for msg in messages:
        total += get_token_count(str(msg.get("content", "")))
        total += get_token_count(str(msg.get("role", "")))
        total += 4  # Formatting overhead per message
    return total


def summarize_messages(messages: list[dict[str, Any]], max_summary_items: int = 50) -> str:
    """Create a structured summary of older messages.

    This is a fast heuristic summarization approach. For future enhancement,
    this could be swapped out with an LLM-based summary.
    """
    summary_parts = ["## Compressed context summary\n"]

    # Intelligently truncate if there are too many messages
    if len(messages) > max_summary_items:
        half = max_summary_items // 2
        messages_to_summarize = (
            messages[:half]
            + [{"role": "system", "content": f"... [{len(messages) - max_summary_items} messages omitted] ..."}]
            + messages[-half:]
        )
    else:
        messages_to_summarize = messages

    for msg in messages_to_summarize:
        role = msg.get("role", "unknown")
        content = str(msg.get("content", "")).strip()

        is_existing_summary = (
            role == "system" and ("Compressed History Summary" in content or "Compressed context summary" in content)
        ) or msg.get("is_summary")

        if is_existing_summary:
            # Flatten existing summary
            lines = content.split("\n")
            for line in lines:
                line_stripped = line.strip()
                if line_stripped and not line_stripped.startswith("##"):
                    summary_parts.append(line_stripped)
        else:
            if len(content) > 100:
                content = content[:97] + "..."
            summary_parts.append(f"- {role.capitalize()}: {content}")

    return "\n".join(summary_parts)


def build_sliding_context(
    messages: list[dict[str, Any]], max_recent_messages: int = 10, max_context_tokens: int = 8000
) -> list[dict[str, Any]]:
    """Build an optimized sliding context window to prevent token overflow.

    Args:
        messages: The full list of conversation messages.
        max_recent_messages: Number of recent messages to preserve exactly.
        max_context_tokens: Token threshold that triggers summarization.

    Returns:
        An optimized list of messages under the token limit.
    """
    if not messages:
        return []

    current_tokens = count_message_tokens(messages)

    # Check if we need to slide the window
    if current_tokens <= max_context_tokens:
        return messages.copy()

    logger.info(f"Sliding window activated. Current tokens: {current_tokens} > Limit: {max_context_tokens}")

    # 1. Find system message dynamically
    system_msg = next((m for m in messages if m.get("role") == "system" and not m.get("is_summary")), None)

    # 2. Find goal safely
    goal_msg = next((m for m in messages if m.get("type") == "goal"), None)
    if goal_msg is None:
        # Fallback to the first user message
        goal_msg = next((m for m in messages if m.get("role") == "user"), None)

    # Calculate recent messages window boundary
    recent_start_idx = max(0, len(messages) - max_recent_messages)

    # Exclude system_msg and goal_msg from the recent messages and middle messages to prevent duplication
    recent_messages = []
    for m in messages[recent_start_idx:]:
        if m is system_msg or m is goal_msg:
            continue
        recent_messages.append(m)

    # Identify middle messages to summarize
    middle_messages = []
    for m in messages[:recent_start_idx]:
        if m is system_msg or m is goal_msg:
            continue
        middle_messages.append(m)

    # Rebuild optimized context
    optimized_context = []
    if system_msg:
        optimized_context.append(system_msg)
    if goal_msg and goal_msg is not system_msg:
        optimized_context.append(goal_msg)

    # Summarize middle history
    if middle_messages:
        summarized_history = summarize_messages(middle_messages)
        logger.info(f"Summarized {len(middle_messages)} messages.")

        # Clean up any existing summaries in the recent messages to prevent summary stacking
        recent_messages = [m for m in recent_messages if not m.get("is_summary")]

        summary_msg = {
            "role": "system",
            "content": summarized_history,
            "is_summary": True,
        }
        optimized_context.append(summary_msg)

    # Add the recent messages back
    optimized_context.extend(recent_messages)
    logger.info(f"Preserved last {len(recent_messages)} interactions.")

    return optimized_context


def fit_messages_to_token_budget(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """Hard-fit a message list below a model input budget.

    ``build_sliding_context`` summarizes history but intentionally preserves a
    generous recent window. A stricter per-action budget may be smaller than
    that conversation budget. This final pass drops oldest optional history,
    preserves the system policy and newest request, and only as a last resort
    trims the *front* of the newest request so its goal at the tail survives.
    """
    if not messages or max_tokens <= 0:
        return messages.copy()
    fitted = [message.copy() for message in messages]
    if count_message_tokens(fitted) <= max_tokens:
        return fitted

    system_index = next(
        (
            index
            for index, message in enumerate(fitted)
            if message.get("role") == "system" and not message.get("is_summary")
        ),
        None,
    )
    latest_index = len(fitted) - 1
    goal_index = next((index for index, message in enumerate(fitted) if message.get("type") == "goal"), None)

    # Drop summaries and ordinary history from oldest to newest. Keep the
    # original goal until all other optional context has been removed.
    removable = [index for index in range(len(fitted)) if index not in {system_index, latest_index, goal_index}]
    removable.extend(index for index in [goal_index] if index is not None and index not in {system_index, latest_index})
    removed: set[int] = set()
    for index in removable:
        removed.add(index)
        candidate = [message for position, message in enumerate(fitted) if position not in removed]
        if count_message_tokens(candidate) <= max_tokens:
            return candidate

    mandatory = [message for position, message in enumerate(fitted) if position not in removed]
    if count_message_tokens(mandatory) <= max_tokens:
        return mandatory

    # The static system policy can be large. Retain it intact and fit the
    # newest request by taking the longest suffix that stays under budget.
    latest_position = next(
        (position for position, message in enumerate(mandatory) if message is fitted[latest_index]),
        len(mandatory) - 1,
    )
    latest = mandatory[latest_position].copy()
    content = str(latest.get("content", ""))
    low, high = 0, len(content)
    best = ""
    while low <= high:
        length = (low + high) // 2
        latest["content"] = content[-length:] if length else ""
        candidate = mandatory.copy()
        candidate[latest_position] = latest.copy()
        if count_message_tokens(candidate) <= max_tokens:
            best = str(latest["content"])
            low = length + 1
        else:
            high = length - 1
    latest["content"] = best
    mandatory[latest_position] = latest
    return mandatory
