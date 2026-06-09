import asyncio
import os
import re
from pathlib import Path
from typing import List

import pytest

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

SAFE_EXTENSION = "-safe.pdf"
FORMATS_REGEX = (
    r".*\.(pdf|docx|doc|xlsx|xls|pptx|ppt|odt|ods|odp|odg|jpg|jpeg|gif|png)$"
)

TIMEOUT = 600  # 10 minutes per document


def get_test_docs(min_size: int, max_size: int) -> List[Path]:
    return sorted(
        [
            doc
            for doc in TEST_DOCS_DIR.rglob("*")
            if doc.is_file()
            and min_size < doc.stat().st_size < max_size
            and not doc.name.endswith(SAFE_EXTENSION)
            and re.match(FORMATS_REGEX, doc.name)
        ]
    )


docs_10K = get_test_docs(min_size=0, max_size=10 * 2**10)
docs_100K = get_test_docs(min_size=10 * 2**10, max_size=100 * 2**10)
docs_10M = get_test_docs(min_size=100 * 2**10, max_size=10 * 2**20)
docs_100M = get_test_docs(min_size=10 * 2**20, max_size=100 * 2**20)

for_each_10K_doc = pytest.mark.parametrize(
    "doc", docs_10K, ids=[str(doc.name) for doc in docs_10K]
)
for_each_100K_doc = pytest.mark.parametrize(
    "doc", docs_100K, ids=[str(doc.name) for doc in docs_100K]
)
for_each_10M_doc = pytest.mark.parametrize(
    "doc", docs_10M, ids=[str(doc.name) for doc in docs_10M]
)
for_each_100M_doc = pytest.mark.parametrize(
    "doc", docs_100M, ids=[str(doc.name) for doc in docs_100M]
)


class TestLargeSet:
    async def run_doc_test(
        self, doc: Path, container_image: str, container_security_args: List[str]
    ) -> None:
        try:
            returncode, _stdout, stderr = await asyncio.wait_for(
                run_container_conversion(doc, container_image, container_security_args),
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

    @for_each_10K_doc
    @pytest.mark.asyncio
    async def test_10K_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        await self.run_doc_test(doc, container_image, container_security_args)

    @for_each_100K_doc
    @pytest.mark.asyncio
    async def test_100K_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        await self.run_doc_test(doc, container_image, container_security_args)

    @for_each_10M_doc
    @pytest.mark.asyncio
    async def test_10M_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        await self.run_doc_test(doc, container_image, container_security_args)

    @for_each_100M_doc
    @pytest.mark.asyncio
    async def test_100M_docs(self, request: pytest.FixtureRequest, doc: Path) -> None:
        container_image = request.getfixturevalue("container_image")
        container_security_args = request.getfixturevalue("container_security_args")
        await self.run_doc_test(doc, container_image, container_security_args)
