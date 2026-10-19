import importlib
import os
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"

# The module validates its configuration while it is being imported, so the values have to
# be in place before importlib touches app/bridge.py.
REQUIRED_ENV = {
    "TG_API_ID": "12345",
    "TG_API_HASH": "0123456789abcdef0123456789abcdef",
    "MATRIX_HOMESERVER": "https://matrix.test.invalid",
    "MATRIX_ACCESS_TOKEN": "test-access-token",
    "TG_CHANNELS": "testchannel",
    "MATRIX_ROOM_IDS": "!testroom:matrix.test.invalid",
}

# Module globals a test is allowed to mutate; restored after every test.
MUTABLE_GLOBALS = (
    "ADMIN_MATRIX_USER_ID",
    "ALLOW_NON_ADMIN_STOP",
    "LLAMAGUARD_API_URL",
    "LLAMAGUARD_CHECKS",
    "LLAMAGUARD_REQUIRE_CHECKS",
    "SHM_WAIT_TIMEOUT_SECONDS",
    "TOPIC_NAMES_CACHE_SIZE",
    "ENABLE_IMAGES",
    "ENABLE_VIDEOS",
)


@pytest.fixture(scope="session")
def bridge_module(tmp_path_factory):
    """Imports app/bridge.py once per session.

    Importing the module constructs TelegramClient('session/tgmabr'), and Telethon's SQLite
    session needs that directory to exist relative to the current working directory. A
    temporary directory keeps the session file out of the repository.
    """
    workdir = tmp_path_factory.mktemp("bridge-session")
    (workdir / "session").mkdir()

    previous_cwd = os.getcwd()
    os.chdir(workdir)
    os.environ.update(REQUIRED_ENV)
    sys.path.insert(0, str(APP_DIR))
    try:
        module = importlib.import_module("bridge")
    finally:
        sys.path.remove(str(APP_DIR))
        os.chdir(previous_cwd)
    return module


@pytest.fixture
def bridge(bridge_module, monkeypatch):
    """Per-test state: fresh caches/counters and unpatched configuration afterwards."""
    bridge_module.PROCESSED_ALBUMS.clear()
    bridge_module.TOPIC_NAMES_CACHE.clear()
    bridge_module.SAFETY_STATS.update({"blocked": 0, "unavailable": 0})
    bridge_module.DELIVERY_STATS.update({"failed": 0, "partial": 0})
    for name in MUTABLE_GLOBALS:
        monkeypatch.setattr(bridge_module, name, getattr(bridge_module, name))
    return bridge_module
