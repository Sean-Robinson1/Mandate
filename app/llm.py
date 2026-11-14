"""LLM adapter. The LLM only interprets text and words explanations; it never decides what is allowed.

Providers are swappable via LLM_PROVIDER:
  claude_cli  local development: shells out to the Claude Code CLI (`claude -p`), using the user's subscription
  gemini      Gemini API (GEMINI_API_KEY, GEMINI_MODEL)
  none        no LLM; the rule-based parser handles everything
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


class ClaudeCLIClient:
    def __init__(self, model: str | None = None, timeout: float = 90.0):
        self.model = model or config.CLAUDE_CLI_MODEL
        self.name = f"claude_cli:{self.model}"
        self.timeout = timeout
        self._exe = shutil.which("claude")

    def complete(self, prompt: str) -> str:
        if not self._exe:
            raise LLMError("claude CLI not found on PATH")
        try:
            # Run from an empty temp dir so the CLI does not load this project's context.
            proc = subprocess.run(
                [self._exe, "-p", "--model", self.model, "--output-format", "text"],
                input=prompt, capture_output=True, text=True, encoding="utf-8",
                timeout=self.timeout, cwd=tempfile.gettempdir(),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise LLMError(f"claude CLI failed: {e}") from e
        if proc.returncode != 0:
            raise LLMError(f"claude CLI exit {proc.returncode}: {proc.stderr[:300]}")
        return proc.stdout.strip()


class GeminiClient:
    def __init__(self, model: str | None = None, api_key: str | None = None, timeout: float = 30.0):
        self.model = model or os.getenv("GEMINI_MODEL", "")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.name = f"gemini:{self.model}"
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        if not (self.api_key and self.model):
            raise LLMError("GEMINI_API_KEY and GEMINI_MODEL must be set")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        try:
            r = httpx.post(url, headers={"x-goog-api-key": self.api_key}, timeout=self.timeout,
                           json={"contents": [{"parts": [{"text": prompt}]}]})
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise LLMError(f"Gemini call failed: {e}") from e


def get_llm(provider: str | None = None) -> LLMClient | None:
    provider = (provider or config.LLM_PROVIDER).lower()
    if provider == "claude_cli":
        return ClaudeCLIClient()
    if provider == "gemini":
        return GeminiClient()
    return None


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
