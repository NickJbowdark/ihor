"""Provider-agnostic LLM client for any OpenAI-compatible Chat Completions API.

Works with (configure in .env, see .env.example):
  * DeepSeek API      GIM_LLM_PROVIDER=deepseek  GIM_LLM_BASE_URL=https://api.deepseek.com
  * Ollama (local)    GIM_LLM_PROVIDER=ollama    GIM_LLM_BASE_URL=http://localhost:11434/v1
  * any other OpenAI-compatible endpoint (GIM_LLM_PROVIDER=openai_compatible)

The model id is NEVER hard-coded: copy it from the provider's documentation
(DeepSeek) or from `ollama list` (Ollama) into GIM_LLM_MODEL.

Safety and cost controls:
  * monthly budget cap (GIM_LLM_MONTHLY_BUDGET_USD) checked BEFORE every call with a
    pessimistic estimate; every call (ok or failed) is written to llm_calls
  * prices per million tokens come from .env (you copy them from the pricing page);
    if they are unknown (0) for a paid provider, calls are refused unless you set them
  * the API key is only sent as an Authorization header and is redacted from all errors
  * JSON output is requested and parsed defensively
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from gim.security import redact
from gim.storage import intel

PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "needs_key": True, "paid": True},
    "ollama": {"base_url": "http://localhost:11434/v1", "needs_key": False, "paid": False},
    "openai_compatible": {"base_url": "", "needs_key": True, "paid": True},
}
CHARS_PER_TOKEN = 3.5  # conservative estimate for budgeting before the call


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class NotConfigured(LLMError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    monthly_budget_usd: float
    price_input_per_m: float
    price_output_per_m: float
    timeout_s: float
    max_output_tokens: int
    temperature: float
    json_mode: bool

    @property
    def enabled(self) -> bool:
        return self.provider not in ("", "none")

    def problems(self) -> list[str]:
        """Configuration problems, in plain language (empty = ready)."""
        if not self.enabled:
            return ["no AI provider configured (GIM_LLM_PROVIDER=none) -- the non-AI baseline is used"]
        out = []
        if self.provider not in PRESETS:
            out.append(f"unknown GIM_LLM_PROVIDER {self.provider!r}; use deepseek, ollama or openai_compatible")
            return out
        if not self.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            out.append("GIM_LLM_BASE_URL must be https:// (or http://localhost for Ollama)")
        if not self.model:
            out.append("GIM_LLM_MODEL is empty -- copy the exact model id from the provider docs / `ollama list`")
        if PRESETS[self.provider]["needs_key"] and not self.api_key:
            out.append("GIM_LLM_API_KEY is empty")
        if PRESETS[self.provider]["paid"] and (self.price_input_per_m <= 0 or self.price_output_per_m <= 0):
            out.append("set GIM_LLM_PRICE_INPUT_PER_M and GIM_LLM_PRICE_OUTPUT_PER_M (USD per million tokens, "
                       "from the provider's pricing page) so the budget cap can work")
        return out

    def estimate_cost(self, prompt_chars: int, output_tokens: int | None = None) -> float:
        tokens_in = prompt_chars / CHARS_PER_TOKEN
        tokens_out = output_tokens if output_tokens is not None else self.max_output_tokens
        return (tokens_in * self.price_input_per_m + tokens_out * self.price_output_per_m) / 1_000_000


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def load_config() -> LLMConfig:
    provider = os.getenv("GIM_LLM_PROVIDER", "none").strip().lower() or "none"
    preset = PRESETS.get(provider, {"base_url": ""})
    return LLMConfig(
        provider=provider,
        base_url=(os.getenv("GIM_LLM_BASE_URL", "").strip() or preset["base_url"]).rstrip("/"),
        model=os.getenv("GIM_LLM_MODEL", "").strip(),
        api_key=os.getenv("GIM_LLM_API_KEY", "").strip(),
        monthly_budget_usd=_f("GIM_LLM_MONTHLY_BUDGET_USD", 5.0),
        price_input_per_m=_f("GIM_LLM_PRICE_INPUT_PER_M", 0.0),
        price_output_per_m=_f("GIM_LLM_PRICE_OUTPUT_PER_M", 0.0),
        timeout_s=_f("GIM_LLM_TIMEOUT_S", 180.0),
        max_output_tokens=int(_f("GIM_LLM_MAX_OUTPUT_TOKENS", 4000)),
        temperature=_f("GIM_LLM_TEMPERATURE", 0.2),
        json_mode=os.getenv("GIM_LLM_JSON_MODE", "true").strip().lower() not in ("0", "false", "no"),
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output (tolerates ``` fences and leading prose)."""
    t = _FENCE.sub("", (text or "").strip())
    try:
        obj = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("model output contains no JSON object") from None
        try:
            obj = json.loads(t[start:end + 1])
        except ValueError as exc:
            raise LLMError(f"model output is not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise LLMError("model output JSON is not an object")
    return obj


class LLMClient:
    def __init__(self, config: LLMConfig, conn, session: requests.Session | None = None):
        self.config = config
        self.conn = conn
        self.session = session or requests.Session()

    def check_ready(self) -> None:
        problems = self.config.problems()
        if problems:
            raise NotConfigured("; ".join(problems))

    def chat_json(self, messages: list[dict[str, str]], purpose: str) -> dict[str, Any]:
        """One chat call returning a parsed JSON object. Enforces the budget and logs cost."""
        self.check_ready()
        cfg = self.config
        prompt_chars = sum(len(m["content"]) for m in messages)
        spent = intel.month_spend(self.conn)["cost_usd"]
        estimate = cfg.estimate_cost(prompt_chars)
        if spent + estimate > cfg.monthly_budget_usd:
            raise BudgetExceeded(f"monthly AI budget ${cfg.monthly_budget_usd:.2f} would be exceeded "
                                 f"(spent ${spent:.4f}, this call up to ${estimate:.4f})")
        body: dict[str, Any] = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature,
                                "max_tokens": cfg.max_output_tokens, "stream": False}
        if cfg.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        started = time.monotonic()
        usage_in = usage_out = 0
        try:
            data = self._post(f"{cfg.base_url}/chat/completions", body, headers)
            usage = data.get("usage") or {}
            usage_in = int(usage.get("prompt_tokens") or prompt_chars / CHARS_PER_TOKEN)
            usage_out = int(usage.get("completion_tokens") or 0)
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("response has no choices")
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if choices[0].get("finish_reason") == "length":
                raise LLMError("model output was cut off (max tokens) -- raise GIM_LLM_MAX_OUTPUT_TOKENS")
            result = parse_json_object(content)
        except Exception as exc:
            err = redact(str(exc))[:500]
            intel.record_llm_call(self.conn, provider=cfg.provider, model=cfg.model, purpose=purpose,
                                  input_tokens=usage_in, output_tokens=usage_out,
                                  cost_usd=self._cost(usage_in, usage_out), ok=False, error=err,
                                  duration_ms=int((time.monotonic() - started) * 1000))
            raise LLMError(err) from None
        intel.record_llm_call(self.conn, provider=cfg.provider, model=cfg.model, purpose=purpose,
                              input_tokens=usage_in, output_tokens=usage_out, cost_usd=self._cost(usage_in, usage_out),
                              ok=True, duration_ms=int((time.monotonic() - started) * 1000))
        return result

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return round((tokens_in * self.config.price_input_per_m + tokens_out * self.config.price_output_per_m) / 1e6, 6)

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        delay = 5.0
        for attempt in (1, 2, 3):
            try:
                resp = self.session.post(url, json=body, headers=headers, timeout=self.config.timeout_s)
            except requests.RequestException as exc:
                if attempt == 3:
                    raise LLMError(f"cannot reach AI provider: {exc}") from None
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(float(resp.headers.get("Retry-After", delay) or delay), 60.0))
                delay *= 2
                continue
            if resp.status_code == 401:
                raise LLMError("AI provider rejected the API key (HTTP 401)")
            if resp.status_code == 402:
                raise LLMError("AI provider reports insufficient balance (HTTP 402)")
            if resp.status_code != 200:
                raise LLMError(f"AI provider HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except ValueError:
                raise LLMError("AI provider returned non-JSON HTTP body") from None
        raise LLMError("unreachable")  # pragma: no cover
