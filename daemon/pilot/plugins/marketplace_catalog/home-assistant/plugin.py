"""Official Heliox Home Assistant marketplace plugin."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _settings() -> tuple[str, str]:
    return (
        os.environ.get("HA_URL", "").strip().rstrip("/"),
        os.environ.get("HA_TOKEN", "").strip(),
    )


def _request(endpoint: str, *, method: str = "GET", body: dict | None = None) -> dict | list:
    url, token = _settings()
    if not url or not token:
        return {"error": "HA_URL and HA_TOKEN environment variables are required"}
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{url}/api{endpoint}",
        method=method,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {"status": "success"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"error": f"Home Assistant returned HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"error": f"Home Assistant request failed: {exc}"}


def handle_tool(tool_name: str, params: dict) -> dict:
    """Execute one Home Assistant tool."""

    if tool_name == "ha_lights":
        states = _request("/states")
        if isinstance(states, dict):
            return states
        lights = [state for state in states if str(state.get("entity_id", "")).startswith("light.")]
        return {"status": "success", "lights": lights, "count": len(lights)}

    if tool_name == "ha_set_light":
        entity_id = str(params.get("entity_id") or "").strip()
        state = str(params.get("state") or "").strip().lower()
        if not entity_id.startswith("light."):
            return {"error": "entity_id must start with 'light.'"}
        if state not in {"on", "off"}:
            return {"error": "state must be 'on' or 'off'"}
        result = _request(
            f"/services/light/turn_{state}",
            method="POST",
            body={"entity_id": entity_id},
        )
        if isinstance(result, dict) and "error" in result:
            return result
        return {"status": "success", "entity_id": entity_id, "state": state}

    return {"error": f"Unknown tool: {tool_name}"}
