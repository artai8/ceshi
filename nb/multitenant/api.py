"""FastAPI control plane and Telegram Mini App host."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError

from .models import (
    AccountStatus,
    AccountView,
    ForwardTask,
    ForwardTaskInput,
    MiniAppAuthRequest,
    SessionResponse,
    TaskUpdate,
    TenantView,
)
from .repository import MongoRepository, RevisionConflict
from .security import expiry, validate_miniapp_init_data
from .settings import Settings


COOKIE_NAME = "nb_session"
STATIC_DIR = Path(__file__).with_name("static")
MAX_WATERMARK_BYTES = 5 * 1024 * 1024
MAX_WATERMARK_DIMENSION = 4096
MAX_WATERMARK_PIXELS = 16_000_000


def create_app(
    settings: Optional[Settings] = None,
    repository: Optional[MongoRepository] = None,
) -> FastAPI:
    cfg = settings or Settings.for_api()
    repo = repository or MongoRepository(cfg.mongo_uri, cfg.mongo_db)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        repo.ensure_indexes()
        repo.ping()
        yield

    app = FastAPI(title="nb multi-tenant control plane", version="3.0.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.repository = repo
    app.mount("/miniapp/static", StaticFiles(directory=STATIC_DIR), name="miniapp-static")

    def current_tenant(
        request: Request,
        authorization: Annotated[Optional[str], Header()] = None,
    ) -> int:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1].strip()
        if not token:
            token = request.cookies.get(COOKIE_NAME, "")
        request.state.session_token = token
        tenant_id = repo.resolve_session(token)
        if tenant_id is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return tenant_id

    def require_admin(tenant_id: int = Depends(current_tenant)) -> int:
        if tenant_id not in cfg.admin_ids:
            raise HTTPException(status_code=403, detail="Administrator required")
        return tenant_id

    @app.get("/health")
    def health() -> dict[str, str]:
        repo.ping()
        return {"status": "ok"}

    @app.get("/miniapp", include_in_schema=False)
    def miniapp() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/auth/miniapp", response_model=SessionResponse)
    def miniapp_auth(payload: MiniAppAuthRequest, request: Request) -> SessionResponse:
        try:
            user = validate_miniapp_init_data(payload.init_data, cfg.bot_token)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        client_ip = request.client.host if request.client else "unknown"
        if not repo.consume_rate_limit(
            "miniapp-tenant",
            str(user.id),
            limit=10,
            window_seconds=60,
            tenant_id=user.id,
        ) or not repo.consume_rate_limit(
            "miniapp-ip",
            client_ip,
            limit=60,
            window_seconds=60,
        ):
            raise HTTPException(status_code=429, detail="Too many login attempts")
        display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
        repo.upsert_tenant(user.id, username=user.username, display_name=display_name)
        token, expires_at = repo.create_session(
            user.id, kind="miniapp", expires_at=expiry(hours=1)
        )
        return SessionResponse(token=token, expires_at=expires_at)

    @app.get("/auth/magic/{token}", include_in_schema=False)
    def magic_login(token: str) -> RedirectResponse:
        result = repo.consume_magic_link(token, session_hours=cfg.web_session_hours)
        if result is None:
            raise HTTPException(status_code=401, detail="Link is invalid or expired")
        session_token, _ = result
        response = RedirectResponse(url=f"{cfg.public_base_url}/dashboard", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            session_token,
            max_age=cfg.web_session_hours * 3600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/api/logout", status_code=204)
    def browser_logout(
        request: Request,
        _: int = Depends(current_tenant),
    ) -> Response:
        repo.revoke_session(getattr(request.state, "session_token", ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/api/me", response_model=TenantView)
    def me(tenant_id: int = Depends(current_tenant)) -> TenantView:
        account = repo.get_account(tenant_id)
        return TenantView(
            tenant_id=tenant_id,
            is_admin=tenant_id in cfg.admin_ids,
            account_status=account.get("status") if account else None,
        )

    @app.get("/api/account", response_model=AccountView)
    def account(tenant_id: int = Depends(current_tenant)) -> AccountView:
        doc = repo.get_account(tenant_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="No Telegram account is bound")
        return AccountView.model_validate(doc)

    @app.delete("/api/account", status_code=202)
    def delete_account(tenant_id: int = Depends(current_tenant)) -> dict[str, str]:
        if not repo.request_account_delete(tenant_id):
            raise HTTPException(status_code=404, detail="No Telegram account is bound")
        return {"status": "deleting"}

    @app.get("/api/tasks", response_model=list[ForwardTask])
    def list_tasks(tenant_id: int = Depends(current_tenant)) -> list[ForwardTask]:
        return repo.list_tasks(tenant_id)

    @app.post("/api/tasks", response_model=ForwardTask, status_code=201)
    def create_task(
        payload: ForwardTaskInput,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        account = repo.get_account(tenant_id)
        if account is None or account.get("status") != AccountStatus.ACTIVE.value:
            raise HTTPException(status_code=409, detail="Bind a Telegram account first")
        return repo.create_task(tenant_id, payload)

    @app.get("/api/tasks/{task_id}", response_model=ForwardTask)
    def get_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.put("/api/tasks/{task_id}", response_model=ForwardTask)
    def update_task(
        task_id: str,
        payload: TaskUpdate,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        try:
            return repo.update_task(tenant_id, task_id, payload.revision, payload.task)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail="Task was changed in another session") from exc

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> Response:
        if not repo.delete_task(tenant_id, task_id):
            raise HTTPException(status_code=404, detail="Task not found")
        return Response(status_code=204)

    def _task_action(
        task_id: str,
        action: str,
        tenant_id: int,
    ) -> ForwardTask:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        account = repo.get_account(tenant_id)
        if action != "pause" and (
            account is None or account.get("status") != AccountStatus.ACTIVE.value
        ):
            raise HTTPException(status_code=409, detail="Telegram account is not active")
        if action != "pause" and (not task.sources or not task.destinations):
            raise HTTPException(status_code=422, detail="At least one source and destination are required")
        return repo.set_task_action(tenant_id, task_id, action)

    @app.post("/api/tasks/{task_id}/validate", response_model=ForwardTask)
    def validate_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "validate", tenant_id)

    @app.post("/api/tasks/{task_id}/run", response_model=ForwardTask)
    def run_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "run", tenant_id)

    @app.post("/api/tasks/{task_id}/pause", response_model=ForwardTask)
    def pause_task(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        return _task_action(task_id, "pause", tenant_id)

    @app.get("/api/tasks/{task_id}/status", response_model=ForwardTask)
    def task_status(
        task_id: str,
        tenant_id: int = Depends(current_tenant),
    ) -> ForwardTask:
        task = repo.get_task(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task

    @app.post("/api/tasks/{task_id}/watermark")
    async def upload_watermark(
        task_id: str,
        file: UploadFile = File(),
        tenant_id: int = Depends(current_tenant),
    ) -> dict[str, str]:
        if repo.get_task(tenant_id, task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")
        data = await file.read(MAX_WATERMARK_BYTES + 1)
        if len(data) > MAX_WATERMARK_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(status_code=422, detail="Watermark must be a PNG up to 5 MB")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise UnidentifiedImageError("not PNG")
                width, height = image.size
                if (
                    width > MAX_WATERMARK_DIMENSION
                    or height > MAX_WATERMARK_DIMENSION
                    or width * height > MAX_WATERMARK_PIXELS
                ):
                    raise UnidentifiedImageError("PNG dimensions are too large")
                image.verify()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            raise HTTPException(status_code=422, detail="Watermark PNG is invalid") from exc
        try:
            asset_id = repo.put_watermark(tenant_id, task_id, data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Task not found") from exc
        return {"asset_id": asset_id}

    @app.get("/api/admin/tenants")
    def admin_tenants(_: int = Depends(require_admin)) -> list[dict]:
        result = []
        for tenant in repo.tenants.find({}, {"_id": 1, "username": 1, "status": 1, "updated_at": 1}):
            tenant["tenant_id"] = tenant.pop("_id")
            result.append(tenant)
        return result

    @app.get("/api/admin/workers")
    def admin_workers(_: int = Depends(require_admin)) -> list[dict]:
        return list(repo.leases.find({}, {"_id": 0}))

    @app.get("/api/admin/errors")
    def admin_errors(_: int = Depends(require_admin)) -> list[dict]:
        return repo.list_system_errors()

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        "nb.multitenant.api:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(__import__("os").getenv("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
