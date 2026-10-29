"""Which LLM each ADK agent runs on, chosen by model name in .env.

`claude-*` names run through ADK's AnthropicLlm (official `anthropic` SDK, Claude API,
ANTHROPIC_API_KEY); anything else is passed to ADK as a Gemini model name. Per-model
quirks live here so the agents don't need to know them:

- Claude Sonnet 5.5 / Opus 5.5: thinking is always on (disabling it is a 400) and forced
  tool use is a 400. Asking for adaptive thinking explicitly makes ADK deliver an
  `output_schema` through an ordinary tool call instead of a forced one. Effort `low`
  keeps them fast; sampling params (temperature) are rejected, so none are sent.
- Claude Haiku 4.5: no thinking, no effort parameter (it errors); forced tool use works,
  so `output_schema` is fine as is.
- No `temperature` for any Claude model: the anthropic SDK 2.x `messages.create()` no
  longer takes it (TypeError). The output schema is what keeps verdicts consistent.
- Gemini: thinking level LOW; temperature 0 for classifiers.
"""
from __future__ import annotations

from google.genai import types


def is_claude(name: str) -> bool:
    return name.startswith("claude-")


def llm(name: str, *, max_retries: int | None = None, timeout_s: float | None = None):
    """The `model=` value for an LlmAgent. max_retries=0 turns off the SDK's silent
    retries, for callers that retry themselves and report each attempt (SPEC §16).
    timeout_s bounds one call: the SDK default is 10 minutes, so a hung call outlived the
    pipeline's 15 s guard timeout before the guard's own retry could run (eval run 7)."""
    if not is_claude(name):
        return name
    from anthropic import AsyncAnthropic
    from google.adk.models.anthropic_llm import AnthropicLlm

    opts = {k: v for k, v in (("max_retries", max_retries), ("timeout", timeout_s)) if v is not None}
    client = AsyncAnthropic(**opts) if opts else None
    return AnthropicLlm(model=name, client=client)


def gen_config(name: str, *, classifier: bool) -> types.GenerateContentConfig:
    """generate_content_config for an LlmAgent: `classifier` for the guard agents."""
    if is_claude(name):
        from google.adk.models.anthropic_llm import AnthropicGenerateContentConfig

        if name.startswith("claude-haiku-4-5"):
            return AnthropicGenerateContentConfig()
        return AnthropicGenerateContentConfig(
            effort="low",
            thinking_config=types.ThinkingConfig(thinking_budget=-1),   # adaptive
        )
    return types.GenerateContentConfig(
        temperature=0 if classifier else None,
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        # guards retry themselves; Google's minimum per-call deadline is 10 s
        http_options=types.HttpOptions(timeout=10_000, retry_options=types.HttpRetryOptions(attempts=1))
        if classifier else None,
    )


def api_error(exc: BaseException) -> tuple[int | None, float, str] | None:
    """(HTTP status or None, suggested wait in s, short label) for a model-provider API
    error, or None if `exc` is not one. Lets callers retry the same way for both providers."""
    from google.genai import errors as genai_errors
    if isinstance(exc, genai_errors.APIError):
        wait = 0.0
        details = exc.details.get("error", {}).get("details", []) if isinstance(exc.details, dict) else []
        for d in details:   # Google's RetryInfo: a daily-quota 429 says ~16 h
            if str(d.get("@type", "")).endswith("RetryInfo"):
                try:
                    wait = float(str(d.get("retryDelay", "0s")).rstrip("s"))
                except ValueError:
                    pass
        return exc.code, wait, f"{exc.code} {exc.status}"
    try:
        import anthropic
    except ImportError:
        return None
    if isinstance(exc, anthropic.APIStatusError):
        try:
            wait = float(exc.response.headers.get("retry-after", 0))
        except ValueError:
            wait = 0.0
        kind = (exc.body or {}).get("error", {}).get("type", "") if isinstance(exc.body, dict) else ""
        return exc.status_code, wait, f"{exc.status_code} {kind}".strip()
    if isinstance(exc, anthropic.APIConnectionError):   # includes timeouts: worth one more try
        return 503, 0.0, f"connection error ({type(exc).__name__})"
    return None
