# Dangerzone-image

This repository contains the dangerzone container image that is used to perform "document to pixels" conversions. This container is used by [dangerzone](https://dangerzone.rocks) to securely convert its documents.

## Using the container image

The image is published on a monthly basis on the container registry, alongside their Cosign signatures.
Additionally, nightly and development branches are published under the `dangerzone-testing` namespace.

| Channel | Location                               | Signed?    | Use it for  |
| ------- | -------------------------------------- | ---------- | ----------- |
| Stable  | [`ghcr.io/freedomofpress/dangerzone/v1`](https://ghcr.io/freedomofpress/dangerzone/v1) | ✅ ([prod keys](/freedomofpress-dangerzone.pub))  | Production  |
| Nightly | [`ghcr.io/freedomofpress/dangerzone-testing/main/v1`](https://ghcr.io/freedomofpress/dangerzone-testing/main/v1) | ✅ ([testing keys](/tests/assets/dangerzone-testing.pub))  | Development |
| Branch  | `ghcr.io/freedomofpress/dangerzone-testing/<branch-name>/v1` | ✅ ([testing keys](/tests/assets/dangerzone-testing.pub))  | Development |

## What this container provides

This container provides a way to convert documents to pixel buffers, using a secure sandbox.

The security of the sandbox is provided by different layers:

- The container uses [gVisor](/docs/gvisor.md), an application Kernel that provides a strong layer of isolation between running applications and the host operating system. It is written in a memory-safe language (Go) and runs in userspace.
- Additionally, it is expected that this container is run with specific flags and a specific seccomp policy, to unsure that users are not mapped in the container, that no network is available in the container, etc. See the "how to use" section.

We also provide the following guarantees, related to the distribution of the image:

- The container is [signed](/docs/sign-image.md) in an auditable way, using Cosign
- Ultimately, the container is [reproducible](/docs/reproducibility.md), and so one can verify that it can be rebuilt, resulting to the same digests.

## How to use this container?

The recommended way to use this container is via these flags. They require to defined a specific seccomp policy. Seccomp policies is a way to define which system calls are authorized inside the container.

Here is a podman command with the proper flags, and [the gvisor seccomp policy](tests/share/seccomp.gvisor.json).

```bash
podman run \
    --log-driver none \
    --security-opt no-new-privileges \
    --userns nomap \
    --security-opt seccomp=tests/share/seccomp.gvisor.json \
    --cap-drop all \
    --cap-add SYS_CHROOT \
    --security-opt label=type:container_engine_t \
    --network=none \
    -u dangerzone \
    --rm -i ghcr.io/freedomofpress/dangerzone/v1 \
    /usr/bin/python3 -m dangerzone.conversion.doc_to_pixels
```

### Output Format

The output of the container is streamed to `stdout` in a custom binary format:

1.  **Total Pages**: A 4-byte unsigned integer representing the total number of pages in the converted document.
2.  **For each page**:
    a.  **Page Width**: A 4-byte unsigned integer representing the width of the page in pixels.
    b.  **Page Height**: A 4-byte unsigned integer representing the height of the page in pixels.
    c.  **Pixel Data**: bytes of raw RGB pixel data
        - Length is `width` x `height` x 3 color channels

## dangerzone-insecure-conversion python package

> [!WARNING]
> Do not use this unless you are certain about what you are doing.
> Do not use this to convert documents that should be processed safely!

The python code that runs inside the container is packaged under the name "dangerzone-insecure-conversion". It's considered insecure because the intended way to run dangerzone is by using a hardened sandbox, which is provided by dangerzone.

With that being said, there are situations where it's useful to run this code on its own, for instance when adding new file formats.

### Running the tests

```bash
uv pip install -e .
uv run pytest

# Or, if you prefer to run the tests outside the sandbox:
uv run pytest --local

# It's also possible to run tests in parallel if you have multiple cores:
uv run --with pytest-xdist pytest -n 6
```

## Building and Reproducing the Image

To build, verify, reproduce, or release the Dangerzone container image, use the unified `image` tool:

```bash
uv run image <subcommand> [OPTIONS]
```

### Subcommands

#### `build`

Build a reproducible container image:

```bash
uv run image build [OPTIONS]
```

**Options:**
*   `--platform <PLATFORM>`: Specify the build platform (e.g., `linux/amd64`, `linux/arm64`). Defaults to the current platform.
*   `--runtime <RUNTIME>`: Specify the container runtime (`docker` or `podman`). Defaults to `podman`.
*   `--debian-archive-date <YYYYMMDD>`, `-d`: Use a specific Debian snapshot archive date for reproducibility.
*   `--tag <TAG>`: Provide a custom tag for the image (for development only).
*   `--output <PATH>`, `-o`: Path to store the container image (default: `container.tar`).
*   `--use-cache [yes|no]`: Use the builder's cache to speed up builds (default: yes).
*   `--dry`: Print commands without executing them.

**Example:**
```bash
uv run image build --platform linux/amd64 --debian-archive-date 20231026
```

#### `verify-attestation`

Verify the SLSA provenance attestation for a container image:

```bash
uv run image verify-attestation --image <FULL_IMAGE_REF> [OPTIONS]
```

**Options:**
*   `--image <REF>`: Full image reference (e.g., `ghcr.io/foo/bar@sha256:...`). **Required.**
*   `--repository <REPO>`: GitHub repository to verify against (default: `freedomofpress/dangerzone-image`).
*   `--workflow <PATH>`: GitHub Actions workflow path (default: `.github/workflows/release-container-image.yml`).

**Example:**
```bash
uv run image verify-attestation --image "ghcr.io/freedomofpress/dangerzone/v1@sha256:..."
```

#### `reproduce`

Reproduce a container image and verify its digest:

```bash
uv run image reproduce [OPTIONS] <DIGEST>
```

**Options:**
*   `--platform <PLATFORM>`: Specify the build platform. Defaults to the current platform.
*   `--runtime <RUNTIME>`: Specify the container runtime (`docker` or `podman`). Defaults to `podman`.
*   `--no-cache`: Do not use existing cached images for the container build.
*   `--debian-archive-date <DATE>`: Use a specific Debian snapshot archive date, or `autodetect` to retrieve it from the image annotation.
*   `--dry`: Print commands without executing them.

**Examples:**
```bash
# With explicit date
uv run image reproduce --debian-archive-date 20231026 <digest>

# Autodetect date from image annotation (requires full image name with digest)
uv run image reproduce --debian-archive-date autodetect ghcr.io/freedomofpress/dangerzone/v1@sha256:<digest>
```

#### `release`

Attest provenance, reproduce, and release a container image:

```bash
uv run image release --ghcr-signer-path <PATH> [OPTIONS]
```

**Options:**
*   `--ghcr-signer-path <PATH>`: Path to the ghcr-signer repository. **Required.**
*   `--commit <SHA>`: Full SHA1 commit to use. Defaults to the git HEAD of the current branch.
*   `--repository <REPO>`: GitHub repository to use (default: `freedomofpress/dangerzone-image`).
*   `--workflow <PATH>`: GitHub Actions workflow path.
*   `--image-name <NAME>`: Container image name (default: `ghcr.io/freedomofpress/dangerzone/v1`).
*   `--skip-reproduction-for [<DIGEST> ...]`: Digests to skip during reproduction.
*   `--skip-signing`: Skip generating signatures.
*   `--dry`: Print commands without executing them.

### Dependencies

The following tools are required for various subcommands:

| Tool | Required by | Notes |
| ---- | ----------- | ----- |
| `podman` or `docker` | `build`, `reproduce` | Container runtime for building images |
| `crane` | `reproduce` (autodetect), `release` | Installed via `uvx mazette install crane` |
| `cosign` | `verify-attestation`, `release` | Installed via `uvx mazette install cosign` |
| `git` | `build`, `release` | For determining git tags and cloning repos |

The `repro-build` Python library is included as a project dependency and installed automatically via `uv sync`.

Install `crane` and `cosign` with:

```bash
uvx mazette install
```
