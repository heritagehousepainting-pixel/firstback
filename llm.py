"""Shared LLM provider core for the trades_core kernel (vendored into each product).

The provider plumbing both products had duplicated: MiniMax (OpenAI-compatible, live),
Claude (optional), and a keyless demo fallback. Each app keeps its OWN prompts, output
shaping (_clean_punct differs by newline handling), and generation functions — only the
HTTP/SDK call and the provider-selection/think-stripping helpers live here.

Edit trades_core/llm.py, then run `python3 trades_core/sync.py`.
"""
import json as _json
import re

from config import (PROVIDER, ANTHROPIC_API_KEY, CLAUDE_MODEL,
                    MINIMAX_API_KEY, MINIMAX_MODEL, MINIMAX_BASE_URL)


# ---- Provider message/tool mappers (shared by the blocking + streaming tool calls) -------
def _claude_msgs(messages):
    """Neutral thread -> Anthropic Messages content blocks."""
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                               "input": tc.get("input") or {}})
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                 "content": m["content"]}]})
    return out


def _claude_tools(tools):
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["input_schema"]} for t in tools]


def _claude_result(content):
    """Anthropic response content blocks -> normalized {text, tool_calls}."""
    text = "".join(b.text for b in content if b.type == "text").strip()
    calls = [{"id": b.id, "name": b.name, "input": b.input or {}}
             for b in content if b.type == "tool_use"]
    return {"text": text, "tool_calls": calls}


def _minimax_msgs(system, messages):
    """Neutral thread -> OpenAI-compatible messages (system prepended)."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            am = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                am["tool_calls"] = [{"id": tc["id"], "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": _json.dumps(tc.get("input") or {})}}
                                    for tc in m["tool_calls"]]
            out.append(am)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                        "content": m["content"]})
    return out


def _minimax_tools(tools):
    return [{"type": "function", "function": {"name": t["name"],
             "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]


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


def tool_complete(provider, system, messages, tools, *, max_tokens=700, temperature=0.4):
    """One round of a tool-use conversation, provider-agnostic. `messages` is a NEUTRAL list
    the caller threads across rounds:
        {"role":"user","content":str}
        {"role":"assistant","content":str,"tool_calls":[{"id","name","input"}]}
        {"role":"tool","tool_call_id":str,"content":str}
    `tools` is [{"name","description","input_schema"}] (JSON-Schema input). Returns a
    normalized {"text":str,"tool_calls":[{"id","name","input"}]}. An unknown/demo provider
    returns empties so the caller falls back to the deterministic keyword router.
    """
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        # No temperature/top_p/budget_tokens: Claude Opus 4.8 rejects them (adaptive thinking
        # only). The neutral message/tool mapping is shared with tool_complete_stream.
        resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                       system=system, messages=_claude_msgs(messages),
                                       tools=_claude_tools(tools))
        return _claude_result(resp.content)
    if provider == "minimax":
        import requests
        omsgs = _minimax_msgs(system, messages)
        otools = _minimax_tools(tools)
        resp = requests.post(
            f"{MINIMAX_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {MINIMAX_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": MINIMAX_MODEL, "messages": omsgs, "tools": otools,
                  "max_completion_tokens": max_tokens, "temperature": temperature,
                  "thinking": {"type": "disabled"}},
            timeout=30)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        calls = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                inp = _json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                inp = {}
            calls.append({"id": tc.get("id") or fn.get("name"), "name": fn.get("name"),
                          "input": inp})
        return {"text": strip_think(msg.get("content") or ""), "tool_calls": calls}
    return {"text": "", "tool_calls": []}


def tool_complete_stream(provider, system, messages, tools, *, max_tokens=700,
                         temperature=0.4):
    """Streaming sibling of tool_complete for ONE tool-use round. A generator that yields
    ('text', delta) as the model produces visible text, then exactly one ('result', {text,
    tool_calls}) when the round is complete -- the same normalized shape tool_complete returns.

    Live token streaming is implemented for Claude via the Anthropic SDK's messages.stream
    (Opus 4.8: no temperature/top_p/budget_tokens -- adaptive thinking only). For any other
    provider there is no live token stream here: the caller computes the reply and streams it
    over the SSE channel itself, so this yields a single ('result', ...) with no text deltas."""
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        with client.messages.stream(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                     system=system, messages=_claude_msgs(messages),
                                     tools=_claude_tools(tools)) as stream:
            for event in stream:
                if event.type == "content_block_delta" \
                        and getattr(event.delta, "type", "") == "text_delta":
                    yield ("text", event.delta.text)
            final = stream.get_final_message()
        yield ("result", _claude_result(final.content))
        return
    # No live stream for this provider: hand back the blocking result, no deltas.
    yield ("result", tool_complete(provider, system, messages, tools,
                                   max_tokens=max_tokens, temperature=temperature))
