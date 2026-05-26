import subprocess
import zipfile
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_IMAGE_SCRIPT = REPO_ROOT / "src" / "scripts" / "image.py"
IMAGE_ID_FILE = REPO_ROOT / "image-id.txt"

TESTS_DIRECTORY = Path(__file__).parent
SAFE_EXTENSION = "-safe.pdf"
TEST_DOCS_DIRECTORY = TESTS_DIRECTORY / "test_docs"

_DANGERZONE_SHARE_DIR = Path(__file__).parent / "share"


@pytest.fixture
def pdf_11k_pages(tmp_path: Path) -> str:
    filename = "sample-11k-pages.pdf"
    zip_path = TEST_DOCS_DIRECTORY / f"{filename}.zip"
    with zipfile.ZipFile(zip_path, "r") as zip_file:
        zip_file.extractall(tmp_path)
    return str(tmp_path / filename)


test_docs = [
    p
    for p in TEST_DOCS_DIRECTORY.glob("*")
    if p.is_file()
    and not (
        p.name.endswith(SAFE_EXTENSION)
        or p.name.startswith("sample_bad")
        or ".pdf.zip" in p.name
    )
]

for_each_doc = pytest.mark.parametrize(
    "doc", test_docs, ids=[str(doc.name) for doc in test_docs]
)


@pytest.fixture
def bad_doc(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    if request.param == "pdf_11k_pages":
        filename = "sample-11k-pages.pdf"
        zip_path = TEST_DOCS_DIRECTORY / f"{filename}.zip"
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            zip_file.extractall(tmp_path)
        return tmp_path / filename
    return Path(request.param)


def get_runtime_security_args() -> List[str]:
    result = subprocess.run(
        ["podman", "version", "-f", "{{.Client.Version}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    major, minor, *_ = result.stdout.strip().split(".")
    runtime_version = (int(major), int(minor))

    if runtime_version < (4, 0):
        seccomp_path = _DANGERZONE_SHARE_DIR / "seccomp.gvisor.permissive.json"
    else:
        seccomp_path = _DANGERZONE_SHARE_DIR / "seccomp.gvisor.json"

    security_args = ["--log-driver", "none"]
    security_args += ["--security-opt", "no-new-privileges"]
    if runtime_version >= (4, 1):
        security_args += ["--userns", "nomap"]
    security_args += ["--security-opt", f"seccomp={seccomp_path}"]
    security_args += ["--cap-drop", "all"]
    security_args += ["--cap-add", "SYS_CHROOT"]
    security_args += ["--security-opt", "label=type:container_engine_t"]
    security_args += ["--network=none"]
    security_args += ["-u", "dangerzone"]

    return security_args


def determine_container_image(config: pytest.Config) -> str:
    image = config.getoption("--container-image")
    if not image:
        image_id_txt = _DANGERZONE_SHARE_DIR / "image-id.txt"
        if image_id_txt.exists():
            image = image_id_txt.read_text().strip()
    if not image:
        raise pytest.UsageError(
            "No container image available. Provide --container-image, run with "
            "--build, or use --local."
        )
    return image


@pytest.fixture
def container_image(request: pytest.FixtureRequest) -> str:
    return determine_container_image(request.config)


@pytest.fixture
def container_security_args() -> List[str]:
    try:
        return get_runtime_security_args()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        pytest.skip(f"Could not determine container security args: {e}")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-pixel-references",
        action="store_true",
        default=False,
        help="Regenerate reference pixel data (.bin files, gzip-compressed) using container conversion",
    )
    parser.addoption(
        "--container-image",
        default=None,
        help="Container image to use for container conversion tests",
    )
    parser.addoption(
        "--local",
        action="store_true",
        default=False,
        help="Run conversion tests locally instead of in a container",
    )
    parser.addoption(
        "--build",
        action="store_true",
        default=False,
        help="Build the container image via image.py before running tests.",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--local") and config.getoption("--update-pixel-references"):
        raise pytest.UsageError(
            "--update-pixel-references must run in a container; do not combine with --local."
        )
    if config.getoption("--build") and config.getoption("--local"):
        raise pytest.UsageError("--build is meaningless with --local (no container is used).")
    if config.getoption("--build") and config.getoption("--container-image"):
        raise pytest.UsageError("--build and --container-image are mutually exclusive.")
    if not config.getoption("--local"):
        determine_container_image(config)


def run_container_conversion(
    doc: Path,
    container_image: str,
    container_security_args: List[str],
    timeout: int = 5 * 60,
) -> subprocess.CompletedProcess:
    in_bytes = doc.read_bytes()
    return subprocess.run(
        [
            "podman",
            "run",
            "--rm",
            "-i",
            *container_security_args,
            container_image,
            "/usr/bin/python3",
            "-m",
            "dangerzone.conversion.doc_to_pixels",
        ],
        input=in_bytes,
        capture_output=True,
        timeout=timeout,
    )
