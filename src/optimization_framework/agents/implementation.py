"""Implementation-library adapter to lead-agent-owned persistent workers, not LLMAdapter."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from optimization_framework.contracts.base import content_hash
from .client import service_token


def review_context(context):
    """Keep protected results intact in the library while bounding Pi's review input.

    Full-grid candidate arrays are useful for machine checks, but cannot be read
    meaningfully by the reviewer and can exceed the Pi transport limit. Their
    digest and basic statistics let the reviewer identify the exact saved data.
    All check outcomes, scalar measurements, source and criteria remain present.
    """
    if not isinstance(context, dict) or not isinstance(context.get("report"), dict):
        return context

    def compact(value):
        if isinstance(value, list):
            if len(value) > 1024 and all(type(item) in (bool, int, float) for item in value):
                return {"array_digest": content_hash(value), "length": len(value),
                        "minimum": min(value), "maximum": max(value), "sum": sum(value)}
            return [compact(item) for item in value]
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items()}
        return value

    return {**context, "report": {**compact(context["report"]),
            "full_report_digest": content_hash(context["report"]),
            "array_note": "Large numeric arrays are represented by digest and statistics; the complete protected report remains in the implementation job."}}


class PiImplementationAdapter:
    def __init__(self, request, *, deadline_monotonic, progress, usage=None):
        self.request = request
        self.deadline = deadline_monotonic
        self.progress = progress
        self.usage = usage or {"calls": 0, "subscription_calls": 0, "billing_mode": "subscription", "api_cost_usd": 0}

    def transport(self, route, body=None):
        base = os.environ.get("GRATING_PI_WORKSPACE_URL", "http://127.0.0.1:8765")
        request = Request(base + "/api/internal/pi/implementation" + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + service_token(), "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("detail", "Pi assignment failed")
            except ValueError:
                detail = "Pi assignment failed"
            raise ValueError(detail) from None
        except (URLError, TimeoutError, OSError):
            raise ConnectionError("Pi workspace connection interrupted") from None

    def call(self, role, context, *, result_type, instructions):
        if role == "implementation_validator":
            context = review_context(context)
        request_id = content_hash([self.request.grant_id, role, context])[:32]
        payload = {"grant_id": self.request.grant_id, "request_id": request_id, "role": role,
            "instructions": instructions + "\nUse isolated workspace tools for development. Submit the final structured result through submit_output.",
            "context": context, "output_schema": result_type.model_json_schema(),
            "deadline_at": time.time() + max(0, self.deadline - time.monotonic())}
        submitted = None
        while time.monotonic() < self.deadline:
            self.progress()
            try:
                if submitted is None:
                    submitted = self.transport("", payload)
                current = self.transport("/" + submitted["run_id"])
                accounting = self.usage.setdefault("agent_usage", {})
                accounting[current["agent_id"]] = current.get("usage", {})
                for key in ("calls", "input", "output", "cacheRead", "cacheWrite"):
                    self.usage[key] = sum(value.get(key, 0) for value in accounting.values())
                self.usage["subscription_calls"] = self.usage["calls"]
                if current["status"] == "completed":
                    if current.get("output") is None:
                        raise ValueError("Pi worker completed without the required structured output; saved work is retained")
                    self.usage["agent_runs"] = list(dict.fromkeys([*self.usage.get("agent_runs", []), submitted["run_id"]]))
                    return result_type.model_validate(current["output"])
                if current["status"] in {"failed", "stopped", "interrupted", "paused"}:
                    raise ValueError(current.get("error") or "Pi worker interrupted; inspect its saved artifacts")
            except ConnectionError:
                pass  # Stable request identity reconciles a lost POST reply.
            time.sleep(.5)
        raise ValueError("Implementation time allocation exhausted; Pi work and tool receipts are preserved")
