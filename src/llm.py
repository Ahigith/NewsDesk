"""Pluggable LLM backend. Pick a provider with the LLM_PROVIDER env var.

Every provider here except Anthropic exposes an OpenAI-compatible
/chat/completions endpoint, so one code path covers all of them and the project
needs no vendor SDK -- just `requests`.

Why free tiers are genuinely enough here: after deduping and the article cap,
a daily run makes roughly 10-15 requests totalling well under 200k tokens.
Every provider below allows that many times over. This is not a case of
squeezing a production workload into a free tier; the workload is simply small.

    LLM_PROVIDER=groq        (default)  free, fast, no card
    LLM_PROVIDER=gemini                 free tier, no card
    LLM_PROVIDER=openrouter             free `:free` models, no card
    LLM_PROVIDER=cerebras               free tier, no card
    LLM_PROVIDER=anthropic              paid, best quality
    LLM_PROVIDER=none                   no LLM at all, rule-based scoring
"""
from __future__ import annotations

import json
import os
import random
import re
import time

import requests

TIMEOUT = 120

PROVIDERS = {
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "batch": 12,
        "json_mode": True,
        "signup": "https://console.groq.com/keys",
        "limits": "free tier, ~30 req/min and a generous daily token budget",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash",
        "batch": 20,
        "json_mode": True,
        "signup": "https://aistudio.google.com/apikey",
        "limits": "free tier, ~15 req/min, daily request cap",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "batch": 12,
        "json_mode": False,  # varies by underlying model; safer to parse loosely
        "signup": "https://openrouter.ai/keys",
        "limits": "free `:free` models, low daily request cap",
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "llama-3.3-70b",
        "batch": 12,
        "json_mode": True,
        "signup": "https://cloud.cerebras.ai",
        "limits": "free tier, daily token budget",
    },
    "anthropic": {
        "url": "https://api.anthropic.com/v1/messages",
        "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-haiku-4-5-20251001",
        "batch": 20,
        "json_mode": False,  # uses assistant prefill instead
        "signup": "https://console.anthropic.com",
        "limits": "paid, roughly $3-5/month at this volume",
    },
}


class LLMUnavailable(RuntimeError):
    """Raised when the chosen provider has no key -- caller falls back to rules."""


def active_provider() -> tuple[str, dict] | tuple[None, None]:
    name = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    if name in ("none", "off", ""):
        return None, None
    if name not in PROVIDERS:
        print(f"!! unknown LLM_PROVIDER={name!r}; falling back to rule-based scoring")
        return None, None
    cfg = dict(PROVIDERS[name])
    cfg["model"] = os.environ.get("LLM_MODEL", "").strip() or cfg["model"]
    cfg["batch"] = int(os.environ.get("LLM_BATCH_SIZE", "") or cfg["batch"])
    cfg["name"] = name
    if not os.environ.get(cfg["key_env"], "").strip():
        print(
            f"!! LLM_PROVIDER={name} but {cfg['key_env']} is not set.\n"
            f"   Get a key at {cfg['signup']} ({cfg['limits']}).\n"
            f"   Falling back to rule-based scoring for this run."
        )
        return None, None
    return name, cfg


def _post(cfg: dict, payload: dict, headers: dict) -> dict:
    """POST with backoff. Free tiers rate-limit; 429 is expected, not exceptional."""
    delay = 4.0
    last = None
    for attempt in range(5):
        try:
            r = requests.post(cfg["url"], json=payload, headers=headers, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("retry-after") or delay)
                print(f"    rate-limited ({r.status_code}); waiting {wait:.0f}s")
                time.sleep(min(wait, 60) + random.uniform(0, 2))
                delay *= 2
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"provider {cfg['name']} failed after retries: {last}")


def complete_json(cfg: dict, system: str, user: str, max_tokens: int = 8000) -> str:
    """Send one prompt, return the raw text the model produced."""
    key = os.environ[cfg["key_env"]].strip()

    if cfg["name"] == "anthropic":
        body = _post(
            cfg,
            {
                "model": cfg["model"],
                "max_tokens": max_tokens,
                "system": system,
                "messages": [
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": "{"},  # prefill forces raw JSON
                ],
            },
            {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        return "{" + body["content"][0]["text"]

    payload = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if cfg.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if cfg["name"] == "openrouter":
        headers["HTTP-Referer"] = "https://github.com"
        headers["X-Title"] = "newsdesk"

    body = _post(cfg, payload, headers)
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected response shape from {cfg['name']}: {str(body)[:300]}")


# --------------------------------------------------------------------------
# Tolerant JSON extraction. Smaller free models wrap JSON in prose or markdown
# fences, or truncate mid-array. All three are recoverable.
# --------------------------------------------------------------------------
FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
OBJ_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}")


def extract_results(text: str) -> list[dict]:
    """Pull a list of result objects out of whatever the model returned."""
    if not text:
        return []
    fenced = FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    text = text.strip()

    # Happy path: a well-formed object or array.
    for candidate in (text, text + "}", text + "]}", text + '"}]}'):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            for key in ("results", "articles", "items", "data", "scores"):
                if isinstance(parsed.get(key), list):
                    return [x for x in parsed[key] if isinstance(x, dict)]
            return [parsed] if "i" in parsed else []

    # Salvage: scrape whole objects out of a truncated or prose-wrapped response.
    out = []
    for match in OBJ_RE.findall(text):
        try:
            obj = json.loads(match)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "i" in obj:
            out.append(obj)
    return out
