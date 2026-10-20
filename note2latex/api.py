import io
import secrets
import zipfile
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .engine import Engine
from .ingest import MAX_BYTES, MAX_PAGES, ingest


class Edit(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    target: str = Field(pattern=r"^p\d{3}-b\d{3}$")
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")


def create_app(settings=None, provider=None):
    settings = settings or Settings.load()
    engine = Engine(settings, provider)

    @asynccontextmanager
    async def lifespan(app):
        engine.store.recover()
        yield
        await run_in_threadpool(engine.close)

    def authorize(request: Request):
        if settings.service_token and not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + settings.service_token
        ):
            raise HTTPException(401, "Bearer token required")

    app = FastAPI(title="Note2LaTeX Agent", lifespan=lifespan,
                  dependencies=[Depends(authorize)])
    app.state.engine = engine

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Job not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/health")
    def health():
        try:
            settings.require_provider()
            configured = True
        except ValueError:
            configured = False
        return {"status": "ok", "provider_configured": configured,
                "pdf_enabled": settings.compile_pdf}

    @app.post("/api/jobs", status_code=202)
    async def upload(
        files: list[UploadFile] = File(...),
        title: str = Form("Math notes", min_length=1, max_length=200),
        template: Literal["notes", "article", "homework"] = Form("notes"),
    ):
        try:
            settings.require_provider()
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
        if not 1 <= len(files) <= MAX_PAGES:
            raise HTTPException(400, "Upload 1 to 12 files")
        pages, total = [], 0
        try:
            for file in files:
                data = bytearray()
                while chunk := await file.read(1024 * 1024):
                    data.extend(chunk)
                    total += len(chunk)
                    if len(data) > MAX_BYTES or total > 50 * 1024 * 1024:
                        raise HTTPException(413, "Upload exceeds 25 MB/file or 50 MB/job")
                pages.extend(await run_in_threadpool(ingest, file.filename or "", bytes(data)))
                if len(pages) > MAX_PAGES:
                    raise HTTPException(400, "Maximum 12 pages per job")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        finally:
            for file in files:
                await file.close()
        meta = engine.store.create(title, template, pages)
        engine.submit(meta["id"])
        return meta

    @app.get("/api/jobs")
    def jobs():
        return engine.store.list()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return engine.store.get(job_id)

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str):
        return engine.store.events(job_id)

    @app.get("/api/jobs/{job_id}/document")
    def document(job_id: str):
        return engine.document(job_id)

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    def cancel(job_id: str):
        return engine.cancel(job_id)

    @app.post("/api/jobs/{job_id}/resume", status_code=202)
    def resume(job_id: str):
        settings.require_provider()
        engine.resume(job_id)
        return {"accepted": True}

    @app.post("/api/jobs/{job_id}/refine", status_code=202)
    def refine(job_id: str, edit: Edit):
        settings.require_provider()
        engine.refine(job_id, edit.message, edit.target, edit.revision)
        return {"accepted": True}

    @app.get("/api/jobs/{job_id}/download/{filename}")
    def download(job_id: str, filename: str):
        if filename not in {"note.md", "note.html", "note.tex", "note.pdf", "bundle.zip"}:
            raise HTTPException(404, "Unknown export")
        meta = engine.store.get(job_id)
        if not meta["revision"]:
            raise HTTPException(409, "No published revision")
        root = engine.store.path(job_id) / "versions" / meta["revision"]
        if filename == "bundle.zip":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in root.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
            return Response(buffer.getvalue(), media_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="bundle.zip"'})
        path = root / filename
        if not path.is_file():
            raise HTTPException(404, "Export unavailable; PDF requires isolated compilation")
        return FileResponse(path, filename=filename,
                            headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})

    return app
