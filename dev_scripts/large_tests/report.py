#!/usr/bin/env python3

import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

DOC_TO_PIXELS_LOG_START = "----- DOC TO PIXELS LOG START -----"
DOC_TO_PIXELS_LOG_END = "----- DOC TO PIXELS LOG END -----"

EXPECTED_PATTERNS = [
    re.compile(r"^Converting page X/X to pixels$"),
    re.compile(r"^Converting page X/X from pixels to searchable PDF$"),
    re.compile(r"^Converting to PDF using LibreOffice$"),
    re.compile(r"^Converted document to pixels$"),
    re.compile(r"^Safe PDF created$"),
    re.compile(r"^Compressing PDF$"),
    re.compile(r"^Merging X pages into a single PDF$"),
    re.compile(r"^Calculating number of pages$"),
    re.compile(r"^\[COMMAND\].*$"),
    re.compile(r"^Result: (SUCCESS|FAILURE)$"),
    re.compile(r"^pdfinfo:$"),
    re.compile(r"^pdftoppm: Syntax Error.*$"),
    re.compile(r"^convert /tmp/input_file as a .*$"),
    re.compile(r"^time=.*msg=\"forwarding signal.*"),
    re.compile(r"^time=.*msg=\"Waiting for container.*"),
    re.compile(r"^Installing LibreOffice extension.*$"),
    re.compile(r"^Archive:.*$"),
    re.compile(r"^ extracting:.*$"),
    re.compile(r"^  inflating:.*$"),
    re.compile(r"^$"),
]


def scrub_container_line(line: str) -> str:
    line = re.sub(r"\b[0-9a-fA-F]{6,}\b", "X", line)
    line = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "X", line)
    line = re.sub(r"\d+", "X", line)
    return line


def is_expected_line(line: str) -> bool:
    return any(p.match(line) for p in EXPECTED_PATTERNS)


def is_blank_line(line: str) -> bool:
    return line == ""


def parse_junit(xml_file: str) -> ET.Element:
    tree = ET.parse(xml_file)
    return tree.getroot()


def count_results(root: ET.Element) -> Dict[str, int]:
    total_errors = 0
    total_failures = 0
    total_skipped = 0
    total_tests = 0
    for testsuite in root.findall("testsuite"):
        total_errors += int(testsuite.attrib.get("errors", "0"))
        total_failures += int(testsuite.attrib.get("failures", "0"))
        total_skipped += int(testsuite.attrib.get("skipped", "0"))
        total_tests += int(testsuite.attrib.get("tests", "0"))
    return {
        "errors": total_errors,
        "failures": total_failures,
        "skipped": total_skipped,
        "tests": total_tests,
    }


def get_test_cases(root: ET.Element) -> List[ET.Element]:
    cases = []
    for testsuite in root.findall("testsuite"):
        cases.extend(testsuite.findall("testcase"))
    return cases


def get_test_status(testcase: ET.Element) -> str:
    if testcase.find("failure") is not None:
        return "FAIL"
    elif testcase.find("error") is not None:
        return "ERROR"
    return "PASS"


def get_extension(name: str) -> str:
    m = re.search(r"\[([^\]]+)\]", name)
    if m:
        ext = Path(m.group(1)).suffix.lstrip(".")
        return ext if ext else "none"
    return "none"


def get_size_bucket(name: str) -> str:
    if "10K_docs" in name:
        return "0KB  -  10KB"
    elif "100K_docs" in name:
        return "10KB - 100KB"
    elif "10M_docs" in name:
        return "100KB - 10MB"
    elif "100M_docs" in name:
        return "10MB - 100MB"
    return "unknown"


def extract_captured_text(testcase: ET.Element, tag: str) -> str:
    elem = testcase.find(tag)
    if elem is not None and elem.text:
        text = elem.text
        lines = text.split("\n")
        content_lines = []
        in_content = False
        for line in lines:
            if "Captured" in line and "---" in line:
                in_content = True
                continue
            if in_content:
                content_lines.append(line)
        if content_lines:
            return "\n".join(content_lines)
    return ""


def extract_container_output(testcase: ET.Element) -> str:
    output = extract_captured_text(testcase, "system-out")
    if DOC_TO_PIXELS_LOG_START in output and DOC_TO_PIXELS_LOG_END in output:
        (_, rest) = output.split(DOC_TO_PIXELS_LOG_START, 1)
        (log, _) = rest.split(DOC_TO_PIXELS_LOG_END, 1)
        return log.strip()
    elif output:
        return output.strip()
    return ""


def get_container_lines(testcase: ET.Element) -> List[str]:
    output = extract_container_output(testcase)
    if output:
        return [line.rstrip() for line in output.split("\n")]
    return []


def generate_report(xml_file: str) -> str:
    root = parse_junit(xml_file)
    results = count_results(root)
    test_cases = get_test_cases(root)

    total = results["tests"]
    failures = results["failures"]
    errors = results["errors"]
    skipped = results["skipped"]
    failure_rate = failures / total if total > 0 else 0.0

    lines = []
    lines.append("==== RESULTS SUMMARY ===")
    lines.append(f"    errors: {errors}")
    lines.append(f"    failures: {failures}")
    lines.append(f"    successes: {total - errors - failures - skipped}")
    lines.append(f"    skipped: {skipped}")
    lines.append(f"    tests: {total}")
    lines.append(f"    failure rate: {failure_rate}")
    lines.append("")
    lines.append("")

    ext_counter = Counter()
    size_timing: Dict[str, dict] = {}
    for tc in test_cases:
        name = tc.attrib.get("name", "")
        ext = get_extension(name)
        bucket = get_size_bucket(name)
        ext_counter[ext] += 1
        d = size_timing.setdefault(bucket, {"count": 0, "total": 0.0})
        d["count"] += 1
        d["total"] += float(tc.attrib.get("time", 0))

    lines.append("=== TEST OVERVIEW ===")
    lines.append("")
    lines.append("  Extensions breakdown (All available tests)")
    for ext, count in ext_counter.most_common():
        lines.append(f"    {count:>8} {ext}")
    lines.append("")
    lines.append("  File sizes breakdown (All available tests)")
    lines.append(f"    {'Bucket':<15} {'Docs':>6} {'Total':>10} {'Avg':>8}")
    grand_docs = 0
    grand_total = 0.0
    for bucket in ["0KB  -  10KB", "10KB - 100KB", "100KB - 10MB", "10MB - 100MB"]:
        d = size_timing.get(bucket)
        if d and d["count"]:
            grand_docs += d["count"]
            grand_total += d["total"]
            avg = d["total"] / d["count"]
            lines.append(f"    {bucket:<15} {d['count']:>6} {d['total']:>8.1f}s {avg:>7.1f}s")
    if grand_docs:
        lines.append(f"    {'─' * 31}")
        lines.append(f"    {'Total':<15} {grand_docs:>6} {grand_total:>8.1f}s {grand_total / grand_docs:>7.1f}s")
    lines.append("")
    lines.append("")

    all_lines: List[str] = []
    for tc in test_cases:
        all_lines.extend(get_container_lines(tc))

    if all_lines:
        scrubbed = [scrub_container_line(line) for line in all_lines]
        filtered = [l for l in scrubbed if not is_expected_line(l)]
        counter = Counter(filtered)
        lines.append("=== MOST COMMON CONTAINER OUTPUT ===")
        lines.append("")
        lines.append("  Top 30:")
        for output, count in counter.most_common(30):
            lines.append(f"    {count:>5} {output}")
        lines.append("")
        lines.append("")

    fail_lines: List[str] = []
    for tc in test_cases:
        if get_test_status(tc) in ("FAIL", "ERROR"):
            fail_lines.extend(get_container_lines(tc))

    if fail_lines:
        scrubbed = [scrub_container_line(line) for line in fail_lines]
        filtered = [l for l in scrubbed if not is_expected_line(l)]
        counter = Counter(filtered)
        lines.append("=== FAILURE REASONS ===")
        lines.append("")
        lines.append("  All failures:")
        for output, count in counter.most_common():
            lines.append(f"    {count:>5} {output}")
        lines.append("")
        lines.append("")

    timeout_files: List[str] = []
    for tc in test_cases:
        output = extract_captured_text(tc, "system-out")
        if "TIMEOUT EXCEEDED" in output:
            m = re.search(r"'(.*?)'", output)
            if m:
                timeout_files.append(m.group(1))

    lines.append("=== TIMEOUTS ===")
    lines.append("")
    if timeout_files:
        lines.append(f"  Summary: {len(timeout_files)}")
        lines.append("")
        lines.append("  Affected files:")
        for f in timeout_files:
            lines.append(f"    - {f}")
    else:
        lines.append("  Summary: 0")
        lines.append("")
        lines.append("  Affected files:")
    lines.append("")
    lines.append("")

    failed_entries: List[Tuple[str, List[str]]] = []
    for tc in test_cases:
        if get_test_status(tc) in ("FAIL", "ERROR"):
            name = tc.attrib.get("name", "")
            m = re.search(r"\[([^\]]+)\]", name)
            fname = m.group(1) if m else name
            container_lines = get_container_lines(tc)
            scrubbed = [scrub_container_line(l) for l in container_lines]
            filtered = [l for l in scrubbed if not is_expected_line(l)]
            preview = filtered[:3]
            failed_entries.append((fname, preview))

    lines.append("=== FAILED FILES ===")
    lines.append("")
    if failed_entries:
        for fname, preview in sorted(failed_entries, key=lambda x: x[0]):
            lines.append(f"  - {fname}")
            for pline in preview:
                lines.append(f"      {pline}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    xml_file = sys.argv[1]
    print(generate_report(xml_file))
