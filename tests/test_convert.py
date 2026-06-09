import asyncio
import gzip
import io
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple

import pytest

from dangerzone_insecure_converter import errors
from dangerzone_insecure_converter.common import INT_BYTES
from dangerzone_insecure_converter.doc_to_pixels import DocumentToPixels
from dangerzone_insecure_converter.errors import MAX_PAGES

from .conftest import TEST_DOCS_DIRECTORY, for_each_doc

TIMEOUT = 30
MAX_STREAM_SIZE = 2 * 1024 * 1024 * 1024  # 2 GiB

REFERENCE_DIR = Path(__file__).parent / "test_docs" / "reference"
DIFF_ARTIFACTS_DIR = Path(__file__).parent / "_diff_artifacts"
_GZIP_MAGIC = b"\x1f\x8b"


def dump_pixel_diff(doc_name: str, actual: bytes, reference: bytes) -> Path:
    """Write per-page PNGs for actual and reference output side-by-side.

    Returns the directory containing the artifacts. Also (re)writes
    `_diff_artifacts/diff.html`, an index of all artifact subdirectories
    that pairs actual and reference pages for visual inspection.
    """
    import fitz

    out_dir = DIFF_ARTIFACTS_DIR / doc_name
    out_dir.mkdir(parents=True, exist_ok=True)

    for label, data in (("actual", actual), ("reference", reference)):
        try:
            pages = parse_pixel_output(data)
        except Exception as e:
            (out_dir / f"{label}.parse-error.txt").write_text(repr(e))
            continue
        for i, (width, height, rgb) in enumerate(pages, start=1):
            pix = fitz.Pixmap(fitz.csRGB, width, height, rgb, 0)
            pix.save(str(out_dir / f"{label}-page-{i:03d}.png"))

    write_diff_index()
    return out_dir


def write_diff_index() -> None:
    """Generate `_diff_artifacts/diff.html` from whatever subdirs currently exist.

    Pairs each actual-page-NNN.png with its reference-page-NNN.png by filename.
    Idempotent — safe to call after every dump.
    """
    docs = []
    for sub in sorted(DIFF_ARTIFACTS_DIR.iterdir()):
        if not sub.is_dir():
            continue
        page_nums = sorted(
            int(p.stem.rsplit("-", 1)[1])
            for p in sub.glob("actual-page-*.png")
        )
        if page_nums:
            docs.append((sub.name, page_nums))

    rows = []
    for name, pages in docs:
        rows.append(f'<h2 id="{name}">{name}</h2>')
        for i in pages:
            p = f"{i:03d}"
            rows.append(
                '<div class="pair">'
                f'<figure><figcaption>actual page {i}</figcaption>'
                f'<img src="{name}/actual-page-{p}.png" loading="lazy"></figure>'
                f'<figure><figcaption>reference page {i}</figcaption>'
                f'<img src="{name}/reference-page-{p}.png" loading="lazy"></figure>'
                "</div>"
            )

    nav = " ".join(f'<a href="#{name}">{name}</a>' for name, _ in docs)
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Pixel diff</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 24px; background: #1a1a1a; color: #ddd; }}
  h1 {{ font-size: 18px; }}
  h2 {{ font-size: 16px; margin-top: 32px; border-bottom: 1px solid #444; padding-bottom: 6px; }}
  .pair {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }}
  .pair figure {{ margin: 0; }}
  .pair figcaption {{ font-size: 12px; color: #888; margin-bottom: 4px; }}
  .pair img {{ width: 100%; border: 1px solid #333; background: white; }}
  nav a {{ color: #6cf; margin-right: 12px; }}
</style></head><body>
<h1>Pixel diff: actual (left) vs reference (right)</h1>
<nav>{nav}</nav>
{''.join(rows)}
</body></html>
"""
    (DIFF_ARTIFACTS_DIR / "diff.html").write_text(html)


class CapturingDocumentToPixels(DocumentToPixels):
    """DocumentToPixels subclass that captures output to local buffers."""

    def __init__(self) -> None:
        super().__init__()
        self._pixel_output = io.BytesIO()
        self._progress_lines: List[str] = []

    async def write_page_count(self, count: int) -> None:
        self._pixel_output.write(count.to_bytes(INT_BYTES, "big", signed=False))

    async def write_page_width(self, width: int) -> None:
        self._pixel_output.write(width.to_bytes(INT_BYTES, "big", signed=False))

    async def write_page_height(self, height: int) -> None:
        self._pixel_output.write(height.to_bytes(INT_BYTES, "big", signed=False))

    async def write_page_data(self, data: bytes) -> None:
        self._pixel_output.write(bytes(data))

    def update_progress(self, text: str, *, error: bool = False) -> None:
        self._progress_lines.append(text)


def parse_pixel_output(data: bytes) -> List[Tuple[int, int, bytes]]:
    """Parse the binary pixel output into a list of (width, height, rgb_data) per page."""
    offset = 0
    page_count = int.from_bytes(data[offset : offset + INT_BYTES], "big")
    offset += INT_BYTES

    pages = []
    for _ in range(page_count):
        width = int.from_bytes(data[offset : offset + INT_BYTES], "big")
        offset += INT_BYTES
        height = int.from_bytes(data[offset : offset + INT_BYTES], "big")
        offset += INT_BYTES
        size = width * height * 3  # RGB
        rgb_data = data[offset : offset + size]
        offset += size
        pages.append((width, height, rgb_data))

    return pages


def read_reference_data(path: Path) -> bytes:
    data = path.read_bytes()
    if data.startswith(_GZIP_MAGIC):
        return gzip.decompress(data)
    return data


def write_reference_data(path: Path, data: bytes) -> None:
    path.write_bytes(gzip.compress(data))


async def read_bounded(sr: asyncio.StreamReader, limit: int) -> bytes:
    buf = b""
    while not sr.at_eof():
        read = await sr.read(limit)
        buf += read
        limit = limit - len(read)
        if limit <= 0:
            raise RuntimeError(f"Stream reached maximum size ({limit} bytes)")
    return buf


async def read_stdout_bounded(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stdout is not None
    header = await proc.stdout.read(INT_BYTES)
    if not header:
        # This may happen in case of a failed conversion.
        return b""
    page_count = int.from_bytes(header, "big", signed=False)
    if page_count >= MAX_PAGES:
        raise ValueError(f"Page count {page_count} exceeds maximum ({MAX_PAGES})")
    rest = await read_bounded(proc.stdout, MAX_STREAM_SIZE)
    return header + rest


async def read_stderr_bounded(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stderr is not None
    return await read_bounded(proc.stderr, MAX_STREAM_SIZE)


async def run_local_conversion(doc: Path) -> tuple[bytes, List[str]]:
    input_file = Path("/tmp/input_file")
    try:
        input_file.write_bytes(doc.read_bytes())

        converter = CapturingDocumentToPixels()
        await converter.convert()
        return converter._pixel_output.getvalue(), converter._progress_lines
    finally:
        if input_file.exists():
            input_file.unlink()


async def run_container_conversion(
    doc: Path, container_image: str, container_security_args: List[str]
) -> tuple[int, bytes, bytes]:
    cid_fd, cid_path = tempfile.mkstemp(prefix="dz-cid-")
    os.close(cid_fd)

    proc = await asyncio.subprocess.create_subprocess_exec(
        "podman",
        "run",
        *container_security_args,
        "--rm",
        "-i",
        "--cidfile",
        cid_path,
        container_image,
        "/usr/bin/python3",
        "-m",
        "dangerzone.conversion.doc_to_pixels",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setpgrp,
    )

    try:
        assert proc.stdin is not None
        proc.stdin.write(doc.read_bytes())
        await proc.stdin.drain()
        proc.stdin.close()

        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_task = asyncio.create_task(read_stdout_bounded(proc))
        stderr_task = asyncio.create_task(read_stderr_bounded(proc))

        # NOTE: Use asyncio.gather here, so that any exception from the above
        # awaitables will cancel the whole group. This way, a parsing error while
        # reading stdout can cancel the `proc.wait()`, which would otherwise remain
        # blocked, even if we attempted to kill the process. We have seen at least
        # one case where the Podman process is killed but `conmon` remains blocked,
        # and therefore the `.wait()` method hangs.
        _, stdout, stderr = await asyncio.gather(proc.wait(), stdout_task, stderr_task)

        assert proc.returncode is not None
        return proc.returncode, stdout, stderr
    finally:
        if proc.returncode is None:
            try:
                if os.path.exists(cid_path):
                    container_id = Path(cid_path).read_text().strip()
                    if container_id:
                        subprocess.run(
                            ["podman", "kill", container_id],
                            capture_output=True,
                            timeout=10,
                        )
            except Exception:
                pass
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.unlink(cid_path)
        except OSError:
            pass


@for_each_doc
@pytest.mark.asyncio
async def test_convert_document(request: pytest.FixtureRequest, doc: Path) -> None:
    """Test conversion to pixels for each valid document.

    By default, conversion tests run in a container; pass --local to run locally.
    Reference pixel data comparisons are only performed in container mode.
    """
    if request.config.getoption("--local"):
        try:
            pixel_data, progress = await asyncio.wait_for(
                run_local_conversion(doc), timeout=TIMEOUT
            )
        except TimeoutError:
            pytest.fail("timeout failed")

        # Check progress messages
        assert "Converted document to pixels" in progress
    else:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        try:
            returncode, pixel_data, stderr = await asyncio.wait_for(
                run_container_conversion(doc, container_image, container_security_args),
                timeout=TIMEOUT,
            )
        except TimeoutError:
            pytest.fail("timeout failed")
        assert returncode == 0, (
            f"Container conversion failed (exit {returncode}).\n"
            f"stderr: {stderr.decode(errors='replace')}"
        )

        reference_bin = REFERENCE_DIR / f"{doc.stem}.bin"
        if request.config.getoption("--update-pixel-references"):
            REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
            write_reference_data(reference_bin, pixel_data)
        elif reference_bin.exists():
            reference_data = read_reference_data(reference_bin)
            if pixel_data != reference_data:
                dump_pixel_diff(doc.name, pixel_data, reference_data)
                pytest.fail(
                    f"Pixel data does not match reference for {doc.name}. "
                    f"Open {DIFF_ARTIFACTS_DIR / 'diff.html'} in a browser "
                    "to compare actual vs reference. "
                    "Run with --update-pixel-references to regenerate."
                )

    # Parse and validate pixel data structure
    pages = parse_pixel_output(pixel_data)
    assert len(pages) > 0, "Expected at least one page"
    for width, height, rgb_data in pages:
        assert width > 0, "Page width must be positive"
        assert height > 0, "Page height must be positive"
        assert len(rgb_data) == width * height * 3, "RGB data length mismatch"


@pytest.mark.parametrize(
    "bad_doc, expected_error",
    [
        (TEST_DOCS_DIRECTORY / "sample_bad_pdf.pdf", errors.DocFormatUnsupported),
        pytest.param("pdf_11k_pages", errors.MaxPagesException),
    ],
    indirect=["bad_doc"],
)
@pytest.mark.asyncio
async def test_bad_pdf(
    request: pytest.FixtureRequest,
    bad_doc: Path,
    expected_error: type[errors.ConversionException],
) -> None:
    """Test that invalid documents raise the expected errors."""
    if request.config.getoption("--local"):
        try:
            with pytest.raises(expected_error):
                await asyncio.wait_for(run_local_conversion(bad_doc), timeout=TIMEOUT)
        except TimeoutError:
            pytest.fail("timeout failed")
    else:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        try:
            returncode, _stdout, stderr = await asyncio.wait_for(
                run_container_conversion(
                    bad_doc, container_image, container_security_args
                ),
                timeout=TIMEOUT,
            )
        except TimeoutError:
            pytest.fail("timeout failed")
        assert returncode == expected_error.error_code, (
            f"Container conversion failed with exit {returncode} "
            f"(expected {expected_error.error_code}).\n"
            f"stderr: {stderr.decode(errors='replace')}"
        )
