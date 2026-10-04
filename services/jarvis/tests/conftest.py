from __future__ import annotations

import pytest

from jarvis.app import build_context, create_app
from jarvis.config.settings import AISettings, ProviderSettings, Settings
from jarvis.util import logging as jlog

TOKEN = "test-token-abc"


@pytest.fixture(autouse=True)
def _quiet_logging(tmp_path, monkeypatch):
    """Keep tests off the real data directory and out of the real log file."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    from jarvis.config import paths

    paths.reset_cache()
    jlog.reset()
    jlog.configure(level="WARNING", to_file=False)
    yield
    paths.reset_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ai=AISettings(
            default="dev_echo",
            providers={"dev_echo": ProviderSettings(kind="dev_echo")},
        )
    )


@pytest.fixture
def ctx(settings):
    context = build_context(settings, TOKEN, ":memory:")
    yield context
    context.db.close()


@pytest.fixture
def app(ctx):
    return create_app(ctx)


@pytest.fixture
async def client(app):
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"authorization": f"Bearer {TOKEN}"},
    ) as c:
        yield c
