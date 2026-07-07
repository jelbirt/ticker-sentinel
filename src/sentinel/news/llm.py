"""Claude CLI bridge for LLM news styles (subscription auth via `claude setup-token`).

Guardrails, in order of defense:
- exactly ONE subprocess call per run, no retries, no loops
- prompt hard-truncated before it leaves this module
- output capped via CLAUDE_CODE_MAX_OUTPUT_TOKENS + a subprocess timeout
- every failure mode (CLI missing, auth expired, rate-limited, timeout, empty
  output) returns None so the calling style FAILS OPEN to a deterministic
  renderer — the email is never blocked by the LLM path
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 6000
# Cap must comfortably exceed the answer (~300 tokens) or the CLI silently rolls
# into a continuation turn and -p returns ONLY the final chunk (front-truncation).
# Thinking is disabled below — a headline synthesis needs none — which also makes
# output length (and allowance draw) predictable.
MAX_OUTPUT_TOKENS = "4000"
TIMEOUT_SECONDS = 90


def _parse_response(stdout: str) -> str | None:
    """Parse --output-format stream-json (JSONL): concatenate the text of EVERY
    assistant message. The plain/result formats only carry the FINAL message, so
    when the model splits a long answer across messages (e.g. an output-token
    continuation) everything before the last chunk is silently lost — observed
    as front-truncated narratives. Tolerates plain text so a CLI format change
    degrades instead of breaking.
    """
    chunks: list[str] = []
    saw_json = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        saw_json = True
        if payload.get("type") == "assistant":
            for block in (payload.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    chunks.append(block["text"])
        elif payload.get("type") == "result" and payload.get("is_error"):
            log.warning(
                "claude returned an error result: %r", str(payload.get("result"))[:200]
            )
            return None
    if not saw_json:
        return stdout.strip() or None
    text = "".join(chunks).strip()  # continuations resume mid-sentence: join exactly
    return text or None


def call_claude(prompt: str, model: str, timeout: int = TIMEOUT_SECONDS) -> str | None:
    """One headless Claude call. Returns the text response, or None on any failure."""
    cmd = [
        "claude", "-p", "--model", model,
        "--output-format", "stream-json", "--verbose",  # stream-json requires verbose
        prompt[:MAX_PROMPT_CHARS],
    ]
    env = {
        **os.environ,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": MAX_OUTPUT_TOKENS,
        "MAX_THINKING_TOKENS": "0",  # no extended thinking for a summarization task
    }
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except FileNotFoundError:
        log.warning("claude CLI not installed — LLM news style unavailable")
        return None
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timed out after %ss", timeout)
        return None
    except Exception as exc:  # never let the LLM path break a run
        log.warning("claude CLI failed: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        # the CLI prints auth/config errors on stdout, not stderr — log both
        log.warning(
            "claude CLI exit %s: stderr=%r stdout=%r",
            result.returncode,
            (result.stderr or "")[:200],
            (result.stdout or "")[:200],
        )
        return None
    text = _parse_response(result.stdout)
    log.info(
        "claude responded: %s chars from %s-char prompt", len(text or ""), len(prompt)
    )
    return text
