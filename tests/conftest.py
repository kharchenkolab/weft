import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def pixi_bin() -> str:
    p = REPO_ROOT / ".env" / "bin" / "pixi"
    if p.exists():
        return str(p)
    found = shutil.which("pixi")
    if not found:
        pytest.skip("pixi binary not available")
    return found


@pytest.fixture(scope="session")
def docker_available() -> bool:
    if os.system("docker info >/dev/null 2>&1") != 0:
        pytest.skip("docker not available")
    return True
