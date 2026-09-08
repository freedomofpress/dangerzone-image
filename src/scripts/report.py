"""Generate a Markdown report with container build info and CVE comparison."""

import json
import logging
import os
import subprocess
from pathlib import Path

import click
import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent

CVES_DASHBOARD = "https://cves.dangerzone.rocks"
LATEST_CVES_URL = f"{CVES_DASHBOARD}/grype.json"
NIGHTLY_CVES_URL = f"{CVES_DASHBOARD}/nightly/grype.json"

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low"]

REPO = "freedomofpress/dangerzone-image"
ACTIONS_URL = f"https://github.com/{REPO}/actions/workflows"
API_RUNS_URL = f"https://api.github.com/repos/{REPO}/actions/workflows"

# Nightly workflow that builds and publishes the container image from `main`.
RELEASE_WORKFLOW = "release.yml"
# Workflow that runs the CI tests for the container image.
CI_WORKFLOW = "ci.yml"


def _find_workflow_run(workflow_file, date_str):
    """Return the html_url of the `main` run of a workflow for a given date.

    The nightly image is built and tested by scheduled runs on `main`, so we look
    up the latest *scheduled* run of ``workflow_file`` on the ``main`` branch that
    was created on ``date_str`` (a ``YYYY-MM-DD`` string).

    Falls back to the workflow's run listing on any lookup failure, so report
    generation never breaks because of a transient API error or rate limit.
    """
    listing_url = f"{ACTIONS_URL}/{workflow_file}"
    url = f"{API_RUNS_URL}/{workflow_file}/runs"
    params = {"branch": "main", "created": date_str, "event": "schedule"}
    click.echo(f"  Looking up {workflow_file} run for {date_str}...", err=True)

    headers = {"Accept": "application/vnd.github+json"}
    # Use a token when available to avoid the low unauthenticated rate limit.
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
    except (requests.RequestException, json.JSONDecodeError) as e:
        click.echo(f"  Lookup failed ({e}); linking to run listing.", err=True)
        return listing_url

    if not runs:
        click.echo("  No scheduled run found; linking to run listing.", err=True)
        return listing_url

    # The API returns the most recent run first.
    run = runs[0]
    click.echo(f"  Found run: {run['html_url']}", err=True)
    return run["html_url"]


def _generate_checklist(date_str):
    release_url = _find_workflow_run(RELEASE_WORKFLOW, date_str)
    ci_url = _find_workflow_run(CI_WORKFLOW, date_str)
    return f"""\
## Checklist

- [ ] Make sure that the above digests are the latest ones in https://ghcr.io/freedomofpress/dangerzone/v1.
- [ ] The image is the latest one built by our CI ([run]({release_url}))
- [ ] The CI tests pass for this image ([run]({ci_url}))
- [ ] The reported security vulnerabilities do not impact Dangerzone, or can't be avoided for now.
"""


def _locate_crane():
    """Find the crane binary via Mazette helpers."""
    for candidate in (
        PROJECT_ROOT / "helpers" / "crane" / "crane",
        PROJECT_ROOT / "helpers" / "crane",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _find_latest_signatures_dir(signatures_dir):
    """Find the most recent directory with signatures."""
    signatures_path = Path(signatures_dir)
    click.echo(f"Scanning {signatures_dir} for latest signature directory...", err=True)
    dirs = [d for d in signatures_path.iterdir() if d.is_dir()]
    if not dirs:
        raise click.ClickException(
            f"No signature directories found in {signatures_path}"
        )
    latest = sorted(dirs, reverse=True)[0]
    click.echo(f"Latest signature directory: {latest.name}", err=True)
    return latest


def _identify_images(latest_dir):
    """Distinguish the root manifest from the platform manifests.

    Find the root manifest in the signatures directory, by looking for the
    ``LATEST`` marker. The rest of the manifests should be the platform ones.
    """
    click.echo(
        f"Identifying root and platform images in {latest_dir.name}...", err=True
    )
    hash_dirs = sorted([d for d in latest_dir.iterdir() if d.is_dir()])
    if not hash_dirs:
        raise click.ClickException(f"No image directories in {latest_dir}")

    root_dir = None
    platform_dirs = []

    for d in hash_dirs:
        if (d / "LATEST").exists():
            root_dir = d
        else:
            platform_dirs.append(d)

    if root_dir is None:
        raise click.ClickException(f"No LATEST marker found in {latest_dir}")
    if len(platform_dirs) != 2:
        raise click.ClickException(
            f"Found more than two platform images: "
            f"{', '.join(d.name for d in platform_dirs)}",
        )

    click.echo(f"  Root image digest: {root_dir.name}", err=True)
    click.echo("  Platform image digests:", err=True)
    click.echo(f"  - {platform_dirs[0].name}", err=True)
    click.echo(f"  - {platform_dirs[1].name}", err=True)
    return root_dir, platform_dirs


def _read_image_ref(image_dir):
    """Get the image reference from the directories."""
    image_file = image_dir / "IMAGE"
    if not image_file.exists():
        raise click.ClickException(f"IMAGE file not found in {image_dir}")
    ref = image_file.read_text().strip()
    click.echo(f"  Read image reference: {ref[:80]}...", err=True)
    return ref


def _run_crane(crane_path, args):
    """Run the crane command."""
    cmd = [crane_path] + args
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip()


def _lookup_platforms_with_crane(crane_path, root_ref, platform_dirs):
    """Retrieve the architecture of a platform manifest from the root manifest."""
    click.echo("Fetching multi-arch manifest to identify platforms...", err=True)
    manifest_output = _run_crane(crane_path, ["manifest", root_ref])

    try:
        manifest = json.loads(manifest_output)
    except json.JSONDecodeError:
        raise click.ClickException("Failed to parse manifest JSON")

    manifests = manifest.get("manifests", [])
    if not manifests:
        raise click.ClickException("No platform manifests found in multi-arch image")

    arch_map = {}
    for entry in manifests:
        platform = entry.get("platform", {})
        arch = platform.get("architecture", "")
        os_name = platform.get("os", "")
        entry_digest = entry["digest"].replace("sha256:", "")
        if arch and os_name:
            arch_map[entry_digest] = f"{os_name}/{arch}"

    result = {}
    for d in platform_dirs:
        if d.name in arch_map:
            result[d.name] = arch_map[d.name]
            click.echo(f"  {d.name[:12]}... -> {arch_map[d.name]}", err=True)
        else:
            result[d.name] = None
            click.echo(f"  {d.name[:12]}... -> unknown platform", err=True)

    return result


def _find_smallest_logindex(latest_dir):
    """Find the smallest logIndex across all MANIFEST files in the signatures dir."""
    click.echo(f"Extracting logIndex values from {latest_dir.name}...", err=True)
    hash_dirs = sorted([d for d in latest_dir.iterdir() if d.is_dir()])
    min_index = float("inf")

    for d in hash_dirs:
        manifest_file = d / "MANIFEST"
        if not manifest_file.exists():
            continue
        try:
            manifest = json.loads(manifest_file.read_text())
            bundle_str = (
                manifest.get("layers", [{}])[0]
                .get("annotations", {})
                .get("dev.sigstore.cosign/bundle", "")
            )
            if not bundle_str:
                continue
            bundle = json.loads(bundle_str)
            log_index = bundle.get("Payload", {}).get("logIndex", -1)
            if log_index < min_index:
                min_index = log_index
        except (json.JSONDecodeError, KeyError, IndexError):
            continue

    return min_index


def _generate_build_info(date, root_ref, platform_info, platform_dirs):
    lines = []
    lines.append(
        f"This PR updates the Dangerzone container image to the `{date}` build.\n"
    )
    lines.append("\n")
    lines.append("## Build info\n")
    lines.append("\n")
    lines.append(f"- Root image: `{root_ref}`\n")

    for d in sorted(platform_dirs, key=lambda x: x.name):
        platform_label = platform_info.get(d.name)
        if platform_label is None:
            platform_label = f"platform ({d.name[:12]}...)"
        platform_ref = _read_image_ref(d)
        lines.append(f"- `{platform_label}` image: `{platform_ref}`\n")

    return "".join(lines)


def _fetch_grype_json(url):
    """Fetch Grype JSON data from a URL."""
    import urllib.error
    import urllib.request

    click.echo(f"  Fetching {url}...", err=True)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        raise click.ClickException(f"Failed to fetch or parse {url}: {e}")
    return _parse_grype_data(data)


def _parse_grype_data(data):
    matches = data.get("matches", [])
    result = {}

    for match in matches:
        vuln = match.get("vulnerability", {})
        severity = vuln.get("severity", "Unknown")
        cve_id = vuln.get("id", "")
        fix_state = vuln.get("fix", {}).get("state", "")

        if not cve_id:
            continue

        if severity not in result:
            result[severity] = {}

        if cve_id not in result[severity]:
            result[severity][cve_id] = {
                "fix_state": fix_state,
            }

    return result


def _compare_cves(latest, nightly):
    fixed_cves = {}
    pending_cves = {}

    for severity in SEVERITY_ORDER:
        _latest_cves = set(latest.get(severity, {}).keys())
        _nightly_cves = set(nightly.get(severity, {}).keys())

        fixed_cves[severity] = _latest_cves - _nightly_cves

        pending_cves[severity] = {
            cve
            for cve in _nightly_cves
            if nightly[severity][cve]["fix_state"] != "wont-fix"
        }

    return fixed_cves, pending_cves


def _generate_cve_section(title, cve_by_severity):
    total = sum(len(v) for v in cve_by_severity.values())
    lines = []

    if total == 0:
        lines.append(f"## No {title.lower()}\n")
        return "".join(lines)

    lines.append("<details>\n")
    lines.append(f"<summary><h3>{title} ({total})</h3></summary>\n")
    lines.append("\n")

    for severity in SEVERITY_ORDER:
        cves = cve_by_severity.get(severity, {})
        if not cves:
            continue

        lines.append(f"### {severity}\n")
        lines.append("\n")
        for cve_id in sorted(cves):
            url = f"https://security-tracker.debian.org/tracker/{cve_id}"
            lines.append(f"* [{cve_id}]({url})\n")
        lines.append("\n")

    lines.append("</details>\n")

    return "".join(lines)


def generate_report(signatures_dir):
    """Generate a Markdown report from the signatures directory.

    Args:
        signatures_dir: Path to the signatures directory of GHCR signer.

    Returns:
        The report as a string.
    """
    crane_path = _locate_crane()
    if crane_path is None:
        raise click.ClickException(
            "crane not found. Run `mazette install crane` or ensure it is in helpers/"
        )

    latest_dir = _find_latest_signatures_dir(signatures_dir)
    root_dir, platform_dirs = _identify_images(latest_dir)
    date_str = latest_dir.name[:10]
    click.echo(f"Build date: {date_str}", err=True)

    click.echo("\nReading image references...", err=True)
    root_ref = _read_image_ref(root_dir)

    click.echo("\nResolving platform architectures...", err=True)
    platform_info = _lookup_platforms_with_crane(crane_path, root_ref, platform_dirs)

    click.echo("\nFinding smallest logIndex...", err=True)
    min_logindex = _find_smallest_logindex(latest_dir)
    click.echo(f"  Smallest logIndex: {min_logindex}", err=True)

    click.echo("\nGenerating build info section...", err=True)
    build_info = _generate_build_info(date_str, root_ref, platform_info, platform_dirs)
    build_info += f"\n- Smallest Rekor logIndex: `{min_logindex}`\n"

    click.echo("\nFetching vulnerability scans...", err=True)
    latest_cves = _fetch_grype_json(LATEST_CVES_URL)
    nightly_cves = _fetch_grype_json(NIGHTLY_CVES_URL)

    total_latest = sum(len(v) for v in latest_cves.values())
    total_nightly = sum(len(v) for v in nightly_cves.values())
    click.echo(f"  Latest:  {total_latest} unique CVE(s)", err=True)
    click.echo(f"  Nightly: {total_nightly} unique CVE(s)", err=True)

    click.echo("Comparing CVEs...", err=True)
    fixed, pending = _compare_cves(latest_cves, nightly_cves)

    click.echo(f"  Fixed:       {len(fixed)} CVE(s)", err=True)
    click.echo(f"  Pending fix: {len(pending)} CVE(s)", err=True)

    click.echo("Generating CVE report sections...", err=True)
    cve_report = "## CVEs\n\n"
    cve_report += _generate_cve_section("Fixed", fixed)
    cve_report += "\n"
    cve_report += _generate_cve_section("Pending fixes", pending)

    click.echo("\nResolving workflow run links...", err=True)
    checklist = _generate_checklist(date_str)

    report = build_info + "\n" + cve_report + "\n" + checklist
    return report
