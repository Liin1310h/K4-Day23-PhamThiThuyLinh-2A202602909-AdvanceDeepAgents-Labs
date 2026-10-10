"""check_citations.py - STUDENT IMPLEMENTS `check`.   Runs INSIDE the sandbox (standard library only).

research.py uploads this file to the sandbox and the lead agent runs it with the `execute` tool:
    python3 /tmp/work/research/check_citations.py [report.md] [sources.json]
It must exit 0 and print "OK: ..." when the report is consistent, else print each problem and exit 1.
"""
import json
import re
import sys

REPORT = "/tmp/work/report/report.md"
SOURCES = "/tmp/work/research/sources.json"

# Same conventions as finalize_citations.py, so a report that the finalizer produced is judged the same way.
CITATION = re.compile(r"\[(\d+(?:\s*[,–-]\s*\d+)*)\](?!\()")  # [3]  [1, 2]  [1-3]; not a Markdown link [3](url)
CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)        # code is never part of the citations
REFERENCES_HEADING = re.compile(r"(?m)^##[ \t]+References[ \t]*$")
REFERENCE_LINE = re.compile(r"^\s*\[(\d+)\]")
URL = re.compile(r"https?://\S+")


def _numbers(group):
    """'2' -> [2], '1, 2' -> [1, 2], '1-3' -> [1, 2, 3]."""
    numbers = []
    for part in re.split(r"\s*,\s*", group):
        span = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            numbers.extend(range(a, b + 1) if 0 <= b - a <= 200 else [a, b])
        else:
            numbers.append(int(part))
    return numbers


def check(report_text, sources):
    """Return a list of problem strings (empty list = OK). The rules are in GUIDE.md part 4."""
    if not isinstance(sources, list) or not sources:
        return ["no sources in sources.json"]

    problems = []
    url_of = {}  # n -> url
    seen_urls = set()
    for entry in sources:
        if not isinstance(entry, dict):
            problems.append(f"source entry is not an object: {entry!r}")
            continue
        n, url = entry.get("n"), entry.get("url")
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"source n={n!r} is not an integer")
            continue
        if n in url_of:
            problems.append(f"source [{n}] appears twice in sources.json")
        url_of[n] = url
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append(f"source [{n}] has a url that is not http(s): {url!r}")
        elif url in seen_urls:
            problems.append(f"url {url} appears in more than one source")
        else:
            seen_urls.add(url)

    # split at the LAST "## References" heading, as finalize_citations.py does
    headings = list(REFERENCES_HEADING.finditer(report_text))
    if headings:
        body, references = report_text[: headings[-1].start()], report_text[headings[-1].end():]
    else:
        problems.append("report has no '## References' heading")
        body, references = report_text, ""

    cited = set()
    for group in CITATION.findall(CODE.sub(" ", body)):
        cited.update(_numbers(group))
    problems += [f"[{n}] cited but missing from sources.json" for n in sorted(cited - set(url_of))]
    problems += [f"source [{n}] never cited" for n in sorted(set(url_of) - cited)]

    reference_count = {}
    for line in references.splitlines():
        match = REFERENCE_LINE.match(line)
        if not match:
            continue
        n = int(match.group(1))
        reference_count[n] = reference_count.get(n, 0) + 1
        found = URL.findall(line)
        if len(found) != 1:
            problems.append(f"reference [{n}] must contain exactly one URL, found {len(found)}")
        elif n in url_of and found[0] != url_of[n]:
            problems.append(f"reference [{n}] has URL {found[0]} but sources.json says {url_of[n]}")
    for n in sorted(url_of):
        if reference_count.get(n, 0) == 0:
            problems.append(f"source [{n}] has no reference line")
        elif reference_count[n] > 1:
            problems.append(f"source [{n}] has {reference_count[n]} reference lines")
    problems += [f"reference [{n}] is not in sources.json" for n in sorted(set(reference_count) - set(url_of))]
    return problems


def main(argv):
    report_path = argv[1] if len(argv) > 1 else REPORT
    sources_path = argv[2] if len(argv) > 2 else SOURCES
    try:
        with open(report_path, encoding="utf-8") as f:
            report = f.read()
        with open(sources_path, encoding="utf-8") as f:
            sources = json.load(f)
    except (OSError, ValueError) as exc:
        print(f"cannot read inputs: {exc}")
        return 1
    problems = check(report, sources)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"OK: {len(sources)} sources, all citations resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
