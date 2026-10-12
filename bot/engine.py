"""Calls the configured model with the docs + conversation and streams the answer back."""

import logging
from collections.abc import AsyncIterator

import anthropic

from bot import config
from bot.prompt import context_block, system_blocks

log = logging.getLogger(__name__)

REFUSAL_TEXT = (
    "Sorry, I can't help with that request. "
    f"Please contact {config.SUPPORT_CONTACT} for assistance."
)
ERROR_TEXT = (
    "Sorry, I'm having trouble answering right now. Please try again in a moment, "
    f"or contact {config.SUPPORT_CONTACT}."
)
CUT_SHORT_TEXT = "\n\n_(Answer was cut short. Ask me to continue.)_"


# ---------------------------------------------------------------- Claude

_client = anthropic.AsyncAnthropic() if config.PROVIDER == "anthropic" else None


def _request_kwargs(messages: list[dict], summary: str | None = None) -> dict:
    kwargs = dict(
        model=config.MODEL,
        max_tokens=config.MAX_TOKENS,
        system=system_blocks(summary),
        messages=messages,
        output_config={"effort": config.EFFORT},
    )
    if config.ENABLE_FALLBACKS:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return kwargs


async def _stream_claude(messages: list[dict], usage_out: dict | None, summary: str | None) -> AsyncIterator[str]:
    try:
        async with _client.beta.messages.stream(**_request_kwargs(messages, summary)) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
    except anthropic.RateLimitError:
        log.warning("Claude rate limit hit")
        yield ERROR_TEXT
        return
    except anthropic.APIStatusError as e:
        log.error("Claude API error %s: %s", e.status_code, e.message)
        yield ERROR_TEXT
        return
    except anthropic.APIConnectionError:
        log.error("Could not reach Claude API")
        yield ERROR_TEXT
        return

    if final.stop_reason == "refusal":
        yield ("\n\n" if final.content else "") + REFUSAL_TEXT
    elif final.stop_reason == "max_tokens":
        yield CUT_SHORT_TEXT

    if usage_out is not None:
        u = final.usage
        usage_out.update(
            model=final.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens,
            cache_creation_input_tokens=u.cache_creation_input_tokens,
        )


# ---------------------------------------------------------------- OpenAI-compatible (Gemini, Groq, Ollama, ...)

_oai_client = None
if config.PROVIDER == "openai_compatible":
    import openai

    _oai_client = openai.AsyncOpenAI(base_url=config.LLM_BASE_URL or None, api_key=config.LLM_API_KEY or "none")


async def _stream_openai_compatible(messages: list[dict], usage_out: dict | None,
                                    summary: str | None) -> AsyncIterator[str]:
    import openai

    # Same instructions + docs as Claude, as one system message (no explicit cache markers here;
    # providers that cache do it automatically on a repeated prefix).
    system = "\n\n".join(block["text"] for block in system_blocks(summary))
    finish_reason, usage = None, None
    try:
        stream = await _oai_client.chat.completions.create(
            model=config.MODEL,
            messages=[{"role": "system", "content": system}, *messages],
            max_tokens=config.MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.delta and choice.delta.content:
                    yield choice.delta.content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
    except openai.RateLimitError:
        log.warning("LLM rate limit hit (%s)", config.MODEL)
        yield ERROR_TEXT
        return
    except openai.APIStatusError as e:
        log.error("LLM API error %s: %s", e.status_code, e.message)
        yield ERROR_TEXT
        return
    except openai.APIConnectionError:
        log.error("Could not reach LLM endpoint %s", config.LLM_BASE_URL)
        yield ERROR_TEXT
        return

    if finish_reason == "length":
        yield CUT_SHORT_TEXT
    elif finish_reason == "content_filter":
        yield "\n\n" + REFUSAL_TEXT

    if usage_out is not None and usage is not None:
        usage_out.update(model=config.MODEL, input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens)


# ---------------------------------------------------------------- local Claude Code CLI (personal testing only)

_cc_workdir = None
_API_AUTH_VARS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}


def _claude_code_workdir():
    """Empty temp dir holding the system prompt, so the CLI loads no project settings."""
    global _cc_workdir
    if _cc_workdir is None:
        import tempfile
        from pathlib import Path

        _cc_workdir = Path(tempfile.mkdtemp(prefix="integration-bot-"))
        (_cc_workdir / "system.txt").write_text(
            "\n\n".join(block["text"] for block in system_blocks()), encoding="utf-8")
    return _cc_workdir


def _as_transcript(messages: list[dict], summary: str | None = None) -> str:
    """`claude -p` takes one prompt, so earlier turns (and the partner context, which can't go in
    the shared system-prompt file) are passed in the prompt itself."""
    context = context_block(summary)
    prefix = f"{context}\n\n" if context else ""
    if len(messages) == 1:
        return prefix + messages[0]["content"]
    turns = "\n\n".join(f"[{m['role'].upper()}]\n{m['content']}" for m in messages[:-1])
    return (f"{prefix}Conversation so far:\n\n{turns}\n\n"
            f"Now answer the partner's latest message:\n\n{messages[-1]['content']}")


async def _stream_claude_code(messages: list[dict], usage_out: dict | None,
                              summary: str | None) -> AsyncIterator[str]:
    import asyncio
    import json

    import os

    workdir = _claude_code_workdir()
    # .env puts ANTHROPIC_API_KEY into this process; the CLI would bill that API account instead
    # of the user's claude.ai subscription login, so API-auth variables are not passed on.
    env = {k: v for k, v in os.environ.items() if k not in _API_AUTH_VARS}
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "", "--strict-mcp-config", "--no-session-persistence",
            "--system-prompt-file", str(workdir / "system.txt"), "--model", config.CLAUDE_CODE_MODEL,
            cwd=workdir, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
        )
    except FileNotFoundError:
        log.error("Claude Code CLI ('claude') not found on PATH")
        yield ERROR_TEXT
        return

    proc.stdin.write(_as_transcript(messages, summary).encode())
    await proc.stdin.drain()
    proc.stdin.close()

    produced, result = False, None
    try:
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "stream_event":
                delta = event.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    produced = True
                    yield delta["text"]
            elif event.get("type") == "result":
                result = event
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()

    if result is None or result.get("is_error"):
        stderr = (await proc.stderr.read()).decode(errors="replace")[-500:]
        log.error("Claude Code CLI failed: %s %s", (result or {}).get("result", ""), stderr)
        if not produced:
            yield ERROR_TEXT
        return
    if usage_out is not None:
        u = result.get("usage") or {}
        usage_out.update(model=f"claude-code:{config.CLAUDE_CODE_MODEL}", input_tokens=u.get("input_tokens"),
                         output_tokens=u.get("output_tokens"),
                         cache_read_input_tokens=u.get("cache_read_input_tokens"),
                         cache_creation_input_tokens=u.get("cache_creation_input_tokens"))


# ---------------------------------------------------------------- entry point

_STREAMS = {
    "anthropic": _stream_claude,
    "openai_compatible": _stream_openai_compatible,
    "claude_code": _stream_claude_code,
}


async def stream_answer(messages: list[dict], usage_out: dict | None = None,
                        summary: str | None = None) -> AsyncIterator[str]:
    """Yield the answer text in chunks.

    `messages` is the full conversation (alternating user/assistant, ending with
    the user's question). `summary` is the partner's browser-kept session memory, if any.
    If `usage_out` is given, token usage is written into it.
    """
    stream = _STREAMS.get(config.PROVIDER, _stream_claude)
    async for text in stream(messages, usage_out, summary):
        yield text


async def complete(system: str, prompt: str, max_tokens: int = 800) -> str | None:
    """One short, non-streaming call (used to write session summaries). Returns None on failure."""
    try:
        if config.PROVIDER == "openai_compatible":
            r = await _oai_client.chat.completions.create(
                model=config.MODEL, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}])
            return (r.choices[0].message.content or "").strip() or None
        if config.PROVIDER == "claude_code":
            return await _complete_claude_code(system, prompt)
        r = await _client.messages.create(model=config.MODEL, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in r.content if b.type == "text").strip() or None
    except Exception:  # summaries are best-effort; never break the chat because of them
        log.exception("Summary call failed")
        return None


async def _complete_claude_code(system: str, prompt: str) -> str | None:
    import asyncio
    import json
    import os

    env = {k: v for k, v in os.environ.items() if k not in _API_AUTH_VARS}
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
        "--no-session-persistence", "--system-prompt", system, "--model", config.CLAUDE_CODE_MODEL,
        cwd=_claude_code_workdir(), env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=120)
    result = json.loads(out or b"{}")
    return None if result.get("is_error") else (result.get("result") or "").strip() or None
