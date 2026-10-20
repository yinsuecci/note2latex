import io
import json
import zipfile
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from note2latex.agent import TOOLS, review
from note2latex.api import create_app
from note2latex.config import Settings
from note2latex.engine import Engine
from note2latex.ingest import ingest
from note2latex.provider import Provider, ProviderError
from note2latex.schema import Block, Document, Page, validate_math


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 100), "white").save(buffer, "PNG")
    return buffer.getvalue()


def extracted(text="中文数学笔记"):
    return {"content": json.dumps({"blocks": [
        {"kind": "paragraph", "text": text},
        {"kind": "equation", "latex": r"\frac{1}{2}"},
        {"kind": "diagram", "shapes": [
            {"kind": "arrow", "points": [[0.1, 0.5], [0.9, 0.5]]},
            {"kind": "text", "points": [[0.8, 0.6]], "text": "x"},
        ]},
    ]})}


def call(name, args):
    return {"content": None, "tool_calls": [
        {"id": "call-1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def finish():
    return call("finish_review", {"warnings": []})


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def settings(tmp_path):
    return Settings(base_url="https://example.com/v1", api_key="test-secret",
                    vision_model="vision", agent_model="agent", data_dir=tmp_path)


@pytest.mark.parametrize("latex", [r"\input{secret}", r"\write18{cmd}", r"\def\x{a}",
                                  "x%comment", "$x$", r"\begin{document}x\end{document}",
                                  r"\frac{1}{2", "^^5cinput", r"\begin {document}"])
def test_reject_unsafe_math(latex):
    with pytest.raises(ValueError):
        validate_math(latex)


def test_math_and_schema():
    assert validate_math(r"\begin{aligned}x&=\sqrt{2}\\y&=3\end{aligned}")
    with pytest.raises(ValueError):
        Block(kind="diagram")
    with pytest.raises(ValueError):
        Block(id="../../file", kind="paragraph", text="a")
    assert "$ref" not in json.dumps(TOOLS)


def test_export_and_local_edit(settings):
    fake = Fake(extracted(), finish())
    engine = Engine(settings, fake)
    try:
        job = engine.store.create("BODY & TITLE", "article", [png()])
        engine.run(job["id"])
        first = engine.store.get(job["id"])
        assert first["status"] == "ready"
        root = engine.store.path(job["id"]) / "versions" / first["revision"]
        tex = (root / "note.tex").read_text(encoding="utf-8")
        assert r"\title{BODY \& TITLE}" in tex
        assert r"\begin{tikzpicture}" in tex
        assert r"\includegraphics" not in tex
        assert (root / "diagrams/p001-b003.svg").exists()
        block = engine.document(job["id"]).pages[0].blocks[0].model_dump()
        block["text"] = "修正后的文字"
        fake.responses.extend([call("replace_block", {
            "id": block["id"], "block": block, "reason": "Fix OCR",
        }), finish()])
        engine.refine(job["id"], "修正文字", block["id"], first["revision"]).result(10)
        second = engine.store.get(job["id"])
        assert second["revision"] != first["revision"]
        assert engine.document(job["id"]).pages[0].blocks[0].text == block["text"]
        assert root.exists()
        with pytest.raises(ValueError):
            engine.refine(job["id"], "stale", block["id"], first["revision"])
        fake.responses.append(ProviderError("Provider HTTP 401"))
        engine.refine(job["id"], "failed", block["id"], second["revision"]).result(10)
        assert engine.store.get(job["id"])["revision"] == second["revision"]
        assert engine.store.get(job["id"])["status"] == "failed"
    finally:
        engine.close()


def test_checkpoints_and_review_fallback(settings):
    fake = Fake(extracted(), ProviderError("offline"))
    engine = Engine(settings, fake)
    try:
        job = engine.store.create("test", "notes", [png(), png()])
        engine.run(job["id"])
        assert engine.store.get(job["id"])["status"] == "failed"
        fake.responses.extend([extracted(), ProviderError("review unavailable")])
        engine.resume(job["id"]).result(10)
        assert fake.calls == 4
        assert engine.store.get(job["id"])["status"] == "needs_review"
        assert len(engine.document(job["id"]).pages) == 2
    finally:
        engine.close()


def test_scope_and_budget(tmp_path):
    image = tmp_path / "page.png"
    image.write_bytes(png())
    a = Block(id="p001-b001", kind="paragraph", text="original")
    b = Block(id="p001-b002", kind="paragraph", text="untouched")
    doc = Document(title="test", pages=[Page(blocks=[a, b])])
    proposal = b.model_dump()
    proposal["text"] = "illegal"
    fake = Fake(call("replace_block", {"id": b.id, "block": proposal, "reason": "edit"}), finish())
    result, _ = review(fake, doc, [image], "edit first", a.id, 2, lambda: None, lambda *a, **k: None)
    assert result.pages[0].blocks[1].text == "untouched"
    with pytest.raises(ValueError, match="round limit"):
        review(Fake({"content": "No tools"}), doc, [image], "edit", a.id, 1,
               lambda: None, lambda *a, **k: None)


def test_cancel_and_recovery(settings):
    engine = Engine(settings, Fake())
    try:
        job = engine.store.create("test", "notes", [png()])
        engine.cancel(job["id"])
        engine.run(job["id"])
        assert engine.store.get(job["id"])["status"] == "cancelled"
        job2 = engine.store.create("test", "notes", [png()])
        engine.store.recover()
        assert engine.store.get(job2["id"])["status"] == "interrupted"
    finally:
        engine.close()


def test_api(settings):
    app = create_app(replace(settings, service_token="local-token"), Fake(extracted(), finish()))
    auth = {"Authorization": "Bearer local-token"}
    with TestClient(app) as client:
        assert client.get("/api/jobs").status_code == 401
        assert client.get("/health", headers=auth).json()["provider_configured"]
        # Run synchronously to make API assertions deterministic.
        app.state.engine.submit = app.state.engine.run
        response = client.post("/api/jobs", headers=auth, files={"files": ("note.png", png(), "image/png")})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert client.get(f"/api/jobs/{job_id}", headers=auth).json()["status"] == "ready"
        result = client.get(f"/api/jobs/{job_id}/download/bundle.zip", headers=auth)
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            assert "note.tex" in archive.namelist()
            assert not any("sources" in name for name in archive.namelist())
        assert client.get(f"/api/jobs/{job_id}/download/note.pdf", headers=auth).status_code == 404
        assert client.get("/api/jobs/not-valid", headers=auth).status_code == 404
        assert client.post("/api/jobs", headers=auth, files={"files": ("bad.png", b"bad")}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/refine", headers=auth, json={
            "target": "p001-b001", "revision": "0" * 32, "message": "fix",
        }).status_code == 409


def test_provider_redacts(settings):
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="test-secret"))
    with pytest.raises(ProviderError) as error:
        Provider(settings, transport).chat([])
    assert "test-secret" not in str(error.value)


def test_ingest():
    assert ingest("note.png", png())[0].startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        ingest("bad.pdf", b"bad")


def test_unconfigured_api(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path), Fake())) as client:
        assert not client.get("/health").json()["provider_configured"]
        assert client.post("/api/jobs", files={"files": ("a.png", png())}).status_code == 503


def test_advanced_geometry_and_unified_numbering(tmp_path):
    from note2latex.schema import Shape
    from note2latex.render import render, svg, tikz

    # Test shape validations
    with pytest.raises(ValueError):
        Shape(kind="polygon", points=[(0.1, 0.1), (0.2, 0.2)])  # requires >= 3 points
    with pytest.raises(ValueError):
        Shape(kind="arc", points=[(0.5, 0.5)], angles=(float("nan"), 90.0))

    diag_block = Block(
        id="p001-b001",
        kind="diagram",
        text="几何辅助图",
        shapes=[
            Shape(kind="polygon", points=[(0.2, 0.8), (0.8, 0.8), (0.5, 0.2)]),
            Shape(kind="line", points=[(0.5, 0.2), (0.5, 0.8)], dashed=True),  # 辅助高线
            Shape(kind="arc", points=[(0.2, 0.8)], radius=0.05, angles=(0, 60)),  # 角度弧
            Shape(kind="axis", points=[(0.05, 0.95), (0.95, 0.95)]),
            Shape(kind="text", points=[(0.5, 0.18)], text="A", text_pos="above"),
            Shape(kind="text", points=[(0.18, 0.82)], text="B", text_pos="below left"),
            Shape(kind="text", points=[(0.82, 0.82)], text="C", text_pos="below right"),
        ],
    )

    tikz_code = tikz(diag_block)
    assert r"\node[above] at (6.0000,6.5600) {A};" in tikz_code
    assert "dashed" in tikz_code
    assert "-- cycle;" in tikz_code
    assert "arc (0.0:60.0:" in tikz_code

    svg_code = svg(diag_block)
    assert '<polygon points=' in svg_code
    assert 'stroke-dasharray="5,5"' in svg_code
    assert '<path d="M ' in svg_code

    # Test unified math environments and equation tags
    doc = Document(
        title="解析几何与代数笔记",
        pages=[
            Page(blocks=[
                Block(id="p001-b001", kind="heading", level=1, text="第一节 三角形性质"),
                Block(id="p001-b002", kind="definition", tag="高线", text="从三角形一个顶点向其对边所作的垂线段。"),
                Block(id="p001-b003", kind="theorem", tag="勾股定理", text="在直角三角形中，斜边的平方等于两直角边的平方和。"),
                Block(id="p001-b004", kind="example", tag="例1", text="求直角边为3和4的直角三角形斜边。"),
                Block(id="p001-b005", kind="proof", text="根据勾股定理直接计算得出。"),
                Block(id="p001-b006", kind="equation", tag="(1.1)", latex="c^2 = a^2 + b^2"),
                Block(id="p001-b007", kind="equation", latex="c = \\sqrt{3^2 + 4^2} = 5"),
                diag_block,
            ])
        ]
    )

    out_dir = tmp_path / "out"
    render(doc, out_dir)

    tex_content = (out_dir / "note.tex").read_text(encoding="utf-8")
    assert r"\section{第一节 三角形性质}" in tex_content
    assert r"\begin{definition}[高线]" in tex_content
    assert r"\begin{theorem}[勾股定理]" in tex_content
    assert r"\begin{example}[例1]" in tex_content
    assert r"\begin{proof}" in tex_content
    assert r"\begin{equation}\tag{(1.1)}" in tex_content
    assert r"\begin{equation}\label{eq:p001-b007}" in tex_content

    md_content = (out_dir / "note.md").read_text(encoding="utf-8")
    assert "## 第一节 三角形性质" in md_content
    assert "> **定义** (高线)：" in md_content
    assert "> **定理** (勾股定理)：" in md_content
    assert r"\tag{(1.1)}" in md_content

