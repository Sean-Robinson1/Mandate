"""LLM adapter. The LLM only interprets text and words explanations; it never decides what is allowed.

Providers are swappable via LLM_PROVIDER in .env (models via the matching *_MODEL variable):
  auto        default: the first of claude, openai, gemini (by API key), then claude_cli, codex_cli (by CLI on PATH),
              else none
  claude      Anthropic API (ANTHROPIC_API_KEY, ANTHROPIC_MODEL)
  claude_cli  local Claude Code CLI (`claude -p`), using the user's subscription (CLAUDE_CLI_MODEL)
  openai      OpenAI Chat Completions API (OPENAI_API_KEY, OPENAI_MODEL)
  codex_cli   local Codex CLI (`codex exec`), using the user's ChatGPT login (CODEX_CLI_MODEL)
  codex       codex_cli if `codex` is on PATH, else openai
  gemini      Gemini API (GEMINI_API_KEY, GEMINI_MODEL)
  none        no LLM; the rule-based parser handles everything
Every failure raises LLMError, and callers fall back to the rule-based parser.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Protocol

import httpx

import settings as config


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, prompt: str) -> str: ...


def _run_cli(cmd: list[str], prompt: str, timeout: float, what: str) -> subprocess.CompletedProcess:
    try:
        # Run from an empty temp dir so the CLI does not load this project's context.
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, cwd=tempfile.gettempdir())
    except (subprocess.TimeoutExpired, OSError) as e:
        raise LLMError(f"{what} failed: {e}") from e
    if proc.returncode != 0:
        raise LLMError(f"{what} exit {proc.returncode}: {(proc.stderr or '')[:300]}")
    return proc


def _post(url: str, headers: dict, body: dict, timeout: float, what: str, pick) -> str:
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        return pick(r.json()).strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise LLMError(f"{what} call failed: {e}") from e


class ClaudeCLIClient:
    def __init__(self, model: str | None = None, timeout: float = 90.0):
        self.model = model or config.CLAUDE_CLI_MODEL
        self.name = f"claude_cli:{self.model}"
        self.timeout = timeout
        self._exe = shutil.which("claude")

    def complete(self, prompt: str) -> str:
        if not self._exe:
            raise LLMError("claude CLI not found on PATH")
        cmd = [self._exe, "-p", "--model", self.model, "--output-format", "text"]
        return _run_cli(cmd, prompt, self.timeout, "claude CLI").stdout.strip()


class CodexCLIClient:
    def __init__(self, model: str | None = None, timeout: float = 120.0):
        self.model = model or config.CODEX_CLI_MODEL
        self.name = f"codex_cli:{self.model}" if self.model else "codex_cli"
        self.timeout = timeout
        self._exe = shutil.which("codex")

    def complete(self, prompt: str) -> str:
        if not self._exe:
            raise LLMError("codex CLI not found on PATH")
        # Progress goes to stderr; -o writes only the final answer, so read that file.
        fd, out = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            cmd = [self._exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", out]
            if self.model:
                cmd += ["-m", self.model]
            proc = _run_cli(cmd + ["-"], prompt, self.timeout, "codex CLI")
            with open(out, encoding="utf-8") as f:
                return (f.read() or proc.stdout).strip()
        finally:
            os.unlink(out)


class AnthropicClient:
    def __init__(self, model: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model or config.ANTHROPIC_MODEL
        self.api_key = api_key or config.ANTHROPIC_API_KEY
        self.name = f"claude:{self.model}"
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY must be set")
        return _post("https://api.anthropic.com/v1/messages",
                     {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                     {"model": self.model, "max_tokens": 1024, "messages": [{"role": "user", "content": prompt}]},
                     self.timeout, "Anthropic",
                     lambda d: "".join(b["text"] for b in d["content"] if b.get("type") == "text"))


class OpenAIClient:
    def __init__(self, model: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model or config.OPENAI_MODEL
        self.api_key = api_key or config.OPENAI_API_KEY
        self.name = f"openai:{self.model}"
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY must be set")
        return _post("https://api.openai.com/v1/chat/completions",
                     {"Authorization": f"Bearer {self.api_key}"},
                     {"model": self.model, "messages": [{"role": "user", "content": prompt}]},
                     self.timeout, "OpenAI", lambda d: d["choices"][0]["message"]["content"])


class GeminiClient:
    def __init__(self, model: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model or config.GEMINI_MODEL
        self.api_key = api_key or config.GEMINI_API_KEY
        self.name = f"gemini:{self.model}"
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        if not (self.api_key and self.model):
            raise LLMError("GEMINI_API_KEY and GEMINI_MODEL must be set")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        return _post(url, {"x-goog-api-key": self.api_key}, {"contents": [{"parts": [{"text": prompt}]}]},
                     self.timeout, "Gemini", lambda d: d["candidates"][0]["content"]["parts"][0]["text"])


def pick_provider(provider: str | None = None) -> str:
    """Resolve 'auto' and 'codex' to a concrete provider name; anything unknown means 'none'."""
    provider = (provider or config.LLM_PROVIDER or "auto").strip().lower()
    if provider == "codex":
        return "codex_cli" if shutil.which("codex") else "openai"
    if provider == "auto":
        for name, available in (("claude", config.ANTHROPIC_API_KEY), ("openai", config.OPENAI_API_KEY),
                                ("gemini", config.GEMINI_API_KEY), ("claude_cli", shutil.which("claude")),
                                ("codex_cli", shutil.which("codex"))):
            if available:
                return name
        return "none"
    return provider if provider in CLIENTS else "none"


CLIENTS = {"claude": AnthropicClient, "claude_cli": ClaudeCLIClient, "openai": OpenAIClient,
           "codex_cli": CodexCLIClient, "gemini": GeminiClient}


def get_llm(provider: str | None = None) -> LLMClient | None:
    client = CLIENTS.get(pick_provider(provider))
    return client() if client else None


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates code fences and chatter around it)."""
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise LLMError("no JSON object in LLM reply")
