import json
import uuid
from concurrent.futures import ThreadPoolExecutor

from .agent import review, transcribe
from .config import Settings
from .provider import Provider, ProviderError
from .render import compile_pdf, render
from .schema import Document, Page
from .store import Store, atomic_json


class Cancelled(Exception):
    pass


class Engine:
    def __init__(self, settings: Settings, provider=None):
        self.settings = settings
        self.store = Store(settings.data_dir)
        self.provider = provider or Provider(settings)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="note2latex")

    def close(self):
        self.pool.shutdown(wait=True)

    def submit(self, job_id):
        return self.pool.submit(self.run, job_id)

    def cancel(self, job_id):
        return self.store.update(job_id, allowed={"queued", "running"}, cancel_requested=True)

    def resume(self, job_id):
        self.store.update(job_id, allowed={"failed", "interrupted", "cancelled"},
                          status="queued", error=None, cancel_requested=False)
        return self.submit(job_id)

    def refine(self, job_id, message, target, revision):
        meta = self.store.get(job_id)
        if revision != meta["revision"] or not revision:
            raise ValueError("Stale revision; reload document before editing")
        doc = self.document(job_id)
        if target not in {b.id for p in doc.pages for b in p.blocks}:
            raise ValueError("Select an existing block ID for local edits")
        self.store.update(job_id, allowed={"ready", "needs_review"}, expected_revision=revision, status="queued",
                          pending_edit={"message": message, "target": target, "base_revision": revision},
                          error=None, cancel_requested=False)
        return self.submit(job_id)

    def document(self, job_id):
        meta = self.store.get(job_id)
        if not meta["revision"]:
            raise ValueError("No published revision")
        path = self.store.path(job_id) / "versions" / meta["revision"] / "document.json"
        return Document.model_validate_json(path.read_text(encoding="utf-8"))

    def run(self, job_id):
        try:
            meta = self.store.update(job_id, allowed={"queued"}, status="running", stage="extract")
        except ValueError:
            return
        root = self.store.path(job_id)
        images = sorted((root / "sources").glob("*.png"))

        def emit(stage, message, **details):
            self.store.event(job_id, stage, message, **details)

        def check():
            if self.store.get(job_id)["cancel_requested"]:
                raise Cancelled()

        try:
            check()
            pending = meta["pending_edit"]
            warnings = []
            if pending:
                document = self.document(job_id)
                instruction, target = pending["message"], pending["target"]
            else:
                pages = []
                for i, image in enumerate(images, 1):
                    check()
                    checkpoint = root / "checkpoints" / f"p{i:03d}.json"
                    if checkpoint.exists():
                        page = Page.model_validate_json(checkpoint.read_text(encoding="utf-8"))
                        emit("extract", "Reused page checkpoint", page=i)
                    else:
                        emit("extract", "Transcribing page", page=i)
                        page = transcribe(self.provider, image, i, check)
                        atomic_json(checkpoint, page.model_dump())
                    pages.append(page)
                document = Document(title=meta["title"], template=meta["template"], pages=pages)
                instruction, target = "Correct transcription against the source pages only.", None
                atomic_json(root / "raw.json", document.model_dump())
            if pending or self.settings.auto_review:
                self.store.update(job_id, stage="review")
                emit("review", "Agent started")
                try:
                    document, warnings = review(
                        self.provider, document, images, instruction, target,
                        self.settings.max_rounds, check, emit,
                    )
                except (ValueError, ProviderError):
                    if pending:
                        raise
                    # Extraction remains usable if optional review fails; no silent success.
                    warnings = ["Agent review incomplete; original transcription retained"]
                    emit("review", "Review failed; retained raw transcription")
            else:
                warnings = ["Automatic review disabled; transcription is unreviewed"]
            check()
            uncertain = [b.id for p in document.pages for b in p.blocks if b.uncertain]
            if uncertain:
                warnings.append("Uncertain blocks: " + ", ".join(uncertain))
            revision = uuid.uuid4().hex
            output = root / "versions" / revision
            self.store.update(job_id, stage="render")
            render(document, output)
            if self.settings.compile_pdf:
                self.store.update(job_id, stage="compile")
                compile_pdf(output, self.settings.tex_image)
            check()
            atomic_json(output / "provenance.json", {
                "vision_model": self.settings.vision_model,
                "agent_model": self.settings.agent_model,
                "parent_revision": meta["revision"],
                "edit": pending, "warnings": warnings,
            })
            # Publication occurs only after every requested export succeeds.
            self.store.update(job_id, status="needs_review" if warnings else "ready", stage="done",
                              revision=revision, warnings=warnings, error=None, pending_edit=None)
            emit("done", "Published revision", revision=revision)
        except Cancelled:
            self.store.update(job_id, status="cancelled", stage="cancelled")
            emit("cancelled", "Stopped at a safe boundary; checkpoints retained")
        except Exception as exc:
            if isinstance(exc, ProviderError):
                error = str(exc)
            else:
                error = f"{type(exc).__name__}: processing failed; previous revision and checkpoints retained"
            self.store.update(job_id, status="failed", error=error)
            emit("failed", error)
