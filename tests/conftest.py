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


class ModuleFacade:
    """Presents the split app modules as one namespace.

    Reads and writes are routed to the module that actually owns the name, so patching
    ``bridge.SHM_WAIT_TIMEOUT_SECONDS`` still reaches ``config`` where the value is read at
    call time. Without the write-through, ``monkeypatch`` would only shadow the attribute on
    the facade.
    """

    def __init__(self, modules):
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name):
        for module in object.__getattribute__(self, "_modules"):
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(f"{name} is not defined by any app module")

    def __setattr__(self, name, value):
        for module in object.__getattribute__(self, "_modules"):
            if hasattr(module, name):
                setattr(module, name, value)
                return
        raise AttributeError(f"{name} is not defined by any app module")


MODULE_NAMES = ("config", "state", "media", "safety", "matrix_io", "commands", "handler")


@pytest.fixture(scope="session")
def bridge_module(tmp_path_factory):
    """Imports the app package once per session and returns it behind a facade.

    Importing the modules constructs TelegramClient('session/tgmabr'), and Telethon's SQLite
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
        importlib.import_module("bridge")
        # Only bridge may import the others: if it stops pulling in (say) handler,
        # the Telegram listener would never register and this must fail loudly.
        missing = [name for name in MODULE_NAMES if name not in sys.modules]
        assert not missing, f"importing bridge did not load: {missing}"
        modules = [sys.modules[name] for name in MODULE_NAMES]
    finally:
        sys.path.remove(str(APP_DIR))
        os.chdir(previous_cwd)

    facade = ModuleFacade(modules)
    object.__setattr__(facade, "session_file", workdir / "session" / "tgmabr.session")
    return facade


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
