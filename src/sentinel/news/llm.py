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

import logging
import os
import subprocess

log = logging.getLogger(__name__)

MAX_PROMPT_CHARS = 6000
MAX_OUTPUT_TOKENS = "1200"
TIMEOUT_SECONDS = 90


def call_claude(prompt: str, model: str, timeout: int = TIMEOUT_SECONDS) -> str | None:
    """One headless Claude call. Returns the text response, or None on any failure."""
    cmd = ["claude", "-p", "--model", model, prompt[:MAX_PROMPT_CHARS]]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": MAX_OUTPUT_TOKENS}
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
        log.warning(
            "claude CLI exit %s: %s", result.returncode, (result.stderr or "")[:200]
        )
        return None
    return result.stdout.strip()
