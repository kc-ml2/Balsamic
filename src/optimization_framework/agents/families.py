"""LLM families: an agent never continues its session on a different model family.

Transcripts, tool-call conventions and reasoning traces differ between vendors, so
continuing a conversation written by one family with another is refused. Switching
models within a family is allowed (and recorded). Different agents of one campaign may
use different families, e.g. through model tiers.
"""
import re

# Providers that serve one vendor's own models.
PROVIDER_FAMILIES = {
    "openai": "openai", "openai-codex": "openai", "azure-openai": "openai", "azure-openai-responses": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "google": "google", "google-gemini-cli": "google", "google-vertex": "google", "gemini": "google",
    "deepseek": "deepseek", "mistral": "mistral", "xai": "xai", "zai": "zhipu", "zai-coding-cn": "zhipu",
}
# Model-name patterns for routers and compatible endpoints (OpenRouter, gateways, local servers).
MODEL_PATTERNS = [
    (r"deepseek", "deepseek"), (r"claude|anthropic", "anthropic"), (r"gemini|gemma", "google"),
    (r"(^|/)(gpt|o[1-9]|codex|chatgpt)|openai", "openai"), (r"qwen", "qwen"), (r"llama|meta-", "meta"),
    (r"mistral|mixtral|codestral|devstral", "mistral"), (r"grok", "xai"), (r"glm|zhipu", "zhipu"),
    (r"kimi|moonshot", "moonshot"), (r"minimax", "minimax"),
]


def family_of(provider, model):
    """Vendor lineage of a model; unknown models form their own provider-scoped family."""
    if provider in PROVIDER_FAMILIES:
        return PROVIDER_FAMILIES[provider]
    name = (model or "").lower()
    for pattern, family in MODEL_PATTERNS:
        if re.search(pattern, name):
            return family
    return f"{provider}:{model}"


def check_same_family(campaign_family, provider, model):
    family = family_of(provider, model)
    if campaign_family and family != campaign_family:
        raise ValueError(f"This campaign runs on the {campaign_family} model family; {provider}/{model} is {family}. "
                         "Continuing a campaign with another model family is not allowed. Start a new campaign instead.")
    return family
