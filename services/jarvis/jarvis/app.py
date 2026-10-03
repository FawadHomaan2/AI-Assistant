"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis.agents.orchestrator import Orchestrator
from jarvis.ai.gateway import Gateway
from jarvis.config.settings import Settings
from jarvis.db.engine import Database
from jarvis.db.repositories import AuditRepository, SessionRepository, TurnRepository
from jarvis.governance.estop import EmergencyStop
from jarvis.transport import routes
from jarvis.transport.auth import AuthMiddleware
from jarvis.util.errors import JarvisError
from jarvis.util.logging import get_logger

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
    version: str = VERSION


def build_context(settings: Settings, token: str, db_path: str | None = None) -> Context:
    db = Database(db_path)
    sessions = SessionRepository(db)
    turns = TurnRepository(db)
    audit = AuditRepository(db)
    gateway = Gateway(settings)
    estop = EmergencyStop()
    orchestrator = Orchestrator(gateway, sessions, turns, audit, estop)
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
