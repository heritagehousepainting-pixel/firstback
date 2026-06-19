"""Shared LLM provider core for the trades_core kernel (vendored into each product).

The provider plumbing both products had duplicated: MiniMax (OpenAI-compatible, live),
Claude (optional), and a keyless demo fallback. Each app keeps its OWN prompts, output
shaping (_clean_punct differs by newline handling), and generation functions — only the
HTTP/SDK call and the provider-selection/think-stripping helpers live here.

Edit trades_core/llm.py, then run `python3 trades_core/sync.py`.
"""
import json as _json
import re

from config import (PROVIDER, ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MODEL_VOICE,
                    MINIMAX_API_KEY, MINIMAX_MODEL, MINIMAX_BASE_URL)

# Phase 1 — Claude pricing (per 1 M tokens, USD) so we can compute cost without
# a live API call. These are the published rates for Sonnet/Haiku at launch; update
# when Anthropic changes pricing. Voice uses CLAUDE_MODEL_VOICE (Haiku).
_CLAUDE_PRICE = {
    # model-id-prefix -> (input_per_1m, output_per_1m)
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku":  (0.80,  4.00),
    "claude-opus":   (15.00, 75.00),
}


def _claude_cost(model, input_tokens, output_tokens):
    """Estimated cost in USD for one Claude call given the token counts."""
    model_l = (model or "").lower()
    inp_rate, out_rate = 3.00, 15.00   # fallback: Sonnet rates
    for prefix, rates in _CLAUDE_PRICE.items():
        if prefix in model_l:
            inp_rate, out_rate = rates
            break
    return round(
        input_tokens / 1_000_000 * inp_rate
        + output_tokens / 1_000_000 * out_rate, 8
    )


# M-4: Confirmation-echo instruction injected into the voice system prompt (Slice 2).
# Slice 4 /internal/voice/stream appends this to the booking brain system prompt so the
# AI always speaks the slot back and waits for a verbal yes before writing [[BOOK]].
# This is a prompt-only guard -- no code branch; see PHASE5G-SPEC.md Risk 5.
VOICE_CONFIRM_BOOKING_PROMPT = (
    "Before you write [[BOOK]], speak the exact booking slot back to the caller "
    "and confirm they said yes. For example: 'So that is Thursday the 19th at "
    "2 PM -- does that work for you?' Only write [[BOOK]] after they confirm. "
    "Keep every reply to 1 to 2 sentences. Speak naturally -- say 'uh-huh', "
    "'got it', 'sure' where a real person would. If the caller says 'um', 'uh', "
    "or other ASR fillers, treat them as thinking time and wait for the real "
    "utterance. If your previous reply appears cut off, treat it as complete "
    "and respond to the caller's new utterance without re-completing the prior "
    "response."
)

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


def complete(provider, system, messages, *, max_tokens, temperature=0.8,
             model=None, return_usage=False):
    """One completion. `system` is the system prompt; `messages` is the conversation
    WITHOUT the system message (a list of {role, content}) — for a single-shot call
    pass [{"role": "user", "content": user_text}]. MiniMax gets the system prepended
    as a message + `temperature`; Claude gets `system=` separately and ignores
    `temperature` (matching prior behavior). MiniMax reasoning is stripped. An unknown
    or demo provider returns '' so callers fall back cleanly.

    `model` overrides CLAUDE_MODEL for Claude calls (e.g. CLAUDE_MODEL_VOICE for Haiku).
    When `return_usage=True`, returns (text, {"input_tokens":int, "output_tokens":int,
    "cost_usd":float, "model":str}) instead of just text.
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
        text = strip_think(resp.json()["choices"][0]["message"]["content"])
        if return_usage:
            return text, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                          "model": MINIMAX_MODEL}
        return text
    if provider == "claude":
        import anthropic  # imported lazily so the other paths need no install
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        _model = model or CLAUDE_MODEL
        # Prompt caching: mark the shared system-prompt block ephemeral so repeated
        # calls with the same system prompt hit Anthropic's cache (cheaper + faster).
        system_block = [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
        resp = client.messages.create(model=_model, max_tokens=max_tokens,
                                       system=system_block, messages=list(messages))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if return_usage:
            inp = getattr(resp.usage, "input_tokens", 0) or 0
            out = getattr(resp.usage, "output_tokens", 0) or 0
            return text, {"input_tokens": inp, "output_tokens": out,
                          "cost_usd": _claude_cost(_model, inp, out), "model": _model}
        return text
    if return_usage:
        return "", {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "model": ""}
    return ""


def tool_complete(provider, system, messages, tools, *, max_tokens=700, temperature=0.4,
                  return_usage=False):
    """One round of a tool-use conversation, provider-agnostic. `messages` is a NEUTRAL list
    the caller threads across rounds:
        {"role":"user","content":str}
        {"role":"assistant","content":str,"tool_calls":[{"id","name","input"}]}
        {"role":"tool","tool_call_id":str,"content":str}
    `tools` is [{"name","description","input_schema"}] (JSON-Schema input). Returns a
    normalized {"text":str,"tool_calls":[{"id","name","input"}]}. An unknown/demo provider
    returns empties so the caller falls back to the deterministic keyword router.
    When `return_usage=True`, the dict also carries "usage" with token + cost info.
    """
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        # No temperature/top_p/budget_tokens: Claude rejects them for extended thinking.
        # Prompt caching: ephemeral on the shared system-prompt block.
        system_block = [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
        resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                       system=system_block, messages=_claude_msgs(messages),
                                       tools=_claude_tools(tools))
        result = _claude_result(resp.content)
        if return_usage:
            inp = getattr(resp.usage, "input_tokens", 0) or 0
            out = getattr(resp.usage, "output_tokens", 0) or 0
            result["usage"] = {"input_tokens": inp, "output_tokens": out,
                               "cost_usd": _claude_cost(CLAUDE_MODEL, inp, out),
                               "model": CLAUDE_MODEL}
        return result
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
        result = {"text": strip_think(msg.get("content") or ""), "tool_calls": calls}
        if return_usage:
            result["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                               "model": MINIMAX_MODEL}
        return result
    result = {"text": "", "tool_calls": []}
    if return_usage:
        result["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                           "model": ""}
    return result


def tool_complete_stream(provider, system, messages, tools, *, max_tokens=700,
                         temperature=0.4, model=None):
    """Streaming sibling of tool_complete for ONE tool-use round. A generator that yields
    ('text', delta) as the model produces visible text, then exactly one ('result', {text,
    tool_calls}) when the round is complete -- the same normalized shape tool_complete returns.

    Live token streaming is implemented for Claude via the Anthropic SDK's messages.stream
    (Opus 4.8: no temperature/top_p/budget_tokens -- adaptive thinking only). For any other
    provider there is no live token stream here: the caller computes the reply and streams it
    over the SSE channel itself, so this yields a single ('result', ...) with no text deltas.

    `model` overrides CLAUDE_MODEL for the Claude path (e.g. CLAUDE_MODEL_VOICE for Haiku).
    When None (default), CLAUDE_MODEL is used -- existing callers are unchanged."""
    if provider == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        _model = model or CLAUDE_MODEL
        # Prompt caching: ephemeral on the shared system-prompt block (streaming path).
        system_block = [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
        with client.messages.stream(model=_model, max_tokens=max_tokens,
                                     system=system_block, messages=_claude_msgs(messages),
                                     tools=_claude_tools(tools)) as stream:
            for event in stream:
                if event.type == "content_block_delta" \
                        and getattr(event.delta, "type", "") == "text_delta":
                    yield ("text", event.delta.text)
            final = stream.get_final_message()
        result = _claude_result(final.content)
        inp = getattr(final.usage, "input_tokens", 0) or 0
        out = getattr(final.usage, "output_tokens", 0) or 0
        result["usage"] = {"input_tokens": inp, "output_tokens": out,
                           "cost_usd": _claude_cost(_model, inp, out),
                           "model": _model}
        yield ("result", result)
        return
    # No live stream for this provider: hand back the blocking result, no deltas.
    yield ("result", tool_complete(provider, system, messages, tools,
                                   max_tokens=max_tokens, temperature=temperature))


def complete_stream_voice(system, messages, *, max_tokens=150, _provider=None):
    """Voice-path streaming completion. Yields raw text-delta strings (no tuples).

    Always uses CLAUDE_MODEL_VOICE (Haiku) for latency + cost reasons. No tools --
    the voice path is conversational-only; booking writes go through /internal/voice/turn
    at stream END, not mid-stream (see PHASE5G-SPEC.md P0-2).

    Provider-safe: when provider is 'demo' or no API key is set, yields nothing so the
    voice path silently gets an empty reply rather than crashing. The caller (Slice 1
    voice_service.py or Slice 4 /internal/voice/stream) decides how to handle an empty
    stream (e.g. send a filler or fall back to the blocking path).

    `_provider` is an internal-only override used by tests to exercise the Claude path
    without changing the module-level PROVIDER constant. Production callers omit it.
    """
    provider = _provider if _provider is not None else PROVIDER
    if provider == "claude" and ANTHROPIC_API_KEY:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system_block = [{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}]
        with client.messages.stream(model=CLAUDE_MODEL_VOICE, max_tokens=max_tokens,
                                     system=system_block,
                                     messages=_claude_msgs(messages)) as stream:
            for event in stream:
                if event.type == "content_block_delta" \
                        and getattr(event.delta, "type", "") == "text_delta":
                    yield event.delta.text
        return
    # demo / minimax / no key: yield nothing; caller handles the empty stream.
    return
