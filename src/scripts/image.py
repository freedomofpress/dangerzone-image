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

import argparse
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
from pathlib import Path
from tempfile import NamedTemporaryFile

from repro_build.repro_build import Builder, analyze_tarball

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent.parent
IMAGE_NAME = "ghcr.io/freedomofpress/dangerzone/v1"
BUILD_CONTEXT = "src"
CONTAINER_RUNTIME = "podman"
ANNOTATION_DATE = "rocks.dangerzone.debian_archive_date={date}"

CUE_POLICY = r"""
// The predicateType field must match this string
predicateType: "https://slsa.dev/provenance/v0.2"

predicate: {{
  builder: {{
    id: =~"^https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v[0-9]+.[0-9]+.[0-9]+$"
  }}
  invocation: {{
    configSource: {{
      entryPoint: "{workflow}"
      uri: =~"^git\\+https://github.com/{repository}"
    }}
  }}
}}
"""

REQUIRED_TOOLS = ["crane"]


def run_cmd(cmd, check=True, capture_output=False, dry=False, **kwargs):
    action = "Would have run" if dry else "Running"
    logger.debug(f"{action}: {shlex.join(cmd)}")
    if dry:
        return None
    return subprocess.run(
        cmd, check=check, capture_output=capture_output, text=True, **kwargs
    )


def ensure_tool(tool_name):
    tool_path = PROJECT_ROOT / "helpers" / tool_name / tool_name
    if not tool_path.exists():
        tool_path = PROJECT_ROOT / "helpers" / tool_name
    if not tool_path.exists():
        raise RuntimeError(
            f"'{tool_name}' is required but not found in helpers/. "
            f"Run 'uvx mazette install' to install it."
        )
    return str(tool_path)


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


def validate_commit_format(value):
    if value is None:
        return value
    if not re.match(r"^[0-9a-f]{40}$", value.lower()):
        raise argparse.ArgumentTypeError(
            f"Invalid commit hash format: {value}. Must be a complete SHA1 commit."
        )
    return value


def get_git_head():
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


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


def cmd_build(args):
    if not args.debian_archive_date:
        args.debian_archive_date = determine_debian_archive_date()

    tag = args.tag or f"{args.debian_archive_date}-{determine_git_tag()}"
    image_name_tagged = f"{IMAGE_NAME}:{tag}"

    print(f"Will tag the container image as '{image_name_tagged}'")

    image_id_path = PROJECT_ROOT / "image-id.txt"
    if not args.dry:
        with open(image_id_path, "w") as f:
            f.write(image_name_tagged)

    date_annotation = ANNOTATION_DATE.format(date=args.debian_archive_date)
    print("Will annotate the image with the following:")
    print(f"- {date_annotation}")

    print("Building container image")

    build_image(
        platform=args.platform,
        runtime=args.runtime,
        cache=args.use_cache,
        date=args.debian_archive_date,
        dry=args.dry,
        tag=image_name_tagged,
        output=args.output,
    )


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
            stderr = e.stderr.decode().strip()
            if not stderr:
                stderr = (
                    "cosign exited with no error output. This usually means:\n"
                    "  - The image tag doesn't have an attached SLSA attestation\n"
                    "  - Use the image digest (ghcr.io/repo@sha256:...) instead of a tag\n"
                    "  - The attestation may not exist for this image reference"
                )
            raise RuntimeError(f"Attestation cannot be verified: {stderr}")
        finally:
            Path(policy_f.name).unlink(missing_ok=True)

    return True


def cmd_verify_attestation(args):
    verify_attestation(args.image, args.repository, args.workflow)
    print("Provenance attestation verified successfully")


def get_debian_archive_date(digest):
    if "@sha256:" not in digest:
        raise RuntimeError(
            "Must pass full image name along with the digest to make autodetection work"
        )
    crane_binary = ensure_tool("crane")
    resp = (
        subprocess.run(
            [crane_binary, "manifest", digest],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    try:
        manifest = json.loads(resp)
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


def cmd_reproduce(args):
    date = args.debian_archive_date

    if args.debian_archive_date == "autodetect":
        logger.info("Autodetecting Debian archive date for image %s", args.digest)
        date = get_debian_archive_date(args.digest)
        logger.info("Successfully retrieved Debian archive date: %s", date)

    logger.info("Building container image")
    build_image(
        platform=args.platform,
        runtime=args.runtime,
        cache=not args.no_cache,
        date=date,
        dry=args.dry,
    )

    if not args.dry:
        logger.info(
            "Check that the reproduced image has the expected digest: %s", args.digest
        )
        tarball_path = PROJECT_ROOT / "container.tar"
        analyze_tarball(tarball_path, args.digest, show_contents=True)
    else:
        logger.info("Would analyze the tarball against digest: %s", args.digest)


def get_candidate_image(commit, image_name):
    crane_binary = ensure_tool("crane")
    short_commit = commit[:7]
    print(f"\nLooking for images for commit: {short_commit}")
    print(f"   Repository: {image_name}\n")

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

    print("Found image:")
    print(f"   {full_image}\n")
    return full_image


def get_platform_digests(full_image):
    crane_binary = ensure_tool("crane")
    print(f"\nGetting platform-specific digests for: {full_image}\n")

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

    print("Platform digests retrieved:")
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
        print(f"- {architecture}: {digest}")

    if len(platforms) != 2:
        raise RuntimeError(
            f"Unsupported number of platforms found: {len(platforms)} "
            f"({', '.join(platforms.keys())}). Expected exactly 2 (linux/amd64 and linux/arm64)."
        )

    return platforms


def reproduce_image(
    root_manifest, platform_name, platform_image_digest, temp_dir, dry=False
):
    image_base = root_manifest.split("@")[0]
    platform_image = f"{image_base}@{platform_image_digest}"

    print(f"\nReproducing image for platform: {platform_name}")
    print(f"   Root manifest: {root_manifest}")
    print(f"   Platform image digest: {platform_image_digest}")
    print(f"   Platform image: {platform_image}")
    print(f"   Repository path: {temp_dir}\n")

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
    print(f"\nImage reproduction successful for {platform_name}\n")


def sign_image(image, ghcr_signer_path):
    print(f"\nSigning image: {image}")
    print(f"   Using ghcr-signer at: {ghcr_signer_path}\n")

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

    print("\nImage signed successfully")
    print("Remember to:")
    print("   1. Create a PR with the signatures")
    print("   2. Wait for CI to pass")
    print("   3. Merge the PR\n")


def cmd_release(args):
    for tool in REQUIRED_TOOLS:
        ensure_tool(tool)

    if not shutil.which("git"):
        raise RuntimeError("'git' is required but not found in PATH")

    commit = args.commit or get_git_head()

    root_manifest = get_candidate_image(commit, args.image_name)

    print(f"\nAttesting provenance for image: {root_manifest}")
    verify_attestation(root_manifest, args.repository, args.workflow)
    print("\nProvenance attestation successful\n")

    digests = get_platform_digests(root_manifest)

    temp_dir = tempfile.mkdtemp(prefix="dangerzone-reproduce-")
    print(f"\nCreated temporary directory: {temp_dir}")

    try:
        print("Cloning Dangerzone repository...")
        run_cmd(
            [
                "git",
                "clone",
                f"https://github.com/{args.repository}.git",
                temp_dir,
            ],
            check=True,
        )
        print("Repository cloned")

        print(f"Checking out commit {commit}...")
        run_cmd(["git", "-C", temp_dir, "checkout", commit], check=True)

        for plat in ["linux/amd64", "linux/arm64"]:
            platform_digest = digests[plat]
            if platform_digest in args.skip_reproduction_for:
                print(
                    f"Skipping reproduction for platform {plat} (digest {platform_digest})"
                )
            else:
                reproduce_image(
                    root_manifest,
                    plat,
                    platform_digest,
                    temp_dir,
                    dry=args.dry,
                )
    finally:
        print(f"\nCleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

    if args.skip_signing:
        print("Skipping signing")
    else:
        sign_image(root_manifest, args.ghcr_signer_path)


def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ("yes", "true", "t", "y", "1"):
        return True
    elif v.lower() in ("no", "false", "f", "n", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError("Boolean value expected.")


def add_build_args(parser):
    parser.add_argument(
        "--runtime",
        choices=["docker", "podman"],
        default=CONTAINER_RUNTIME,
        help="The container runtime for building the image",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="The platform for building the image",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=str(Path("container.tar")),
        help="Path to store the container image",
    )
    parser.add_argument(
        "--use-cache",
        type=str2bool,
        nargs="?",
        default=True,
        const=True,
        help="Use the builder's cache to speed up the builds",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Provide a custom tag for the image (for development only)",
    )
    parser.add_argument(
        "--debian-archive-date",
        "-d",
        default=None,
        help="Use a specific Debian snapshot archive, by its date",
    )
    parser.add_argument(
        "--dry",
        default=False,
        action="store_true",
        help="Do not run any commands, just print what would happen",
    )


def create_parser():
    parser = argparse.ArgumentParser(
        prog="image",
        description="Unified tool for building, verifying, reproducing, and releasing Dangerzone container images",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    build_parser = subparsers.add_parser(
        "build",
        help="Build a reproducible container image",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    build_parser.set_defaults(func=cmd_build)
    add_build_args(build_parser)

    verify_parser = subparsers.add_parser(
        "verify-attestation",
        help="Verify SLSA provenance attestation for an image",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    verify_parser.set_defaults(func=cmd_verify_attestation)
    verify_parser.add_argument(
        "--image",
        required=True,
        help="Full image reference (e.g., ghcr.io/foo/bar@sha256:...)",
    )
    verify_parser.add_argument(
        "--repository",
        default="freedomofpress/dangerzone-image",
        help="The repository to use",
    )
    verify_parser.add_argument(
        "--workflow",
        default=".github/workflows/release-container-image.yml",
        help="The workflow to use",
    )

    reproduce_parser = subparsers.add_parser(
        "reproduce",
        help="Reproduce a container image and verify its digest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    reproduce_parser.set_defaults(func=cmd_reproduce)
    reproduce_parser.add_argument(
        "--platform",
        default=None,
        help="The platform for building the image",
    )
    reproduce_parser.add_argument(
        "--runtime",
        choices=["docker", "podman"],
        default=CONTAINER_RUNTIME,
        help="The container runtime for building the image",
    )
    reproduce_parser.add_argument(
        "--no-cache",
        default=False,
        action="store_true",
        help="Do not use existing cached images for the container build",
    )
    reproduce_parser.add_argument(
        "--debian-archive-date",
        default=None,
        help="Use a specific Debian snapshot archive, by its date, or 'autodetect'",
    )
    reproduce_parser.add_argument(
        "--dry",
        default=False,
        action="store_true",
        help="Do not run any commands, just print what would happen",
    )
    reproduce_parser.add_argument(
        "digest",
        help="The digest of the image that you want to reproduce",
    )

    release_parser = subparsers.add_parser(
        "release",
        help="Attest, reproduce, and release a container image",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    release_parser.set_defaults(func=cmd_release)
    release_parser.add_argument(
        "--commit",
        default=None,
        type=validate_commit_format,
        help="The full SHA1 commit to use",
    )
    release_parser.add_argument(
        "--ghcr-signer-path",
        required=True,
        help="Path to the ghcr-signer repository",
    )
    release_parser.add_argument(
        "--repository",
        default="freedomofpress/dangerzone-image",
        help="The repository to use",
    )
    release_parser.add_argument(
        "--workflow",
        default=".github/workflows/release.yml",
        help="The workflow to use",
    )
    release_parser.add_argument(
        "--image-name",
        default=IMAGE_NAME,
        help="The image name to use",
    )
    release_parser.add_argument(
        "--skip-reproduction-for",
        help="Digests to avoid reproducing",
        nargs="*",
        default=[],
    )
    release_parser.add_argument(
        "--skip-signing",
        help="Skip the generation of the signatures",
        action="store_true",
    )
    release_parser.add_argument(
        "--dry",
        default=False,
        action="store_true",
        help="Do not run any commands, just print what would happen",
    )

    return parser


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 1

    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
