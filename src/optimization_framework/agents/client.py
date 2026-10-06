"""Authenticated local Pi transport. No provider or credential fallback."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def service_token():
    filename = os.environ.get("GRATING_PI_TOKEN_FILE")
    if not filename:
        raise ValueError("Pi service is not configured")
    return Path(filename).read_text().strip()


class PiClient:
    def request(self, method, route, body=None):
        url = os.environ.get("GRATING_PI_URL", "http://127.0.0.1:8768")
        request = Request(url + route, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + service_token(), "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            try:
                detail = json.load(exc).get("detail", "Pi request failed")
            except ValueError:
                detail = "Pi request failed"
            raise ValueError(detail) from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("Pi service is unavailable; work is saved and will reconnect") from None

    def status(self):
        return self.request("GET", "/v1/status")

    def inspect(self, agent_id, after=0):
        return self.request("GET", f"/v1/agents/{agent_id}?after={after}")

    def submit(self, agent, run):
        return self.request("POST", f"/v1/agents/{agent['id']}/runs", {
            "run_id": run["id"], "text": run["input"], "mode": run.get("mode", "follow_up"),
            "deadline_at": agent.get("deadline_at"),
            "spec": {"provider": agent.get("provider") or "openai-codex", "model": agent["model"],
                     "effort": agent["reasoning_effort"], "role": agent["role"]}})

    def control(self, agent_id, action):
        return self.request("POST", f"/v1/agents/{agent_id}/control", {"action": action})
