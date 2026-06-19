"""Phase 5G Slice 2 tests: llm.py voice additions + config.py voice constants.

Run: python3 test_voice_llm.py

Covers:
  - tool_complete_stream with no model arg still uses CLAUDE_MODEL (no regression)
  - complete_stream_voice requests CLAUDE_MODEL_VOICE (Haiku)
  - M-4 prompt constant exists and contains confirm-before-[[BOOK]] instruction
  - VOICE_MONTHLY_CAP_CENTS and VOICE_CREDIT_RATE_CENTS exist with correct defaults
"""
import os
import sys
import types

os.environ["FIRSTBACK_PROVIDER"] = "demo"   # deterministic, no network

import config

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- 1. Config constants exist with correct defaults -------------------------

def test_config_voice_constants():
    check("VOICE_MONTHLY_CAP_CENTS default is 2000",
          config.VOICE_MONTHLY_CAP_CENTS == 2000)
    check("VOICE_CREDIT_RATE_CENTS default is 25",
          config.VOICE_CREDIT_RATE_CENTS == 25)
    check("CLAUDE_MODEL_VOICE contains haiku (case-insensitive)",
          "haiku" in config.CLAUDE_MODEL_VOICE.lower())


# ---- 2. M-4 prompt constant exists and contains the right instruction --------

def test_m4_prompt_constant():
    import llm
    check("llm has VOICE_CONFIRM_BOOKING_PROMPT attribute",
          hasattr(llm, "VOICE_CONFIRM_BOOKING_PROMPT"))
    prompt = getattr(llm, "VOICE_CONFIRM_BOOKING_PROMPT", "")
    check("M-4 prompt references [[BOOK]]",
          "[[BOOK]]" in prompt)
    check("M-4 prompt requires confirmation before [[BOOK]]",
          "confirm" in prompt.lower() or "yes" in prompt.lower())
    check("M-4 prompt is a non-empty string",
          isinstance(prompt, str) and len(prompt) > 20)


# ---- 3. tool_complete_stream model=None regression: still uses CLAUDE_MODEL --

def test_tool_complete_stream_no_model_regression():
    """When model is not passed (or None), tool_complete_stream must use CLAUDE_MODEL."""
    import llm

    # Monkeypatch anthropic.Anthropic so no real HTTP call fires
    captured = {}

    class _FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def __iter__(self):
            return iter([])
        def get_final_message(self):
            class _Msg:
                content = []
                class usage:
                    input_tokens = 0
                    output_tokens = 0
            return _Msg()

    class _FakeMessages:
        def stream(self, **kwargs):
            captured["model"] = kwargs.get("model")
            return _FakeStream()

    class _FakeClient:
        messages = _FakeMessages()

    class _FakeAnthropic:
        def Anthropic(self, **kwargs):
            return _FakeClient()

    orig_anthropic = sys.modules.get("anthropic")
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeAnthropic().Anthropic
    sys.modules["anthropic"] = fake_module

    try:
        # Drain the generator with provider="claude" and no model arg
        list(llm.tool_complete_stream("claude", "sys", [], []))
        check("tool_complete_stream(no model) uses CLAUDE_MODEL (Sonnet)",
              captured.get("model") == config.CLAUDE_MODEL)
    finally:
        if orig_anthropic is not None:
            sys.modules["anthropic"] = orig_anthropic
        elif "anthropic" in sys.modules:
            del sys.modules["anthropic"]


# ---- 4. tool_complete_stream model=CLAUDE_MODEL_VOICE passes Haiku ----------

def test_tool_complete_stream_model_override():
    """When model=CLAUDE_MODEL_VOICE is passed, that model must be forwarded."""
    import llm

    captured = {}

    class _FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def __iter__(self):
            return iter([])
        def get_final_message(self):
            class _Msg:
                content = []
                class usage:
                    input_tokens = 0
                    output_tokens = 0
            return _Msg()

    class _FakeMessages:
        def stream(self, **kwargs):
            captured["model"] = kwargs.get("model")
            return _FakeStream()

    class _FakeClient:
        messages = _FakeMessages()

    class _FakeAnthropic:
        def Anthropic(self, **kwargs):
            return _FakeClient()

    orig_anthropic = sys.modules.get("anthropic")
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeAnthropic().Anthropic
    sys.modules["anthropic"] = fake_module

    try:
        list(llm.tool_complete_stream("claude", "sys", [], [],
                                      model=config.CLAUDE_MODEL_VOICE))
        check("tool_complete_stream(model=CLAUDE_MODEL_VOICE) passes Haiku",
              captured.get("model") == config.CLAUDE_MODEL_VOICE)
    finally:
        if orig_anthropic is not None:
            sys.modules["anthropic"] = orig_anthropic
        elif "anthropic" in sys.modules:
            del sys.modules["anthropic"]


# ---- 5. complete_stream_voice exists and uses CLAUDE_MODEL_VOICE ------------

def test_complete_stream_voice_exists_and_uses_haiku():
    """complete_stream_voice must exist and call the provider with CLAUDE_MODEL_VOICE."""
    import llm

    check("llm has complete_stream_voice function",
          callable(getattr(llm, "complete_stream_voice", None)))

    if not callable(getattr(llm, "complete_stream_voice", None)):
        return  # subsequent checks would crash

    captured = {}
    tokens_yielded = []

    class _FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def __iter__(self):
            # Yield a single text_delta event
            evt = types.SimpleNamespace(
                type="content_block_delta",
                delta=types.SimpleNamespace(type="text_delta", text="hello")
            )
            return iter([evt])
        def get_final_message(self):
            class _Msg:
                content = []
                class usage:
                    input_tokens = 1
                    output_tokens = 1
            return _Msg()

    class _FakeMessages:
        def stream(self, **kwargs):
            captured["model"] = kwargs.get("model")
            captured["max_tokens"] = kwargs.get("max_tokens")
            return _FakeStream()

    class _FakeClient:
        messages = _FakeMessages()

    class _FakeAnthropic:
        def Anthropic(self, **kwargs):
            return _FakeClient()

    orig_anthropic = sys.modules.get("anthropic")
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = _FakeAnthropic().Anthropic
    sys.modules["anthropic"] = fake_module

    orig_key = llm.ANTHROPIC_API_KEY
    llm.ANTHROPIC_API_KEY = "sk-test-fake"   # non-empty so the claude branch fires
    try:
        gen = llm.complete_stream_voice("system prompt",
                                        [{"role": "user", "content": "hello"}],
                                        _provider="claude")
        for tok in gen:
            tokens_yielded.append(tok)
        check("complete_stream_voice uses CLAUDE_MODEL_VOICE (Haiku)",
              captured.get("model") == config.CLAUDE_MODEL_VOICE)
        check("complete_stream_voice yields string tokens",
              all(isinstance(t, str) for t in tokens_yielded))
    finally:
        llm.ANTHROPIC_API_KEY = orig_key
        if orig_anthropic is not None:
            sys.modules["anthropic"] = orig_anthropic
        elif "anthropic" in sys.modules:
            del sys.modules["anthropic"]


# ---- 6. complete_stream_voice demo path does not crash ----------------------

def test_complete_stream_voice_demo_no_crash():
    """With provider=demo (no key), complete_stream_voice must not crash."""
    import llm
    if not callable(getattr(llm, "complete_stream_voice", None)):
        check("complete_stream_voice demo path skipped (fn absent)", False)
        return
    # provider is "demo" per os.environ["FIRSTBACK_PROVIDER"] = "demo" at top
    try:
        result = list(llm.complete_stream_voice("sys", [{"role": "user", "content": "hi"}]))
        check("complete_stream_voice with demo provider does not raise",
              isinstance(result, list))
    except Exception as exc:
        check(f"complete_stream_voice with demo provider raised: {exc}", False)


# ---- Run all ----------------------------------------------------------------

if __name__ == "__main__":
    print("Phase 5G Slice 2 -- llm.py + config.py voice additions")
    print("=" * 60)
    test_config_voice_constants()
    test_m4_prompt_constant()
    test_tool_complete_stream_no_model_regression()
    test_tool_complete_stream_model_override()
    test_complete_stream_voice_exists_and_uses_haiku()
    test_complete_stream_voice_demo_no_crash()
    print("-" * 60)
    print(f"RESULT: {_pass} passed, {_fail} failed")
    sys.exit(0 if _fail == 0 else 1)
