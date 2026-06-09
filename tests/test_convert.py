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

TIMEOUT = 60

REFERENCE_DIR = Path(__file__).parent / "test_docs" / "reference"
_GZIP_MAGIC = b"\x1f\x8b"


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


async def read_and_validate_stdout(
    proc: asyncio.subprocess.Process,
    keep_data: bool = True,
) -> bytes:
    assert proc.stdout is not None
    sr = proc.stdout
    buf = io.BytesIO()

    async def read_exactly(size: int):
        _bytes = await sr.readexactly(size)
        if keep_data:
            buf.write(_bytes)
        return _bytes

    header = await read_exactly(INT_BYTES)
    page_count = int.from_bytes(header, "big", signed=False)
    if page_count >= MAX_PAGES:
        raise ValueError(f"Page count {page_count} exceeds maximum ({MAX_PAGES})")
    assert page_count > 0, "Expected at least one page"

    for _ in range(page_count):
        width_bytes = await read_exactly(INT_BYTES)
        width = int.from_bytes(width_bytes, "big", signed=False)
        assert 0 < width < 10000, "Page width must be positive and less than 10000"
        height_bytes = await read_exactly(INT_BYTES)
        height = int.from_bytes(height_bytes, "big", signed=False)
        assert 0 < height < 10000, "Page height must be positive and less than 10000"
        pixel_size = width * height * 3
        _ = await read_exactly(pixel_size)
    return buf.getvalue()


async def read_stderr(proc: asyncio.subprocess.Process) -> bytes:
    assert proc.stderr is not None
    return await proc.stderr.read()


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
    doc: Path, container_image: str, container_security_args: List[str],
    keep_output: bool = True,
    expect_fail: bool = False,
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

        async def _read_stdout_task():
            return await read_and_validate_stdout(proc, keep_data=keep_output)

        stdout_task = asyncio.create_task(_read_stdout_task())
        stderr_task = asyncio.create_task(read_stderr(proc))

        # NOTE: Use asyncio.gather here, so that any exception from the above
        # awaitables will cancel the whole group. This way, a parsing error while
        # reading stdout can cancel the `proc.wait()`, which would otherwise remain
        # blocked, even if we attempted to kill the process. We have seen at least
        # one case where the Podman process is killed but `conmon` remains blocked,
        # and therefore the `.wait()` method hangs.
        try:
            _, stdout, stderr = await asyncio.gather(
                proc.wait(), stdout_task, stderr_task
            )
        except asyncio.exceptions.IncompleteReadError:
            if expect_fail:
                stdout = b""
                stderr = await stderr_task
                _ = await proc.wait()
            else:
                raise

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

        assert "Converted document to pixels" in progress

        pages = parse_pixel_output(pixel_data)
        assert len(pages) > 0, "Expected at least one page"
        for width, height, rgb_data in pages:
            assert width > 0, "Page width must be positive"
            assert height > 0, "Page height must be positive"
            assert len(rgb_data) == width * height * 3, "RGB data length mismatch"
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
            assert pixel_data == read_reference_data(reference_bin), (
                f"Pixel data does not match reference for {doc.name}. "
                "Run with --update-pixel-references to regenerate."
            )


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
                    bad_doc, container_image, container_security_args, expect_fail=True
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
