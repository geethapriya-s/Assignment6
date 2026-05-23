"""
MCP server for EAGV3 Session 6.

Nine tools, stdio transport:
    web_search, fetch_url, get_time, currency_convert,
    read_file, list_dir, create_file, update_file, edit_file

web_search:  Tavily primary, DuckDuckGo fallback. Hard-capped at 5 results.
fetch_url:   crawl4ai only — clean markdown via headless Chromium.
Usage for tavily and duckduckgo is logged to ./usage.json with monthly
rollover and a soft cap of 950/1000 on Tavily.

File tools are sandboxed under ./sandbox/. Run:  python mcp_server.py
"""

from __future__ import annotations

import json
import os
import threading
from urllib.parse import unquote
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from ddgs import DDGS
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

MAX_SEARCH_RESULTS = 5  # hard cap — Tavily prices per result
WEB_SEARCH_TIMEOUT_SEC = 35
FETCH_HTTPX_TIMEOUT_SEC = 12
FETCH_FAST_TOTAL_SEC = 28
FETCH_CRAWL_TIMEOUT_SEC = 45
WIKI_USER_AGENT = "EAGV3-Session6/1.0 (local assignment; contact: student@example.edu)"

load_dotenv(Path(__file__).parent / ".env")

mcp = FastMCP("eagv3-s6-server")

SANDBOX = Path(__file__).parent / "sandbox"
SANDBOX.mkdir(exist_ok=True)

USAGE_PATH = Path(__file__).parent / "usage.json"
MONTHLY_CAP = 950  # leave 50/mo headroom on Tavily
_usage_lock = threading.Lock()


def _safe(path: str) -> Path:
    p = (SANDBOX / path).resolve()
    base = SANDBOX.resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"Path '{path}' escapes the sandbox")
    return p


def _empty_usage(month: str) -> dict:
    return {
        "month": month,
        "tavily": {"count": 0, "errors": 0},
        "duckduckgo": {"count": 0, "errors": 0},
    }


def _load_usage() -> dict:
    month = datetime.now().strftime("%Y-%m")
    if not USAGE_PATH.exists():
        return _empty_usage(month)
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_usage(month)
    if data.get("month") != month:
        return _empty_usage(month)
    for k in ("tavily", "duckduckgo"):
        data.setdefault(k, {"count": 0, "errors": 0})
    return data


def _save_usage(data: dict) -> None:
    USAGE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _bump(provider: str, field: str = "count") -> None:
    with _usage_lock:
        data = _load_usage()
        data[provider][field] = data[provider].get(field, 0) + 1
        _save_usage(data)


def _under_cap(provider: str) -> bool:
    return _load_usage()[provider]["count"] < MONTHLY_CAP


def _tavily_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(os.environ["TAVILY_API_KEY"])
    resp = client.search(
        query=query,
        max_results=max_results,
        search_depth="basic",
    )
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in resp.get("results", [])
    ]


def _ddg_search(query: str, max_results: int) -> list[dict]:
    hits: list[dict] = []
    with DDGS() as ddgs:
        for backend in ("lite", "html", "auto"):
            try:
                hits = list(ddgs.text(query, max_results=max_results, backend=backend))
            except Exception:
                hits = []
            if hits:
                break
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("href", ""),
            "snippet": h.get("body", ""),
        }
        for h in hits
    ]


def _web_search_sync(query: str, max_results: int) -> list[dict]:
    if os.environ.get("TAVILY_API_KEY") and _under_cap("tavily"):
        try:
            results = _tavily_search(query, max_results)
            if results:
                _bump("tavily")
                return results
        except Exception:
            _bump("tavily", "errors")
    results = _ddg_search(query, max_results)
    _bump("duckduckgo")
    return results


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _wiki_title_from_url(url: str) -> str | None:
    if "wikipedia.org/wiki/" not in url:
        return None
    title = unquote(url.split("/wiki/", 1)[-1].split("?")[0].split("#")[0])
    return title or None


def _wikipedia_mediawiki_fetch(url: str) -> dict | None:
    """MediaWiki extracts API — works when REST/HTML return 403."""
    title = _wiki_title_from_url(url)
    if not title:
        return None
    wiki_headers = {
        "User-Agent": WIKI_USER_AGENT,
        "Accept": "application/json",
    }
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": "1",
        "titles": title,
        "format": "json",
        "formatversion": "2",
    }
    with httpx.Client(timeout=FETCH_HTTPX_TIMEOUT_SEC, follow_redirects=True) as client:
        r = client.get(
            "https://en.wikipedia.org/w/api.php",
            params=params,
            headers=wiki_headers,
        )
        r.raise_for_status()
        data = r.json()
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return None
    page = pages[0]
    if page.get("missing"):
        return None
    text = page.get("extract") or page.get("title") or ""
    if not text.strip():
        return None
    return {
        "status": r.status_code,
        "content_type": "application/json",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
        "source": "wikipedia MediaWiki API",
    }


def _wikipedia_api_fetch(url: str) -> dict | None:
    """REST summary for en.wikipedia.org/wiki/... pages."""
    title = _wiki_title_from_url(url)
    if not title:
        return None
    api_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    headers = {"User-Agent": WIKI_USER_AGENT, "Accept": "application/json"}
    with httpx.Client(timeout=FETCH_HTTPX_TIMEOUT_SEC, follow_redirects=True) as client:
        r = client.get(api_url, headers=headers)
        r.raise_for_status()
        data = r.json()
    text = "\n\n".join(
        part
        for part in (
            data.get("title") or "",
            data.get("description") or "",
            data.get("extract") or "",
        )
        if part
    )
    return {
        "status": r.status_code,
        "content_type": "application/json",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
        "source": "wikipedia REST summary API",
    }


def _httpx_fetch_fallback(url: str, note: str) -> dict:
    """Browser-like httpx HTML fetch."""
    mobile = url
    if "en.wikipedia.org/wiki/" in url and "en.m.wikipedia.org" not in url:
        mobile = url.replace("en.wikipedia.org/wiki/", "en.m.wikipedia.org/wiki/", 1)
    with httpx.Client(timeout=FETCH_HTTPX_TIMEOUT_SEC, follow_redirects=True) as client:
        r = client.get(mobile, headers=_BROWSER_HEADERS)
        if r.status_code >= 400 and len(r.text) < 500:
            r.raise_for_status()
        text = r.text
    return {
        "status": r.status_code,
        "content_type": r.headers.get("content-type", "text/html"),
        "length_bytes": len(text.encode("utf-8")),
        "text": text[:250_000],
        "source": note,
    }


def _httpx_compact_fetch(url: str) -> dict | None:
    """Small text/plain or lightweight responses (weather APIs, etc.)."""
    with httpx.Client(timeout=FETCH_HTTPX_TIMEOUT_SEC, follow_redirects=True) as client:
        r = client.get(
            url,
            headers={**_BROWSER_HEADERS, "Accept": "text/plain,text/*,*/*;q=0.8"},
        )
        if r.status_code >= 400:
            return None
        text = r.text[:8000]
    if len(text.strip()) < 10:
        return None
    lower = text.lstrip().lower()
    if lower.startswith("<!doctype") or (lower.startswith("<html") and text.count("<") > 40):
        return None
    return {
        "status": r.status_code,
        "content_type": r.headers.get("content-type", "text/plain"),
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
        "source": "httpx compact",
    }


def _fast_fetch(url: str) -> dict:
    """Try fast sources only (no Chromium). Raises on total failure."""
    errors: list[str] = []
    for label, fn in (
        ("compact HTTP", _httpx_compact_fetch),
        ("MediaWiki API", _wikipedia_mediawiki_fetch),
        ("REST summary", _wikipedia_api_fetch),
        ("httpx HTML", lambda u: _httpx_fetch_fallback(u, "httpx HTML")),
    ):
        try:
            result = fn(url)
            if result and result.get("text", "").strip():
                return result
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    raise RuntimeError(
        "fast fetch failed (" + "; ".join(errors[:3]) + "). "
        "Use web_search and answer from snippets."
    )


async def _crawl4ai_fetch(url: str, timeout: int = FETCH_CRAWL_TIMEOUT_SEC) -> dict:
    import asyncio

    from crawl4ai import AsyncWebCrawler

    # crawl4ai uses Rich which writes via its own captured stdout reference, so
    # contextlib.redirect_stdout doesn't catch it. Redirect at the file-descriptor
    # level — crawl4ai's banner / [FETCH] / [SCRAPE] markers would otherwise
    # corrupt the MCP stdio JSON-RPC stream.
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        async with AsyncWebCrawler(verbose=False) as crawler:
            r = await asyncio.wait_for(crawler.arun(url=url), timeout=timeout)
    except asyncio.TimeoutError:
        return _httpx_fetch_fallback(
            url, f"crawl4ai timed out after {timeout}s; httpx HTML fallback"
        )
    except Exception as exc:
        return _httpx_fetch_fallback(url, f"crawl4ai failed ({exc}); httpx HTML fallback")
    finally:
        os.dup2(saved_fd, 1)
        os.close(saved_fd)
    # r.markdown is a str subclass (StringCompatibleMarkdown) that Pydantic
    # serializes as {} because its real field is private. Pull the raw string
    # out and force a plain str so FastMCP serializes correctly.
    md = r.markdown
    raw = (
        getattr(md, "raw_markdown", None)
        or getattr(md, "fit_markdown", None)
        or md
        or r.cleaned_html
        or r.html
        or ""
    )
    text = str(raw)
    return {
        "status": int(getattr(r, "status_code", None) or 200),
        "content_type": "text/markdown",
        "length_bytes": len(text.encode("utf-8")),
        "text": text,
    }


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results. Example: web_search("python asyncio tutorial", 3)."""
    import asyncio

    max_results = max(1, min(max_results, MAX_SEARCH_RESULTS))
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_web_search_sync, query, max_results),
            timeout=WEB_SEARCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"web_search timed out after {WEB_SEARCH_TIMEOUT_SEC}s"
        ) from exc


@mcp.tool()
async def fetch_url(url: str, timeout: int = 45, fast: bool = True) -> dict:
    """Fetch page content. fast=true (default): APIs/httpx only (~28s). fast=false: crawl4ai browser (slow)."""
    import asyncio

    budget = max(15, min(int(timeout), 90))
    if fast:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_fast_fetch, url),
                timeout=min(FETCH_FAST_TOTAL_SEC, budget),
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"fetch_url (fast) timed out after {min(FETCH_FAST_TOTAL_SEC, budget)}s. "
                "Use web_search instead."
            ) from exc
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    crawl_timeout = min(FETCH_CRAWL_TIMEOUT_SEC, budget)
    try:
        return await _crawl4ai_fetch(url, timeout=crawl_timeout)
    except Exception as exc:
        raise RuntimeError(
            f"fetch_url (crawl4ai) failed for {url}: {exc}. "
            "Try fast=true or web_search."
        ) from exc


def _resolve_timezone(name: str) -> ZoneInfo:
    """Windows often lacks 'UTC' / 'Etc/UTC' in tzdata — try common aliases."""
    key = name.strip()
    if key.upper() == "UTC":
        candidates = ("UTC", "Etc/UTC", "GMT", "Europe/London")
    else:
        candidates = (key,)
    last_err: Exception | None = None
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except Exception as exc:
            last_err = exc
    raise ValueError(f"No time zone found with key {name!r}") from last_err


@mcp.tool()
def get_time(timezone: str = "UTC") -> dict:
    """Current time in a named IANA timezone. Example: get_time("Asia/Kolkata")."""
    tz = _resolve_timezone(timezone)
    now = datetime.now(tz)
    offset = now.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else 0.0
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %d %B %Y %H:%M:%S %Z"),
        "timezone": timezone,
        "offset_hours": offset_hours,
    }


@mcp.tool()
def currency_convert(amount: float, from_currency: str, to_currency: str) -> dict:
    """Convert money between ISO-3 currencies via frankfurter.dev. Example: currency_convert(100, "USD", "INR")."""
    f = from_currency.upper()
    t = to_currency.upper()
    url = f"https://api.frankfurter.dev/v1/latest?amount={amount}&base={f}&symbols={t}"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    converted = data["rates"][t]
    return {
        "amount": amount,
        "from": f,
        "to": t,
        "rate": converted / amount if amount else 0.0,
        "converted": converted,
        "date": data["date"],
        "source": "frankfurter.dev",
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file from the sandbox. Example: read_file("notes.txt")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    return {
        "path": path,
        "size_bytes": p.stat().st_size,
        "content": text,
        "encoding": "utf-8",
    }


@mcp.tool()
def list_dir(path: str = ".") -> list[dict]:
    """List a directory inside the sandbox. Example: list_dir(".")."""
    p = _safe(path)
    out = []
    for child in sorted(p.iterdir()):
        is_dir = child.is_dir()
        out.append({
            "name": child.name,
            "type": "dir" if is_dir else "file",
            "size_bytes": 0 if is_dir else child.stat().st_size,
        })
    return out


@mcp.tool()
def create_file(path: str, content: str) -> dict:
    """Create a new file in the sandbox; errors if it exists. Example: create_file("hello.txt", "hi")."""
    p = _safe(path)
    if p.exists():
        raise ValueError(f"File '{path}' already exists")
    if not p.parent.exists():
        raise ValueError(f"Parent directory of '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def update_file(path: str, content: str) -> dict:
    """Overwrite an existing sandbox file. Example: update_file("hello.txt", "new body")."""
    p = _safe(path)
    if not p.exists():
        raise ValueError(f"File '{path}' does not exist")
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": path, "size_bytes": p.stat().st_size}


@mcp.tool()
def edit_file(path: str, find: str, replace: str, replace_all: bool = False) -> dict:
    """Find-and-replace inside a sandbox file. Example: edit_file("hello.txt", "foo", "bar")."""
    p = _safe(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        raise ValueError(f"'{find}' not found in '{path}'")
    if count > 1 and not replace_all:
        raise ValueError(
            f"'{find}' occurs {count} times in '{path}'; pass replace_all=True"
        )
    new_text = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    p.write_text(new_text, encoding="utf-8")
    replacements = count if replace_all else 1
    return {
        "ok": True,
        "path": path,
        "replacements": replacements,
        "size_bytes": p.stat().st_size,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
