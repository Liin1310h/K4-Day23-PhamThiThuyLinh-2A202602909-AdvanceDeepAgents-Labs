"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json
import os
import posixpath
import re
import sys
import time
from collections import Counter
from pathlib import Path

from agents import FINALIZER_PATH, NOTES_DIR, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent
from model import make_model
from sandbox import download, open_sandbox, upload
from tools import web_fetch

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"  # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    slug = re.sub(r"\W+", "-", topic.lower()).strip("-")[:60].strip("-")
    return slug or "topic"


TITLE_MATCH_MIN = 0.6  # share of a title's key words that must appear near the top of its page
UNREACHABLE_PREFIXES = ("ERROR", "EXC", "NO RESULTS", "Error fetching")  # what web_fetch returns when it cannot read a page
PAPER_LINK = re.compile(r"(?:arxiv\.org/abs/|huggingface\.co/papers/)([^/?#\s]+)")
TITLE_STOP_WORDS = {"with", "from", "that", "this", "into", "over", "under", "using", "based", "toward", "towards",
                    "their", "about"}


def _fetch_page(url):
    """The text of one source page, read with the lab's web_fetch tool."""
    return web_fetch.invoke({"url": url})


def _title_on_page(title, page):
    """Share of the title's key words that appear in the first part of the page (1.0 = all of them)."""
    words = {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{3,}", title.lower()) if w not in TITLE_STOP_WORDS}
    if not words:
        return 0.0
    head = page[:4000].lower()
    return sum(word in head for word in words) / len(words)


def _is_possible_paper_id(paper_id):
    """arXiv and Hugging Face paper ids look like 2501.00001: YYMM.NNNN or YYMM.NNNNN, and the number is never 0."""
    match = re.fullmatch(r"\d{4}\.(\d{4,5})(v\d+)?", paper_id)
    return bool(match) and int(match.group(1)) > 0


def check_sources(sources):
    """Return (problems, unverified). A problem is a source whose title is not on its page, or a paper id that cannot
    exist. A source whose page cannot be fetched is unverified: it is recorded in meta.json and does not fail the run,
    because the fetch tool also fails for pages that do exist."""
    problems, unverified = [], []
    for source in sources:
        n, url, title = source.get("n"), str(source.get("url") or ""), str(source.get("title") or "")
        link = PAPER_LINK.search(url)
        if link and not _is_possible_paper_id(link.group(1)):
            problems.append(f"source [{n}] has an impossible paper id: {url}")
            continue
        page = str(_fetch_page(url))
        if page.startswith(UNREACHABLE_PREFIXES):
            unverified.append(n)
        elif _title_on_page(title, page) < TITLE_MATCH_MIN:
            problems.append(f"source [{n}]: its title {title!r} is not on the page {url}")
    return problems, unverified


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return (
        f"Topic: {topic}\n\n"
        "Write a cited survey report on this topic. Follow your instructions in order: plan, delegate to the "
        "researchers, merge the notes, write the body, finalize, validate, spot-check, then stop. "
        f"The report must be saved at {REPORT_PATH}."
    )


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}.

    Only the lead's messages are counted: subagent tokens are not included, so this undercounts the real cost.
    """
    calls = Counter()
    input_tokens = output_tokens = 0
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            calls[call["name"]] += 1
        usage = getattr(message, "usage_metadata", None) or {}
        input_tokens += usage.get("input_tokens", 0)
        output_tokens += usage.get("output_tokens", 0)
    return {
        "model": model_name,
        "elapsed_s": round(elapsed, 1),
        "subagent_calls": calls["task"],
        "tool_calls": dict(calls),
        "tokens": {"input": input_tokens, "output": output_tokens},
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path.

    A run that produced no usable report or sources raises RuntimeError and writes NOTHING, so a failed run can
    never leave an empty or half-written report behind.
    """
    files = download(backend, [REPORT_PATH, SOURCES_PATH])
    raw_report, raw_sources = files.get(REPORT_PATH) or b"", files.get(SOURCES_PATH) or b""
    try:
        report_text = raw_report.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{REPORT_PATH} is not UTF-8 text: {exc}") from exc
    if not report_text:
        raise RuntimeError(f"the agent wrote no report to {REPORT_PATH}")
    try:
        sources = json.loads(raw_sources)
    except ValueError as exc:
        raise RuntimeError(f"{SOURCES_PATH} is missing or is not valid JSON: {exc}") from exc
    if not isinstance(sources, list) or not sources or not all(isinstance(s, dict) for s in sources):
        raise RuntimeError(f"{SOURCES_PATH} must be a non-empty JSON list of objects")
    # GUIDE 3 and REPORT_TEMPLATE give every source a title: the References line needs it
    untitled = [s.get("n") for s in sources if not str(s.get("title") or "").strip()]
    if untitled:
        raise RuntimeError(f"{SOURCES_PATH} has sources without a title: n={untitled}")
    # every source must lead to a page that says what its title says (the lead can pair a title with the wrong URL)
    problems, unverified = check_sources(sources)
    if problems:
        raise RuntimeError("sources do not match their pages: " + "; ".join(problems))
    if unverified:
        print(f"warning: could not read the pages of sources n={unverified}; recorded in meta.json", file=sys.stderr)

    slug = slugify(topic)
    meta = {
        "topic": topic,
        **summarize(messages, elapsed, model_name),
        "n_sources": len(sources),
        "source_families": sorted({str(s["source"]) for s in sources if s.get("source")}),
        "unverified_sources": unverified,
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    # the report and sources are saved byte-for-byte as the sandbox produced them (RUBRIC: no hand edits)
    (reports_dir / f"{slug}.sources.json").write_bytes(raw_sources)
    (reports_dir / f"{slug}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = reports_dir / f"{slug}.md"
    report_path.write_bytes(raw_report)  # last: a report file exists only once the other two are written
    return report_path


def diagnose(backend, messages):
    """Print what the lead did when no report came out: the tools it called, its last tool results, its last words,
    and the files that exist in the sandbox. Printed to stderr; nothing is written to reports/."""
    calls = Counter(call["name"] for m in messages for call in getattr(m, "tool_calls", None) or [])
    print(f"diagnosis: tool calls made by the lead: {dict(calls) or 'none'}", file=sys.stderr)
    tool_results = [m for m in messages if getattr(m, "type", "") == "tool"][-3:]
    for result in tool_results:
        print(f"diagnosis: tool result: {str(result.content)[:300]}", file=sys.stderr)
    ai_texts = [m for m in messages if getattr(m, "type", "") == "ai" and str(getattr(m, "content", "")).strip()]
    last_words = str(ai_texts[-1].content)[:600] if ai_texts else "(none)"
    print(f"diagnosis: lead's last message: {last_words}", file=sys.stderr)
    listing = backend.execute(f"ls -R {WORKDIR} | head -n 60").output
    print(f"diagnosis: files in the sandbox:\n{listing}", file=sys.stderr)


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic).

    The sandbox is always stopped and removed, even on errors (open_sandbox is a context manager).
    """
    topic = topic.strip()
    if not topic:
        print('usage: python research.py "<topic>"', file=sys.stderr)
        return 2
    # check the configuration BEFORE a sandbox is created, so a bad .env costs nothing
    model_name = os.getenv("LAB_MODEL") or os.getenv("OPENAI_DEPLOYMENT_MODEL") or ""
    if "<" in model_name:
        print(f"FAILED: LAB_MODEL in .env is still the placeholder {model_name!r}; put a real model name there",
              file=sys.stderr)
        return 1
    try:
        model = make_model()
    except Exception as exc:  # missing model name or API key
        print(f"FAILED: cannot create the model: {exc}", file=sys.stderr)
        return 1
    model_name = model_name or "unknown"
    start = time.monotonic()
    with open_sandbox() as backend:
        setup = backend.execute(f"mkdir -p {NOTES_DIR} {posixpath.dirname(REPORT_PATH)}")
        if setup.exit_code != 0:
            print(f"FAILED: cannot create the folders in the sandbox: {setup.output}", file=sys.stderr)
            return 1
        upload(backend, {VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(), FINALIZER_PATH: FINALIZER_SOURCE.read_bytes()})
        agent = build_lead_agent(backend, model)
        result = agent.invoke({"messages": [{"role": "user", "content": build_prompt(topic)}]},
                              config={"recursion_limit": 1000})
        elapsed = time.monotonic() - start
        try:
            path = save_outputs(backend, topic, result["messages"], elapsed, model_name, reports_dir=REPORTS)
        except RuntimeError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            diagnose(backend, result["messages"])
            return 1
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
