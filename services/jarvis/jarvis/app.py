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
from jarvis.config.settings import Settings
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository, SessionRepository, TurnRepository
from jarvis.governance.consent import ConsentBroker
from jarvis.governance.estop import EmergencyStop
from jarvis.governance.pathjail import PathJail
from jarvis.governance.policy import Policy
from jarvis.governance.scopes import ScopeGrants
from jarvis.platform_ import backends as os_backends
from jarvis.tools.applications import ApplicationTool
from jarvis.tools.browser import BrowserTool
from jarvis.tools.capture import ClipboardTool, NotificationTool, ScreenshotTool
from jarvis.tools.diagnostics_tool import DiagnosticsTool
from jarvis.tools.documents import DocumentTool
from jarvis.tools.filesystem import FileSystemTool
from jarvis.tools.network import NetworkTool
from jarvis.tools.powershell import PowerShellTool
from jarvis.tools.processes import ProcessTool
from jarvis.tools.registry import ToolRegistry
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
    consent: ConsentBroker
    registry: ToolRegistry
    executor: Executor
    voice: VoicePipeline
    browser: BrowserSession
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
    policy = Policy(ScopeGrants(), mode=settings.mode)
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
    if settings.browser.enabled:
        registry.register(BrowserTool(browser))
        registry.register(WebSearchTool(browser, settings.browser.search_engine))

    executor = Executor(registry, policy, consent, audit, estop)

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
    orchestrator = Orchestrator(gateway, sessions, turns, audit, estop, executor)

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
        consent=consent,
        registry=registry,
        executor=executor,
        voice=voice,
        browser=browser,
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
