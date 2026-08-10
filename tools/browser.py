"""Browser and web search tools."""

from __future__ import annotations

import subprocess
import urllib.parse
import webbrowser

from config.settings import get_settings
from core.events import bus
from tools.registry import register_tool


@register_tool(
    name="open_website",
    description="Open a URL in the default browser",
    parameters={"url": "Website URL"},
)
def open_website(params: dict) -> dict:
    url = params["url"].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url)
    return {"success": True, "message": f"Opened {url}."}


@register_tool(
    name="search_web",
    description="Search the web for current information (uses DuckDuckGo, free)",
    parameters={"query": "Search query"},
)
def search_web(params: dict) -> dict:
    settings = get_settings()
    if not settings.web_search_enabled:
        return {"success": False, "message": "Web search is disabled in settings."}
    query = params["query"]
    bus.web_search_status.emit("searching")
    try:
        from duckduckgo_search import DDGS

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=5):
                results.append({"title": r.get("title", ""), "body": r.get("body", ""), "href": r.get("href", "")})
        bus.web_search_status.emit("complete")
        if not results:
            encoded = urllib.parse.quote(query)
            webbrowser.open(f"https://duckduckgo.com/?q={encoded}")
            return {"success": True, "message": f"Opened browser search for: {query}", "results": []}
        summary = "\n".join(f"- {r['title']}: {r['body'][:200]}" for r in results[:3])
        return {"success": True, "message": summary, "results": results}
    except ImportError:
        encoded = urllib.parse.quote(query)
        webbrowser.open(f"https://duckduckgo.com/?q={encoded}")
        bus.web_search_status.emit("complete")
        return {"success": True, "message": f"Opened browser search for: {query}"}
    except Exception as e:
        bus.web_search_status.emit("error")
        return {"success": False, "message": f"Web search failed: {e}"}


@register_tool(
    name="search_youtube",
    description="Search YouTube in the browser",
    parameters={"query": "Search query"},
)
def search_youtube(params: dict) -> dict:
    query = urllib.parse.quote(params["query"])
    url = f"https://www.youtube.com/results?search_query={query}"
    webbrowser.open(url)
    return {"success": True, "message": f"Searching YouTube for {params['query']}."}
