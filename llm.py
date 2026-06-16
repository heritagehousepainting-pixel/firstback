"""Shared LLM provider core for the trades_core kernel (vendored into each product).

The provider plumbing both products had duplicated: MiniMax (OpenAI-compatible, live),
Claude (optional), and a keyless demo fallback. Each app keeps its OWN prompts, output
shaping (_clean_punct differs by newline handling), and generation functions — only the
HTTP/SDK call and the provider-selection/think-stripping helpers live here.

Edit trades_core/llm.py, then run `python3 trades_core/sync.py`.
"""
import re

from config import (PROVIDER, ANTHROPIC_API_KEY, CLAUDE_MODEL,
                    MINIMAX_API_KEY, MINIMAX_MODEL, MINIMAX_BASE_URL)


def active_provider():
    """Which brain is actually usable right now (chosen provider + its key present)."""
    if PROVIDER == "claude" and ANTHROPIC_API_KEY:
        return "claude"
    if PROVIDER == "minimax" and MINIMAX_API_KEY:
        return "minimax"
    return "demo"


def strip_think(text):
    """Remove a MiniMax reasoning block (even an UNCLOSED <think>, when the model
    runs out of tokens mid-reasoning) so partial chain-of-thought never leaks into a
    customer reply or a notes payload; the caller then sees '' and falls back."""
    return re.sub(r"<think>.*?(?:</think>|$)", "", text or "", flags=re.DOTALL).strip()


def complete(provider, system, messages, *, max_tokens, temperature=0.8):
    """One completion. `system` is the system prompt; `messages` is the conversation
    WITHOUT the system message (a list of {role, content}) — for a single-shot call
    pass [{"role": "user", "content": user_text}]. MiniMax gets the system prepended
    as a message + `temperature`; Claude gets `system=` separately and ignores
    `temperature` (matching prior behavior). MiniMax reasoning is stripped. An unknown
    or demo provider returns '' so callers fall back cleanly.
    """
    if provider == "minimax":
        import requests  # bundles certifi so TLS verifies cleanly on macOS
        resp = requests.post(
            f"{MINIMAX_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MINIMAX_MODEL,
                  "messages": [{"role": "system", "content": system}] + list(messages),
                  "max_completion_tokens": max_tokens, "temperature": temperature,
                  "thinking": {"type": "disabled"}},
            timeout=30)
        resp.raise_for_status()
        return strip_think(resp.json()["choices"][0]["message"]["content"])
    if provider == "claude":
        import anthropic  # imported lazily so the other paths need no install
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                       system=system, messages=list(messages))
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    return ""
