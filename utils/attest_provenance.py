#!/usr/bin/env python3
"""
Module for verifying container image provenance attestations using Cosign.

Ported from the Dangerzone repo's dangerzone/updater/cosign.py
"""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import click

PROJECT_ROOT = Path(__file__).parent.parent
COSIGN_BINARY = PROJECT_ROOT / "helpers" / "cosign"

CUE_POLICY = r"""
// The predicateType field must match this string
predicateType: "https://slsa.dev/provenance/v0.2"

predicate: {{
  // This condition verifies that the builder is the builder we
  // expect and trust.
  builder: {{
    id: =~"^https://github.com/slsa-framework/slsa-github-generator/.github/workflows/generator_container_slsa3.yml@refs/tags/v[0-9]+.[0-9]+.[0-9]+$"
  }}
  invocation: {{
    configSource: {{
      // This condition verifies the entrypoint of the workflow.
      entryPoint: "{workflow}"

      // This condition verifies that the image was generated from
      // the source repository we expect.
      uri: =~"^git\\+https://github.com/{repository}"
    }}
  }}
}}
"""


def verify_attestation(
    image_name: str,
    repository: str,
    workflow: str,
) -> bool:
    """
    Verify that a container image has a valid SLSA Level 3 provenance
    attestation from the expected GitHub repository and workflow.
    """
    if not COSIGN_BINARY.exists():
        print(f"Error: cosign binary not found at {COSIGN_BINARY}")
        print("Run 'uvx mazette install cosign' to download it.")
        sys.exit(1)

    policy = CUE_POLICY.format(repository=repository, workflow=workflow)

    with NamedTemporaryFile(mode="w", suffix=".cue", delete=False) as policy_f:
        policy_f.write(policy)
        policy_f.flush()

        cmd = [
            str(COSIGN_BINARY),
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
            raise Exception(f"Attestation cannot be verified: {stderr}")
        finally:
            Path(policy_f.name).unlink(missing_ok=True)

    return True


@click.command()
@click.option("--image", required=True, help="Full image reference (e.g., ghcr.io/foo/bar@sha256:...)")
@click.option("--repository", default="freedomofpress/dangerzone-image")
@click.option("--workflow", default=".github/workflows/release-container-image.yml")
def main(image, repository, workflow):
    """Verify SLSA provenance attestation for a container image."""
    try:
        verify_attestation(image, repository, workflow)
        click.echo("Provenance attestation verified successfully")
        sys.exit(0)
    except Exception as e:
        click.echo(f"Provenance verification failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
