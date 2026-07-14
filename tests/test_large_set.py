import asyncio
import os
import re
from pathlib import Path

import pytest

from .conftest import SAFE_EXTENSION
from .test_convert import run_container_conversion

if not os.environ.get("DZ_RUN_LARGE_TESTS"):
    pytest.skip(
        "Skipping large tests; set DZ_RUN_LARGE_TESTS=1 to run them",
        allow_module_level=True,
    )

LARGE_TEST_REPO_DIR = Path(
    os.environ.get(
        "LARGE_TEST_REPO_DIR",
        str(Path(__file__).parent / "test_docs_large"),
    )
)
TEST_DOCS_DIR = LARGE_TEST_REPO_DIR / "all_documents"

FORMATS_REGEX = (
    r".*\.(pdf|docx|doc|xlsx|xls|pptx|ppt|odt|ods|odp|odg|jpg|jpeg|gif|png)$"
)

TIMEOUT = 90

_SIZE_BUCKETS = [
    ("10K", 0, 10 * 2**10),
    ("100K", 10 * 2**10, 100 * 2**10),
    ("10M", 100 * 2**10, 10 * 2**20),
    ("100M", 10 * 2**20, 100 * 2**20),
]


def get_test_docs(min_size: int, max_size: int) -> list[Path]:
    return sorted(
        doc
        for doc in TEST_DOCS_DIR.rglob("*")
        if doc.is_file()
        and min_size < doc.stat().st_size < max_size
        and not doc.name.endswith(SAFE_EXTENSION)
        and re.match(FORMATS_REGEX, doc.name)
    )


def _mk_param(name: str, lo: int, hi: int):
    docs = get_test_docs(lo, hi)
    return pytest.mark.parametrize(
        "doc", docs, ids=[str(d.name) for d in docs]
    )


class TestLargeSet:
    async def run_doc_test(
        self, doc: Path, container_image: str, container_security_args: list[str]
    ) -> None:
        try:
            returncode, _stdout, stderr = await asyncio.wait_for(
                run_container_conversion(
                    doc, container_image, container_security_args, keep_output=False
                ),
                timeout=TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"*** TIMEOUT EXCEEDED FOR DOCUMENT '{doc}' ***")
            raise
        stderr_str = stderr.decode(errors="replace")
        if stderr_str:
            print(stderr_str, end="")
        assert returncode == 0, (
            f"Failed to convert {doc} (exit {returncode}).\nstderr: {stderr_str}"
        )

    @_mk_param(*_SIZE_BUCKETS[0])
    @pytest.mark.asyncio
    async def test_10K_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        await self.run_doc_test(
            doc,
            request.getfixturevalue("container_image"),
            request.getfixturevalue("container_security_args"),
        )

    @_mk_param(*_SIZE_BUCKETS[1])
    @pytest.mark.asyncio
    async def test_100K_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        await self.run_doc_test(
            doc,
            request.getfixturevalue("container_image"),
            request.getfixturevalue("container_security_args"),
        )

    @_mk_param(*_SIZE_BUCKETS[2])
    @pytest.mark.asyncio
    async def test_10M_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        await self.run_doc_test(
            doc,
            request.getfixturevalue("container_image"),
            request.getfixturevalue("container_security_args"),
        )

    @_mk_param(*_SIZE_BUCKETS[3])
    @pytest.mark.asyncio
    async def test_100M_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        await self.run_doc_test(
            doc,
            request.getfixturevalue("container_image"),
            request.getfixturevalue("container_security_args"),
        )
