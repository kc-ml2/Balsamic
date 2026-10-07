"""Explicit provider selection, activation, and billing identity.

Selecting a backend never enables it or supplies credentials. The default is
Codex with GPT6-sol, awaiting researcher configuration. API backends require an
explicit selection and activation; neither subscription errors nor quota limits
trigger a paid fallback.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse


def _number(name, default=None):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def api_key(base):
    if key := os.environ.get("GRATING_LLM_API_KEY", "").strip():
        return key
    filename = os.environ.get("GRATING_LLM_KEY_FILE")
    if not filename and base != "https://api.openai.com/v1":
        return ""
    try:
        path = Path(filename).expanduser() if filename else Path.cwd() / ".key"
        return path.read_text().strip() if path.is_file() and path.stat().st_size <= 8192 else ""
    except (OSError, UnicodeError):
        return ""


def provider_status():
    """Public, credential-free snapshot; no login or inference is performed."""
    # No provider is selected by default; research roles stay idle until one is chosen.
    selected = os.environ.get("GRATING_LLM_PROVIDER", "").strip() or "none"
    enabled = os.environ.get("GRATING_LLM_ENABLED", "").lower() in {"1", "true", "yes"}
    enabled = enabled and os.environ.get("GRATING_LLM_DISABLED", "").lower() not in {"1", "true", "yes"}
    model = os.environ.get("GRATING_LLM_MODEL", "" if selected == "none" else "gpt-6-sol").strip()
    common = {"provider": selected, "enabled": enabled, "model": model or None,
              "configured": False, "base_url": None, "local": False,
              "reasoning_effort": os.environ.get("GRATING_LLM_REASONING_EFFORT", "low"),
              "status_reason": "Awaiting configuration; model calls are disabled."}
    if selected == "none":
        common.update(transport=None, billing_mode="none", pricing_known=False, input_usd_per_million=None,
                      output_usd_per_million=None, pricing_basis="No model provider selected",
                      status_reason="No model provider selected. Set GRATING_LLM_PROVIDER to codex, openai_api or compatible to enable research roles.")
        return common
    if selected == "codex":
        binary = os.environ.get("GRATING_CODEX_BINARY", "codex")
        available = shutil.which(binary) is not None
        timeout = _number("GRATING_CODEX_TIMEOUT_SECONDS", 120)
        common.update(transport="codex_exec", billing_mode="subscription", binary=binary,
                      available=available, timeout_seconds=min(600, max(5, timeout or 120)),
                      pricing_known=False, input_usd_per_million=None, output_usd_per_million=None,
                      pricing_basis="Codex subscription allowance; API dollar rates do not apply.")
        if enabled:
            common.update(configured=bool(available and model),
                          status_reason="Codex selected; saved ChatGPT authentication is checked before each call."
                          if available and model else "Install Codex and select a model before enabling research.")
        return common
    common.update(billing_mode="api", transport="unsupported", pricing_known=False,
                  input_usd_per_million=None, output_usd_per_million=None, pricing_basis="unconfigured")
    if selected not in {"openai_api", "compatible"}:
        common["status_reason"] = "Unsupported provider; choose codex, openai_api, or compatible."
        return common
    base = os.environ.get("GRATING_LLM_BASE_URL", "https://api.openai.com/v1" if selected == "openai_api" else "").rstrip("/")
    parsed = urlparse(base)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    if selected == "openai_api":
        valid = valid and base == "https://api.openai.com/v1"
    common.update(base_url=base if valid else None, local=local,
                  transport="responses" if selected == "openai_api" else "chat_completions")
    default_pricing = selected == "openai_api" and model == "gpt-6-luna"
    input_price = _number("GRATING_LLM_INPUT_USD_PER_MILLION", .1 if default_pricing else None)
    output_price = _number("GRATING_LLM_OUTPUT_USD_PER_MILLION", .5 if default_pricing else None)
    common.update(input_usd_per_million=input_price, output_usd_per_million=output_price,
                  pricing_known=input_price is not None and output_price is not None,
                  pricing_basis="OpenAI standard gpt-6-luna pricing (2026-09-23)" if default_pricing else "configured environment")
    if enabled:
        ready = bool(valid and model and (local or api_key(base)))
        common.update(configured=ready, status_reason="API backend explicitly enabled." if ready else "Configure the selected API endpoint, model, and its credentials.")
    return common


def api_spend(usage):
    """API-only ledger total, with compatibility for pre-provider historical runs."""
    usage = usage or {}
    if usage.get("billing_mode") == "subscription":
        return 0.0
    return usage.get("api_cost_usd", usage.get("cost_usd")) or 0.0
