"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis.agents.executor import Executor
from jarvis.agents.orchestrator import Orchestrator
from jarvis.ai.gateway import Gateway
from jarvis.browser.session import BrowserSession, BrowserSettings
from jarvis.config import paths
from jarvis.config.settings import Settings
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository, SessionRepository, TurnRepository
from jarvis.governance.consent import ConsentBroker
from jarvis.governance.estop import EmergencyStop
from jarvis.governance.pathjail import PathJail
from jarvis.governance.persistence import GrantStore
from jarvis.governance.policy import Policy
from jarvis.memory.embeddings import best_available
from jarvis.memory.store import MemoryStore
from jarvis.platform_ import backends as os_backends
from jarvis.plugins.host import PluginHost
from jarvis.security.center import SecurityCenter
from jarvis.tools.applications import ApplicationTool
from jarvis.tools.browser import BrowserTool
from jarvis.tools.capture import ClipboardTool, NotificationTool, ScreenshotTool
from jarvis.tools.diagnostics_tool import DiagnosticsTool
from jarvis.tools.documents import DocumentTool
from jarvis.tools.filesystem import FileSystemTool
from jarvis.tools.network import NetworkTool
from jarvis.tools.plugin_proxy import register_plugins
from jarvis.tools.powershell import PowerShellTool
from jarvis.tools.processes import ProcessTool
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.security_tool import SecurityTool
from jarvis.tools.systeminfo import SystemInfoTool
from jarvis.tools.websearch import WebSearchTool
from jarvis.tools.windows_tool import WindowTool
from jarvis.transport import routes
from jarvis.transport.auth import AuthMiddleware
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger
from jarvis.voice.pipeline import VoicePipeline, VoiceSettings
from jarvis.voice.stt import WhisperSpeechToText
from jarvis.voice.tts import PiperTextToSpeech
from jarvis.voice.wake import OpenWakeWordDetector

log = get_logger(__name__)

VERSION = "0.2.0"


@dataclass
class Context:
    """Everything a request handler needs. Built once at startup."""

    settings: Settings
    db: Database
    sessions: SessionRepository
    turns: TurnRepository
    audit: AuditRepository
    gateway: Gateway
    orchestrator: Orchestrator
    estop: EmergencyStop
    token: str
    jail: PathJail
    policy: Policy
    grants: GrantStore
    consent: ConsentBroker
    registry: ToolRegistry
    executor: Executor
    voice: VoicePipeline
    browser: BrowserSession
    memory: MemoryStore
    security: SecurityCenter
    plugins: PluginHost
    version: str = VERSION


def build_context(settings: Settings, token: str, db_path: str | None = None) -> Context:
    db = Database(db_path)
    sessions = SessionRepository(db)
    turns = TurnRepository(db)
    audit = AuditRepository(db)
    gateway = Gateway(settings)
    estop = EmergencyStop()

    # Governance plane. The jail's allowed roots default to the user's own
    # document folders; the policy engine gates every tool call against them.
    jail = PathJail()
    # Permissions are loaded from disk, not rebuilt from defaults: a revocation
    # that lasts until the next launch was never a revocation.
    grants = GrantStore(db)
    stored_grants = grants.load()
    stored_grants.purge_expired()
    stored_mode, stored_read_only = grants.load_posture()
    policy = Policy(stored_grants, mode=stored_mode, read_only=stored_read_only)
    consent = ConsentBroker()

    # One browser for the process. Started lazily on first use, so an install
    # without Chromium costs nothing until something actually browses.
    browser = BrowserSession(
        BrowserSettings(
            headless=settings.browser.headless,
            allowed_hosts=set(settings.browser.allowed_hosts),
            allow_any_host=settings.browser.allow_any_host,
            allow_loopback=settings.browser.allow_loopback,
            executable_path=settings.browser.executable_path,
            timeout_ms=int(settings.browser.timeout_seconds * 1000),
        )
    )

    # Read-only by construction: the center has no code path that changes a
    # security setting, on any platform.
    security = SecurityCenter(db)

    adapters = os_backends()
    registry = ToolRegistry()
    registry.register(FileSystemTool(jail))
    registry.register(DocumentTool(jail))
    registry.register(ProcessTool(adapters.processes))
    registry.register(ApplicationTool(adapters.apps, adapters.processes))
    registry.register(WindowTool(adapters.windows))
    registry.register(SystemInfoTool())
    registry.register(NetworkTool())
    registry.register(DiagnosticsTool(adapters.processes))
    registry.register(ScreenshotTool(jail))
    registry.register(ClipboardTool())
    registry.register(NotificationTool())
    registry.register(PowerShellTool())
    registry.register(SecurityTool(security))
    if settings.browser.enabled:
        registry.register(BrowserTool(browser))
        registry.register(WebSearchTool(browser, settings.browser.search_engine))

    # Plugins are discovered, listed, and loaded only if already enabled.
    # Discovery is not activation: installing something must never be the same
    # act as running it.
    plugins = PluginHost(db, paths.plugins_dir())
    plugins.discover()
    if loaded := register_plugins(registry, plugins, policy.grants):
        log.info("plugin tools registered", count=loaded)

    executor = Executor(registry, policy, consent, audit, estop)

    # The strongest embedder this installation can actually run. Word matching
    # when the semantic model is not downloaded, and the dashboard says which.
    memory = MemoryStore(db, best_available())

    # The voice pipeline is constructed either way; it reports what it is
    # missing rather than being absent, so the interface can offer the download.
    voice = VoicePipeline(
        WhisperSpeechToText(),
        PiperTextToSpeech(),
        OpenWakeWordDetector(settings.voice.wake_word),
        settings=VoiceSettings(
            enabled=settings.voice.enabled,
            push_to_talk=settings.voice.push_to_talk,
        ),
    )
    orchestrator = Orchestrator(gateway, sessions, turns, audit, estop, executor, memory)

    return Context(
        settings=settings,
        db=db,
        sessions=sessions,
        turns=turns,
        audit=audit,
        gateway=gateway,
        orchestrator=orchestrator,
        estop=estop,
        token=token,
        jail=jail,
        policy=policy,
        grants=grants,
        consent=consent,
        registry=registry,
        executor=executor,
        voice=voice,
        browser=browser,
        memory=memory,
        security=security,
        plugins=plugins,
    )


def create_app(ctx: Context) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        log.info(
            "core ready",
            version=ctx.version,
            schema=ctx.db.version,
            provider=ctx.settings.ai.default,
        )
        yield
        await ctx.gateway.aclose()
        # Chromium is a child process; leaving it running would outlive the core.
        await ctx.browser.close()
        # Plugin processes are children; leaving them would outlive the core.
        await ctx.plugins.stop_all()
        ctx.db.close()
        log.info("core stopped")

    app = FastAPI(
        title="Jarvis Core",
        version=VERSION,
        lifespan=lifespan,
        # No CORS middleware: the only legitimate callers are the desktop shell
        # and the dev server, both handled by the auth middleware's origin check.
    )
    app.state.ctx = ctx
    app.add_middleware(AuthMiddleware, token=ctx.token)

    @app.exception_handler(JarvisError)
    async def _jarvis_error(_request: Request, exc: JarvisError) -> JSONResponse:
        """Typed errors become structured responses the UI can explain."""
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    app.include_router(routes.router)
    return app
