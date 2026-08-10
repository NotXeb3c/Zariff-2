"""Official Heliox Spotify Web API marketplace plugin."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SPOTIFY_API = "https://api.spotify.com/v1"


def _request(endpoint: str, *, method: str = "GET") -> dict:
    token = os.environ.get("SPOTIFY_ACCESS_TOKEN", "").strip()
    if not token:
        return {
            "error": (
                "SPOTIFY_ACCESS_TOKEN is required. The token needs "
                "user-read-playback-state and user-modify-playback-state scopes."
            )
        }
    request = urllib.request.Request(
        f"{SPOTIFY_API}{endpoint}",
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read()
            return json.loads(payload.decode("utf-8")) if payload else {"status": "success"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"error": f"Spotify API returned HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"error": f"Spotify API request failed: {exc}"}


def handle_tool(tool_name: str, params: dict) -> dict:
    """Execute a Spotify playback tool."""

    if tool_name == "spotify_play":
        return _request("/me/player/play", method="PUT")
    if tool_name == "spotify_pause":
        return _request("/me/player/pause", method="PUT")
    if tool_name == "spotify_now_playing":
        result = _request("/me/player/currently-playing")
        if "error" in result or "item" not in result:
            return result
        item = result["item"]
        return {
            "status": "success",
            "track": item.get("name", ""),
            "artists": [artist.get("name", "") for artist in item.get("artists", [])],
            "album": item.get("album", {}).get("name", ""),
            "is_playing": result.get("is_playing", False),
            "progress_ms": result.get("progress_ms"),
        }
    return {"error": f"Unknown tool: {tool_name}"}
