"""Official Heliox weather marketplace plugin."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request


def _weather(city: str) -> dict:
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
    request = urllib.request.Request(url, headers={"User-Agent": "Heliox-OS-Agent"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def handle_tool(tool_name: str, params: dict) -> dict:
    """Execute a weather tool using live wttr.in data."""

    city = str(params.get("city") or "").strip()
    if not city:
        return {"error": "city is required"}

    try:
        data = _weather(city)
    except Exception as exc:
        return {"error": f"Live weather request failed: {exc}"}

    if tool_name == "get_weather":
        current = data.get("current_condition", [{}])[0]
        description = current.get("weatherDesc", [{"value": "Unknown"}])[0]
        return {
            "status": "success",
            "city": city,
            "temperature": f"{current.get('temp_C', '?')} °C",
            "condition": description.get("value", "Unknown"),
            "humidity": f"{current.get('humidity', '?')}%",
            "wind_speed": f"{current.get('windspeedKmph', '?')} km/h",
            "source": "wttr.in",
        }

    if tool_name == "get_forecast":
        forecast = []
        for day in data.get("weather", [])[:5]:
            hourly = day.get("hourly") or [{}]
            summary = hourly[min(4, len(hourly) - 1)]
            description = summary.get("weatherDesc", [{"value": "Unknown"}])[0]
            forecast.append(
                {
                    "date": day.get("date"),
                    "max_temperature": f"{day.get('maxtempC', '?')} °C",
                    "min_temperature": f"{day.get('mintempC', '?')} °C",
                    "condition": description.get("value", "Unknown"),
                }
            )
        return {"status": "success", "city": city, "forecast": forecast, "source": "wttr.in"}

    return {"error": f"Unknown tool: {tool_name}"}
