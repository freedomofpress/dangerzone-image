#!/usr/bin/env python3
"""
Unified tool for building, verifying, reproducing, and releasing
Dangerzone container images.

Subcommands:
    build              Build a reproducible container image
    verify-attestation Verify SLSA provenance attestation for an image
    reproduce          Reproduce a container image and verify its digest
    release            Attest, reproduce, and release a container image
"""

import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

import click
from repro_build import Builder, analyze_tarball

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
IMAGE_NAME = "ghcr.io/freedomofpress/dangerzone/v1"
BUILD_CONTEXT = "src"
CONTAINER_RUNTIME = "podman"
ANNOTATION_DATE = "rocks.dangerzone.debian_archive_date={date}"
CACHE_DIR = Path(tempfile.gettempdir()) / "dangerzone-reproduce-cache"
CACHE_TTL = timedelta(hours=2)

# NOTE: You can grab the SLSA attestation for an image/tag pair with the following
# commands:
#
#     IMAGE=ghcr.io/freedomofpress/dangerzone/v1
#     TAG=20260427-0.10.0-55-ga6750d1
#     DIGEST=$(crane digest ${IMAGE?}:${TAG?})
#     ATT_MANIFEST=${IMAGE?}:${DIGEST/:/-}.att
#     ATT_BLOB=${IMAGE?}@$(crane manifest ${ATT_MANIFEST?} | jq -r '.layers[0].digest')
#     crane blob ${ATT_BLOB?} | jq -r '.payload' | base64 -d | jq
CUE_POLICY = r"""
// The predicateType field must match this string
predicateType: "https://slsa.dev/provenance/v0.2"

predicate: {{
  // This condition verifies that the builder is the builder we
  // expect and trust. The following condition can be used
  // unmodified. It verifies that the builder is the container
  // workflow.
  builder: {{
    id: =~"^https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v[0-9]+.[0-9]+.[0-9]+$"
  }}
  invocation: {{
    configSource: {{
      // This condition verifies the entrypoint of the workflow.
      // Replace with the relative path to your workflow in your
      // repository.
      entryPoint: "{workflow}"

      // This condition verifies that the image was generated from
      // the source repository we expect. Replace this with your
      // repository.
      uri: =~"^git\\+https://github.com/{repository}"
    }}
  }}
}}
"""


def run_cmd(cmd, check=True, capture_output=False, dry=False, **kwargs):
    action = "Would have run" if dry else "Running"
    logger.debug(f"{action}: {shlex.join(cmd)}")
    if dry:
        return None
    return subprocess.run(
        cmd, check=check, capture_output=capture_output, text=True, **kwargs
    )


def _is_dirty():
    result = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    )
    return bool(result.stdout.strip())


def _reproduce_cache_path(digest):
    digest = digest.replace("sha256:", "sha256-").replace("/", "_")
    return CACHE_DIR / f"{digest}.json"


def _consult_reproduce_cache(digest, commit, repository, image_name):
    path = _reproduce_cache_path(digest)
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    cached_time = datetime.fromisoformat(data["timestamp"])
    if datetime.utcnow() - cached_time > CACHE_TTL:
        logger.debug("Reproduce cache for %s is stale (TTL: %s)", digest, CACHE_TTL)
        return False
    return (
        data.get("commit") == commit
        and data.get("repository") == repository
        and data.get("image_name") == image_name
    )


def _write_reproduce_cache(digest, commit, repository, image_name):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _reproduce_cache_path(digest)
    path.write_text(
        json.dumps(
            {
                "commit": commit,
                "repository": repository,
                "image_name": image_name,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
    )


def _should_skip_reproduction(
    plat, platform_digest, skip_list, commit, repository, image_name, should_cache
):
    skip_msg = (
        f"⏩ Skipping reproduction for platform {plat} (digest: {platform_digest}). "
    )
    if platform_digest in skip_list:
        click.echo(skip_msg + "Explicitly skipped via --skip-reproduction-for.")
        return True
    if should_cache and _consult_reproduce_cache(
        platform_digest, commit, repository, image_name
    ):
        click.echo(skip_msg + "Has been reproduced recently.")
        return True
    return False


def locate_tool(tool_name):
    tool_path = PROJECT_ROOT / "helpers" / tool_name / tool_name
    if tool_path.exists():
        return str(tool_path)
    tool_path = PROJECT_ROOT / "helpers" / tool_name
    if tool_path.exists():
        return str(tool_path)
    return None


def ensure_tool(tool_name):
    tool = locate_tool(tool_name)
    if tool is not None:
        logger.debug("Found '%s' at '%s'", tool_name, tool)
        return tool

    logger.debug("'%s' not found locally, installing via Mazette", tool_name)
    result = subprocess.run(
        ["mazette", "install", tool_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"'{tool_name}' is required but not found in helpers/ and"
            f" could not be installed via Mazette: {result.stderr.strip()}"
        )
    logger.debug("Mazette install output: %s", result.stdout.strip())

    tool = locate_tool(tool_name)
    if tool is None:
        raise RuntimeError(
            f"'{tool_name}' was installed via Mazette but still not found in helpers/"
        )
    logger.debug("Using '%s' at '%s'", tool_name, tool)
    return str(tool)


def determine_git_tag():
    dirty_ident = secrets.token_hex(2)
    return (
        subprocess.check_output(
            [
                "git",
                "describe",
                "--long",
                "--first-parent",
                f"--dirty=-{dirty_ident}",
                "--always",
            ],
        )
        .decode()
        .strip()
        .rstrip("v")
    )


def determine_debian_archive_date():
    for env in (PROJECT_ROOT / "Dockerfile.env").read_text().split("\n"):
        if env.startswith("DEBIAN_ARCHIVE_DATE"):
            return env.split("=")[1]
    raise RuntimeError(
        "Could not find 'DEBIAN_ARCHIVE_DATE' build argument in Dockerfile.env"
    )


def get_git_head():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def verify_attestation(image_name, repository, workflow):
    cosign_binary = ensure_tool("cosign")

    policy = CUE_POLICY.format(repository=repository, workflow=workflow)

    with NamedTemporaryFile(mode="w", suffix=".cue", delete=False) as policy_f:
        policy_f.write(policy)
        policy_f.flush()

        cmd = [
            cosign_binary,
            "verify-attestation",
            "--type",
            "slsaprovenance",
            "--policy",
            policy_f.name,
            "--certificate-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            "--certificate-identity-regexp",
            r"^https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v[0-9]+.[0-9]+.[0-9]+$",
            image_name,
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, env=os.environ.copy())
        except subprocess.CalledProcessError as e:
            raise Exception(f"Attestation cannot be verified: {e.stderr.decode()}")
        finally:
            Path(policy_f.name).unlink(missing_ok=True)

    return True


def get_debian_archive_date(digest):
    if "@sha256:" not in digest:
        raise RuntimeError(
            "Must pass full image name along with the digest to make autodetection work"
        )
    crane_binary = ensure_tool("crane")
    result = run_cmd(
        [crane_binary, "manifest", digest],
        capture_output=True,
        check=True,
    )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse manifest JSON for {digest}: {e}") from e

    annotations = manifest.get("annotations")
    if not annotations:
        raise RuntimeError(
            f"Image {digest} is a multi-platform manifest index and does not contain "
            "top-level annotations. Use a specific platform image digest instead "
            "(e.g., ghcr.io/repo@sha256:<platform-digest>), or provide the date manually "
            "with --debian-archive-date."
        )

    date = annotations.get("rocks.dangerzone.debian_archive_date")
    if not date:
        raise RuntimeError(
            f"Image {digest} does not have the expected "
            "'rocks.dangerzone.debian_archive_date' annotation. "
            f"Available annotations: {list(annotations.keys())}"
        )

    return date


def reproduce_image(*, platform, runtime, cache, date, digest, dry=False):
    build_image(
        platform=platform,
        runtime=runtime,
        cache=cache,
        date=date,
        dry=dry,
    )
    if dry:
        logger.info("Would analyze the tarball against digest: %s", digest)
        return
    tarball_path = PROJECT_ROOT / "container.tar"
    analyze_tarball(tarball_path, digest, show_contents=True)


def get_candidate_image(commit, image_name):
    crane_binary = ensure_tool("crane")
    short_commit = commit[:7]
    click.echo(f"\n📦 Looking for images for commit: {short_commit}")
    click.echo(f"   Repository: {image_name}\n")

    result = run_cmd(
        [crane_binary, "ls", "--full-ref", image_name],
        capture_output=True,
        check=True,
    )

    if not result or not result.stdout:
        raise RuntimeError(
            f"No images found in repository {image_name}. "
            "Check that the repository name is correct and you have access to it."
        )

    images = [line for line in result.stdout.splitlines() if short_commit in line]

    if not images:
        raise RuntimeError(
            f"No images found for commit {short_commit} in {image_name}. "
            f"Available tags: {result.stdout.splitlines()[:10]}"
            + ("..." if len(result.stdout.splitlines()) > 10 else "")
        )

    latest_image = images[-1]

    result = run_cmd(
        [crane_binary, "digest", latest_image],
        capture_output=True,
        check=True,
    )
    if not result or not result.stdout.strip():
        raise RuntimeError(f"Failed to get digest for image {latest_image}")
    digest = result.stdout.strip()

    image_base = latest_image.split(":")[0]
    full_image = f"{image_base}@{digest}"

    click.echo("✅ Found image:")
    click.echo(f"   {full_image}\n")
    return full_image


def get_platform_digests(full_image):
    crane_binary = ensure_tool("crane")
    click.echo(f"\n📋 Getting platform-specific digests for: {full_image}\n")

    result = run_cmd(
        [crane_binary, "manifest", full_image],
        capture_output=True,
        check=True,
    )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse manifest JSON for {full_image}: {e}"
        ) from e

    manifests_list = manifest.get("manifests")
    if not manifests_list:
        raise RuntimeError(
            f"Image {full_image} does not appear to be a multi-platform manifest index. "
            "Expected 'manifests' key in the manifest JSON."
        )

    click.echo("✅ Platform digests retrieved:")
    platforms = {}
    for m in manifests_list:
        try:
            plat_key = f"{m['platform']['os']}/{m['platform']['architecture']}"
            plat_digest = m["digest"]
        except KeyError as e:
            raise RuntimeError(
                f"Invalid manifest entry in {full_image}: missing key {e}. Entry: {m}"
            ) from e
        platforms[plat_key] = plat_digest

    for architecture, digest in platforms.items():
        click.echo(f"- {architecture}: {digest}")

    if len(platforms) != 2:
        raise RuntimeError(
            f"Unsupported number of platforms found: {len(platforms)} "
            f"({', '.join(platforms.keys())}). Expected exactly 2 (linux/amd64 and linux/arm64)."
        )

    return platforms


def run_reproduce_cmd_in_tmpdir(
    root_manifest, platform_name, platform_image_digest, temp_dir, dry=False
):
    image_base = root_manifest.split("@")[0]
    platform_image = f"{image_base}@{platform_image_digest}"

    click.echo(f"\n🔄 Reproducing image for platform: {platform_name}")
    click.echo(f"   Root manifest: {root_manifest}")
    click.echo(f"   Platform image digest: {platform_image_digest}")
    click.echo("   Debian archive date: (autodetect)")
    click.echo(f"   Platform image: {platform_image}")
    click.echo(f"   Repository path: {temp_dir}\n")

    run_cmd(
        [
            sys.executable,
            "-m",
            "scripts.image",
            "reproduce",
            "--debian-archive-date",
            "autodetect",
            "--platform",
            platform_name,
            platform_image,
        ],
        cwd=temp_dir,
        check=True,
        dry=dry,
    )
    click.echo(f"\n✅ Image reproduction successful for {platform_name}\n")


def sign_image(image, ghcr_signer_path):
    click.echo(f"\n✍️  Signing image: {image}")
    click.echo(f"   Using ghcr-signer at: {ghcr_signer_path}\n")

    ghcr_signer_script = Path(ghcr_signer_path) / "ghcr-signer.py"

    if not ghcr_signer_script.exists():
        raise RuntimeError(f"ghcr-signer.py not found at {ghcr_signer_script}")

    run_cmd(
        [
            sys.executable,
            str(ghcr_signer_script),
            "prepare",
            "--sk",
            image,
        ],
        check=True,
    )

    click.echo("\n✅ Image signed successfully")
    click.echo("⚠️  Remember to:")
    click.echo("   1. Create a PR with the signatures")
    click.echo("   2. Wait for CI to pass")
    click.echo("   3. Merge the PR\n")


def validate_commit_callback(ctx, param, value):
    if value is None:
        return value
    if not re.match(r"^[0-9a-f]{40}$", value.lower()):
        raise click.BadParameter(
            f"Invalid commit hash format: {value}. Must be a complete SHA1 commit."
        )
    return value


def build_image(
    *,
    platform=None,
    runtime=None,
    cache=True,
    date=None,
    dry=False,
    tag=None,
    output=None,
):
    date_annotation = ANNOTATION_DATE.format(date=date)
    builder = Builder(
        context=str(PROJECT_ROOT / BUILD_CONTEXT),
        runtime=runtime or CONTAINER_RUNTIME,
        datetime=date,
        no_cache=not cache,
        file=str(PROJECT_ROOT / "Dockerfile"),
        output=output or str(PROJECT_ROOT / "container.tar"),
        tag=tag,
        build_arg=[f"DEBIAN_ARCHIVE_DATE={date}"],
        annotation=[date_annotation],
        platform=platform,
        dry=dry,
    )
    builder.build()


@click.group(context_settings={"show_default": True})
@click.option("-v", "--verbose", count=True, default=0, help="Increase verbosity")
def cli(verbose):
    """Unified tool for building, verifying, reproducing, and releasing Dangerzone container images."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


@cli.command()
@click.option(
    "--runtime",
    type=click.Choice(["docker", "podman"]),
    default=CONTAINER_RUNTIME,
    help="The container runtime for building the image",
)
@click.option(
    "--platform",
    default=None,
    help="The platform for building the image",
)
@click.option(
    "--output",
    "-o",
    default=str(Path("container.tar")),
    help="Path to store the container image",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Do not use existing cached images for the container build",
)
@click.option(
    "--tag",
    default=None,
    help="Provide a custom tag for the image (for development only)",
)
@click.option(
    "--debian-archive-date",
    "-d",
    default=None,
    help="Use a specific Debian snapshot archive, by its date",
)
@click.option(
    "--dry",
    is_flag=True,
    default=False,
    help="Do not run any commands, just print what would happen",
)
def build(runtime, platform, output, no_cache, tag, debian_archive_date, dry):
    """Build a reproducible container image."""
    logger.info("Building container image")
    if not debian_archive_date:
        debian_archive_date = determine_debian_archive_date()

    tag = tag or f"{debian_archive_date}-{determine_git_tag()}"
    image_name_tagged = f"{IMAGE_NAME}:{tag}"

    click.echo(f"Will tag the container image as '{image_name_tagged}'")

    image_id_path = PROJECT_ROOT / "image-id.txt"
    if not dry:
        with open(image_id_path, "w") as fh:
            fh.write(image_name_tagged)

    date_annotation = ANNOTATION_DATE.format(date=debian_archive_date)
    click.echo("Will annotate the image with the following:")
    click.echo(f"- {date_annotation}")

    click.echo("Building container image")

    build_image(
        platform=platform,
        runtime=runtime,
        cache=not no_cache,
        date=debian_archive_date,
        dry=dry,
        tag=image_name_tagged,
        output=output,
    )


@cli.command("verify-attestation")
@click.option(
    "--image",
    required=True,
    help="Full image reference (e.g., ghcr.io/foo/bar@sha256:...)",
)
@click.option(
    "--repository",
    default="freedomofpress/dangerzone-image",
    help="The repository to use",
)
@click.option(
    "--workflow",
    default=".github/workflows/release.yml",
    help="The workflow to use",
)
def verify_attestation_cmd(image, repository, workflow):
    """Verify SLSA provenance attestation for an image."""
    logger.info("Verifying SLSA provenance attestation for image %s", image)
    ensure_tool("cosign")
    verify_attestation(image, repository, workflow)
    click.echo("✅ Provenance attestation verified successfully")


@cli.command()
@click.option(
    "--platform",
    default=None,
    help="The platform for building the image",
)
@click.option(
    "--runtime",
    type=click.Choice(["docker", "podman"]),
    default=CONTAINER_RUNTIME,
    help="The container runtime for building the image",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Do not use existing cached images for the container build",
)
@click.option(
    "--debian-archive-date",
    default=None,
    help="Use a specific Debian snapshot archive, by its date, or 'autodetect'",
)
@click.option(
    "--dry",
    is_flag=True,
    default=False,
    help="Do not run any commands, just print what would happen",
)
@click.argument("digest")
def reproduce(platform, runtime, no_cache, debian_archive_date, dry, digest):
    """Reproduce a container image and verify its digest."""
    logger.info("Reproducing container image for digest %s", digest)
    ensure_tool("crane")
    date = debian_archive_date

    if debian_archive_date == "autodetect":
        logger.info("Autodetecting Debian archive date for image %s", digest)
        date = get_debian_archive_date(digest)
        logger.info("Successfully retrieved Debian archive date: %s", date)

    logger.info("Building container image")
    reproduce_image(
        platform=platform,
        runtime=runtime,
        cache=not no_cache,
        date=date,
        digest=digest,
        dry=dry,
    )


@cli.command()
@click.option(
    "--commit",
    default=None,
    callback=validate_commit_callback,
    help="The full SHA1 commit to use",
)
@click.option(
    "--ghcr-signer-path",
    required=True,
    type=click.Path(exists=True),
    help="Path to the ghcr-signer repository",
)
@click.option(
    "--repository",
    default="freedomofpress/dangerzone-image",
    help="The repository to use",
)
@click.option(
    "--workflow",
    default=".github/workflows/release.yml",
    help="The workflow to use",
)
@click.option(
    "--image-name",
    default=IMAGE_NAME,
    help="The image name to use",
)
@click.option(
    "--skip-reproduction-for",
    multiple=True,
    default=[],
    help="Digests to avoid reproducing",
)
@click.option(
    "--skip-signing",
    is_flag=True,
    default=False,
    help="Skip the generation of the signatures",
)
@click.option(
    "--dry",
    is_flag=True,
    default=False,
    help="Do not run any commands, just print what would happen",
)
def release(
    commit,
    ghcr_signer_path,
    repository,
    workflow,
    image_name,
    skip_reproduction_for,
    skip_signing,
    dry,
):
    """Attest, reproduce, and release a container image."""
    logger.info("Starting release process for commit %s", commit or "(HEAD)")
    for tool in ["crane", "cosign"]:
        ensure_tool(tool)

    if not shutil.which("git"):
        raise RuntimeError("'git' is required but not found in PATH")

    commit_provided = commit is not None
    commit = commit or get_git_head()
    should_cache = commit_provided or not _is_dirty()

    root_manifest = get_candidate_image(commit, image_name)

    click.echo(f"\n🔐 Attesting provenance for image: {root_manifest}")
    verify_attestation(root_manifest, repository, workflow)
    click.echo("\n✅ Provenance attestation successful\n")

    digests = get_platform_digests(root_manifest)

    temp_dir = tempfile.mkdtemp(prefix="dangerzone-reproduce-")
    click.echo(f"\n📁 Created temporary directory: {temp_dir}")

    try:
        click.echo("📥 Cloning Dangerzone repository...")
        run_cmd(
            [
                "git",
                "clone",
                f"https://github.com/{repository}.git",
                temp_dir,
            ],
            check=True,
        )
        click.echo("✅ Repository cloned")

        click.echo(f"🔀 Checking out commit {commit}...")
        run_cmd(["git", "-C", temp_dir, "checkout", commit], check=True)

        for plat in ["linux/amd64", "linux/arm64"]:
            platform_digest = digests[plat]
            if _should_skip_reproduction(
                plat,
                platform_digest,
                skip_reproduction_for,
                commit,
                repository,
                image_name,
                should_cache,
            ):
                continue
            run_reproduce_cmd_in_tmpdir(
                root_manifest,
                plat,
                platform_digest,
                temp_dir,
                dry=dry,
            )
            if (
                should_cache
                and not dry
                and platform_digest not in skip_reproduction_for
            ):
                _write_reproduce_cache(platform_digest, commit, repository, image_name)
    finally:
        click.echo(f"\n🧹 Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    if skip_signing:
        click.echo("⏩ Skipping signing")
    else:
        sign_image(root_manifest, ghcr_signer_path)


if __name__ == "__main__":
    cli()
