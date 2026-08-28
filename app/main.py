from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.db import PROJECT_ROOT, init_db

DEFAULT_DIST_PATH = PROJECT_ROOT / "web" / "dist"


def _error_json(status: int, *, error: str, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "code": code, "detail": detail},
        status_code=status,
    )


def create_app(
    *,
    db_path: Path | None = None,
    dist_path: Path | None = None,
) -> FastAPI:
    resolved_dist = dist_path if dist_path is not None else DEFAULT_DIST_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = init_db(db_path)
        app.state.engine = engine
        yield
        engine.dispose()

    app = FastAPI(title="本机工作台", lifespan=lifespan)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "lock": None}

    def _spa_index() -> Path | None:
        index = resolved_dist / "index.html"
        if resolved_dist.is_dir() and index.is_file():
            return index
        return None

    @app.get("/{full_path:path}")
    def serve_workbench(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="未知接口",
            )

        index = _spa_index()
        if index is None:
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="前端尚未构建，请使用 Vite 开发服务或先生成 web/dist",
            )

        try:
            if full_path:
                candidate = (resolved_dist / full_path).resolve()
                try:
                    candidate.relative_to(resolved_dist.resolve())
                except ValueError:
                    return FileResponse(index)
                if candidate.is_file():
                    return FileResponse(candidate)
                if Path(full_path).suffix:
                    return _error_json(
                        404,
                        error="Not Found",
                        code="NOT_FOUND",
                        detail="静态资源不存在",
                    )
            return FileResponse(index)
        except OSError:
            return _error_json(
                404,
                error="Not Found",
                code="NOT_FOUND",
                detail="无法读取静态资源",
            )

    return app


app = create_app()
