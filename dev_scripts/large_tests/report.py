#!/usr/bin/env python3

import re
import sys
from collections import Counter
from typing import Dict, List, Tuple

import xml.etree.ElementTree as ET


# Pattern to scrub variable data (dates, hex IDs, numbers) for grouping
VARIABLE_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
    r"|\b[0-9a-f]{8,}\b"
    r"|\b\d{4}/\d{2}/\d{2}\b"
    r"|\b\d{2}:\d{2}:\d{2}\b"
    r'|(?<=file\s)\S+\.pdf'
    r"|\bpage\s+\d+"
    r"|\bpages\s+\d+"
)


def scrub_text(text: str) -> str:
    """Replace variable data with placeholders for better grouping."""
    return VARIABLE_PATTERN.sub("X", text)


def parse_junit(xml_file: str) -> ET.Element:
    tree = ET.parse(xml_file)
    return tree.getroot()


def count_results(root: ET.Element) -> Dict[str, int]:
    testsuite = root.find("testsuite")
    if testsuite is None:
        return {"errors": 0, "failures": 0, "skipped": 0, "tests": 0}
    return {
        "errors": int(testsuite.attrib.get("errors", "0")),
        "failures": int(testsuite.attrib.get("failures", "0")),
        "skipped": int(testsuite.attrib.get("skipped", "0")),
        "tests": int(testsuite.attrib.get("tests", "0")),
    }


def get_test_overview(root: ET.Element) -> List[Tuple[str, str]]:
    testsuite = root.find("testsuite")
    results = []
    if testsuite is not None:
        for testcase in testsuite.findall("testcase"):
            name = testcase.attrib.get("name", "unknown")
            classname = testcase.attrib.get("classname", "")
            full_name = f"{classname}::{name}" if classname else name
            failure = testcase.find("failure")
            error = testcase.find("error")
            if failure is not None:
                status = "FAIL"
            elif error is not None:
                status = "ERROR"
            else:
                status = "PASS"
            results.append((full_name, status))
    return results


def get_container_outputs(root: ET.Element) -> List[str]:
    outputs = []
    testsuite = root.find("testsuite")
    if testsuite is not None:
        for testcase in testsuite.findall("testcase"):
            for child in ("failure", "error"):
                elem = testcase.find(child)
                if elem is not None and elem.text:
                    outputs.append(scrub_text(elem.text.strip()))
    return outputs


def generate_report(xml_file: str) -> str:
    root = parse_junit(xml_file)
    results = count_results(root)
    total = results["tests"]
    failures = results["failures"]
    errors = results["errors"]
    skipped = results["skipped"]
    failure_rate = failures / total if total > 0 else 0.0

    lines = []
    lines.append("==== RESULTS SUMMARY ===")
    lines.append(f"    errors: {errors}")
    lines.append(f"    failures: {failures}")
    lines.append(f"    skipped: {skipped}")
    lines.append(f"    tests: {total}")
    lines.append(f"    failure rate: {failure_rate}")
    lines.append("")
    lines.append("")

    # Test overview
    overview = get_test_overview(root)
    pass_count = sum(1 for _, s in overview if s == "PASS")
    fail_count = sum(1 for _, s in overview if s in ("FAIL", "ERROR"))
    lines.append("=== TEST OVERVIEW ===")
    lines.append(f"  Total: {len(overview)}  Passed: {pass_count}  Failed: {fail_count}")
    if fail_count > 0:
        lines.append("")
        lines.append("  Failures:")
        for name, status in overview:
            if status in ("FAIL", "ERROR"):
                lines.append(f"    [{status}] {name}")
    lines.append("")
    lines.append("")

    # Most common container output
    outputs = get_container_outputs(root)
    if outputs:
        counter = Counter(outputs)
        lines.append("=== MOST COMMON CONTAINER OUTPUT ===")
        lines.append("")
        lines.append("  Top 30:")
        for output, count in counter.most_common(30):
            lines.append(f"    {count:>5} {output}")
        lines.append("")
        lines.append("")

        # Failure reasons
        lines.append("=== FAILURE REASONS ===")
        lines.append("")
        lines.append("  All failures:")
        for output, count in counter.most_common():
            lines.append(f"    {count:>5} {output[:120]}")

    # Timeouts (not directly in JUnit, but useful)
    lines.append("")
    lines.append("")
    lines.append("=== TIMEOUTS ===")
    lines.append("  (Not available from JUnit XML)")

    return "\n".join(lines)


if __name__ == "__main__":
    xml_file = sys.argv[1]
    print(generate_report(xml_file))
