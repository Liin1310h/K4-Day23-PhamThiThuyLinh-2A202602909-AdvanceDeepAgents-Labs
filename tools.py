"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET

import httpx
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"

# ---- our own constants ----
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
ARXIV_MIN_INTERVAL = 3.0  # seconds between two arXiv calls (arXiv asks for this)
ARXIV_NS = "{http://www.w3.org/2005/Atom}"
EXA_HEADERS = {"Accept": "application/json, text/event-stream"}  # Exa answers as server-sent events
SUMMARY_CHARS = 600  # records go into the agent's context: keep them short
PAGE_CHARS = 12000  # longest text returned by web_search and web_fetch


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- shared helpers ----
def _squash(text):
    """Collapse runs of whitespace: arXiv titles and summaries contain newlines."""
    return " ".join(str(text or "").split())


def _retry_after(resp):
    """The Retry-After header in seconds, or None. (The HTTP-date form is not parsed.)"""
    try:
        return float(resp.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


def _send(method, url, *, timeout=20.0, **kwargs):
    """One HTTP request. Network errors and 429/5xx become RetryableError; other HTTP errors propagate."""
    try:
        resp = httpx.request(method, url, timeout=timeout, follow_redirects=True, **kwargs)
    except httpx.TransportError as exc:
        raise RetryableError(f"network error: {exc}") from exc
    if resp.status_code in RETRYABLE_STATUS:
        raise RetryableError(f"HTTP {resp.status_code}", retry_after=_retry_after(resp))
    resp.raise_for_status()
    return resp


def _error(exc):
    """The 'ERROR: ...' text the agent sees. The Exa key is masked: it sits in the URL, so it can appear in exception text."""
    text = f"{type(exc).__name__}: {exc}"
    key = os.environ.get("EXA_API_KEY", "").strip()
    if key:
        text = text.replace(key, "<redacted>")
    return f"ERROR: {text}"


# ---- retry and backoff (GUIDE 1.1) ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(). When it raises RetryableError, wait and call it again. The last failure is raised, with no sleep after it."""
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as exc:
            if attempt == attempts - 1:
                raise
            if exc.retry_after is not None:
                delay = min(max(exc.retry_after, 0.0), cap)  # the server knows best
            else:
                delay = min(cap, base * 2**attempt + random.uniform(0, base))  # exponential backoff with jitter
            time.sleep(delay)


# ---- arXiv (GUIDE 1.2) ----
_arxiv_lock = threading.Lock()
_arxiv_last_call = 0.0


def _arxiv_wait():
    """Start arXiv calls at least ARXIV_MIN_INTERVAL seconds apart, also when subagents call in parallel."""
    global _arxiv_last_call
    with _arxiv_lock:
        pause = ARXIV_MIN_INTERVAL - (time.monotonic() - _arxiv_last_call)
        if pause > 0:
            time.sleep(pause)
        _arxiv_last_call = time.monotonic()


def _search_terms(query):
    """Keep word-like tokens only. Quotes, colons and stray AND/OR written by the LLM would break the arXiv query."""
    return [term for term in re.findall(r"\w[\w-]*", query) if term not in ("AND", "OR", "NOT")]


@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    try:
        terms = _search_terms(query)
        if not terms:
            return "NO RESULTS"  # an empty query never reaches the network
        params = {
            "search_query": " AND ".join(f"all:{term}" for term in terms),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max(1, min(int(max_results), 30)),
        }

        def fetch():
            _arxiv_wait()
            return _send("GET", ARXIV_URL, params=params)

        feed = ET.fromstring(with_retry(fetch, attempts=7, cap=60.0).content)
        records = []
        for entry in feed.findall(f"{ARXIV_NS}entry"):
            raw_id = entry.findtext(f"{ARXIV_NS}id", "")
            if "/abs/" not in raw_id:
                continue  # arXiv also puts error messages in an <entry>
            arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/", 1)[1].strip())  # 2501.00001v1 -> 2501.00001
            records.append({
                "id": arxiv_id,
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "published": entry.findtext(f"{ARXIV_NS}published", "")[:10],
                "title": _squash(entry.findtext(f"{ARXIV_NS}title", "")),
                "summary": _squash(entry.findtext(f"{ARXIV_NS}summary", ""))[:SUMMARY_CHARS],
            })
    except Exception as exc:
        return _error(exc)
    return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"


# ---- Hugging Face (GUIDE 1.3) ----
def _hf_record(paper, summary):
    """The record shape shared by both Hugging Face tools."""
    return {
        "id": paper["id"],
        "url": f"https://huggingface.co/papers/{paper['id']}",
        "published": str(paper.get("publishedAt") or "")[:10],
        "title": _squash(paper.get("title")),
        "summary": _squash(summary)[:SUMMARY_CHARS],
        "upvotes": paper.get("upvotes") or 0,
        "github": paper.get("githubRepo") or "",
        "stars": paper.get("githubStars") or 0,
    }


@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    try:
        params = {"limit": max(1, min(int(limit), 100))}
        if date:
            params["date"] = date
        items = with_retry(lambda: _send("GET", HF_DAILY_URL, params=params)).json()
        needle = keyword.lower()
        records = []
        for item in items:
            paper = item.get("paper") or {}
            if not paper.get("id"):
                continue
            if needle and needle not in f"{paper.get('title') or ''} {paper.get('summary') or ''}".lower():
                continue
            records.append(_hf_record(paper, paper.get("summary")))
        records.sort(key=lambda record: record["upvotes"], reverse=True)
    except Exception as exc:
        return _error(exc)
    return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    try:
        if not query.strip():
            return "NO RESULTS"
        params = {"q": query, "limit": max(1, min(int(limit), 50))}
        items = with_retry(lambda: _send("GET", HF_SEARCH_URL, params=params)).json()
        records = []
        for item in items:
            paper = item.get("paper") or {}
            if not paper.get("id"):
                continue
            # the search endpoint adds an AI-written summary: shorter and easier to read than the abstract
            records.append(_hf_record(paper, paper.get("ai_summary") or paper.get("summary")))
    except Exception as exc:
        return _error(exc)
    return json.dumps(records, ensure_ascii=False) if records else "NO RESULTS"


# ---- web search and fetch through the Exa MCP endpoint (GUIDE 1.4) ----
def _rate_limited(meta, text):
    """Exa's free tier answers HTTP 200 with a notice instead of 429 (GUIDE 1.4). Two signals: a flag in
    result._meta, or a SHORT answer that mentions the rate limit (a real search answer is much longer)."""
    if isinstance(meta, dict) and any("rate" in str(key).lower() and value for key, value in meta.items()):
        return True
    return len(text) < 400 and "rate limit" in text.lower()


def _exa_call(tool_name, arguments):
    """Call one Exa MCP tool over plain HTTP (JSON-RPC 2.0) and return its text. Raises RetryableError when rate limited."""
    key = os.environ.get("EXA_API_KEY", "").strip()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
    resp = _send("POST", EXA_URL, timeout=60.0, params={"exaApiKey": key} if key else None, json=body, headers=EXA_HEADERS)
    # the answer is server-sent events: the JSON object sits on the "data:" line
    data_lines = [line[len("data:"):].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    message = json.loads(data_lines[-1]) if data_lines else resp.json()
    if "error" in message:
        raise RuntimeError(f"Exa returned a JSON-RPC error: {message['error']}")
    result = message["result"]
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()
    if _rate_limited(result.get("_meta"), text):
        raise RetryableError("Exa rate limit")
    return text


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    if not query.strip():
        return "NO RESULTS"
    arguments = {
        "query": query,
        "objective": objective or f"Find web pages that answer: {query}",  # Exa requires an objective
        "numResults": max(1, min(int(num_results), 10)),
    }
    try:
        text = with_retry(lambda: _exa_call("web_search_exa", arguments), attempts=8, cap=60.0)
    except Exception as exc:
        return _error(exc)
    return text[:PAGE_CHARS] or "NO RESULTS"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    try:
        text = with_retry(lambda: _exa_call("web_fetch_exa", {"urls": [url]}), attempts=8, cap=60.0)
    except Exception as exc:
        return _error(exc)
    return text[:PAGE_CHARS] or "NO RESULTS"


# ---- registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
