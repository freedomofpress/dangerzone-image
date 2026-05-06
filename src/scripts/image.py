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


@click.group(context_settings={"show_default": True})
def cli():
    """Unified tool for building, verifying, reproducing, and releasing Dangerzone container images."""
    logging.basicConfig(
        level=logging.DEBUG,
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


if __name__ == "__main__":
    cli()
