import html
import json
import math
import shutil
import subprocess
import tempfile
import re
import uuid
from pathlib import Path

from .schema import Document


def tex_text(text):
    replacements = {
        "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$",
        "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_",
        "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(c, c) for c in text)


def svg(block):
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="-20 -20 640 440" role="img">',
             '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
             'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
             '<path d="M 0 0 L 10 5 L 0 10 z" fill="#222"/></marker></defs>',
             '<g fill="none" stroke="#222" stroke-width="2">']
    for shape in block.shapes:
        pts = [(x * 600, y * 400) for x, y in shape.points]
        x, y = pts[0]
        dash_attr = ' stroke-dasharray="5,5"' if shape.dashed else ''
        if shape.kind in {"line", "arrow", "axis"}:
            end = ' marker-end="url(#arrow)"' if shape.kind in {"arrow", "axis"} else ""
            parts.append(f'<line x1="{x}" y1="{y}" x2="{pts[1][0]}" y2="{pts[1][1]}"{end}{dash_attr}/>')
        elif shape.kind == "rectangle":
            a, b = pts[1]
            parts.append(f'<rect x="{min(x,a)}" y="{min(y,b)}" width="{abs(a-x)}" height="{abs(b-y)}"{dash_attr}/>')
        elif shape.kind == "circle":
            parts.append(f'<circle cx="{x}" cy="{y}" r="{shape.radius*400}"{dash_attr}/>')
        elif shape.kind == "polygon":
            points_str = " ".join(f"{a},{b}" for a, b in pts)
            parts.append(f'<polygon points="{points_str}" fill="none"{dash_attr}/>')
        elif shape.kind == "polyline":
            points_str = " ".join(f"{a},{b}" for a, b in pts)
            parts.append(f'<polyline points="{points_str}"{dash_attr}/>')
        elif shape.kind == "arc" and shape.angles is not None:
            a1, a2 = math.radians(shape.angles[0]), math.radians(shape.angles[1])
            r = shape.radius * 400
            x1, y1 = x + r * math.cos(a1), y - r * math.sin(a1)
            x2, y2 = x + r * math.cos(a2), y - r * math.sin(a2)
            large = 1 if (shape.angles[1] - shape.angles[0]) % 360 > 180 else 0
            parts.append(f'<path d="M {x1} {y1} A {r} {r} 0 {large} 0 {x2} {y2}" fill="none"{dash_attr}/>')
        elif shape.kind == "text":
            pos_attrs = {
                "center": 'text-anchor="middle" dominant-baseline="middle"',
                "above": 'text-anchor="middle" dy="-8"',
                "below": 'text-anchor="middle" dy="16"',
                "left": 'text-anchor="end" dx="-8" dy="4"',
                "right": 'text-anchor="start" dx="8" dy="4"',
                "above left": 'text-anchor="end" dx="-6" dy="-6"',
                "above right": 'text-anchor="start" dx="6" dy="-6"',
                "below left": 'text-anchor="end" dx="-6" dy="14"',
                "below right": 'text-anchor="start" dx="6" dy="14"',
            }.get(shape.text_pos, 'text-anchor="middle" dominant-baseline="middle"')
            parts.append(f'<text x="{x}" y="{y}" {pos_attrs} font-size="16" stroke="none" fill="#222">{html.escape(shape.text)}</text>')
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts)


def tikz(block):
    lines = [r"\begin{tikzpicture}[x=1cm,y=1cm,>=Latex]"]
    for s in block.shapes:
        pts = [f"({x*12:.4f},{(1-y)*8:.4f})" for x, y in s.points]
        dash_opt = ",dashed" if s.dashed else ""
        if s.kind in {"line", "arrow", "axis"}:
            arrow_spec = "-Latex" if s.kind in {"arrow", "axis"} else ""
            opts = ["thick"]
            if arrow_spec:
                opts.append(arrow_spec)
            if s.dashed:
                opts.append("dashed")
            lines.append(r"\draw[" + ",".join(opts) + "] " + pts[0] + " -- " + pts[1] + ";")
        elif s.kind == "rectangle":
            lines.append(r"\draw[thick" + dash_opt + "] " + pts[0] + " rectangle " + pts[1] + ";")
        elif s.kind == "circle":
            lines.append(r"\draw[thick" + dash_opt + "] " + pts[0] + f" circle ({s.radius*8:.4f}cm);")
        elif s.kind == "polygon":
            lines.append(r"\draw[thick" + dash_opt + "] " + " -- ".join(pts) + " -- cycle;")
        elif s.kind == "polyline":
            lines.append(r"\draw[thick" + dash_opt + "] " + " -- ".join(pts) + ";")
        elif s.kind == "arc" and s.angles is not None:
            a1, a2 = s.angles
            r_cm = s.radius * 8
            lines.append(r"\draw[thick" + dash_opt + "] " + pts[0] + f" ++({a1:.1f}:{r_cm:.4f}cm) arc ({a1:.1f}:{a2:.1f}:{r_cm:.4f}cm);")
        elif s.kind == "text":
            pos_opt = f"[{s.text_pos}]" if s.text_pos != "center" else ""
            lines.append(r"\node" + pos_opt + " at " + pts[0] + " {" + tex_text(s.text) + "};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines)


def render(document: Document, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    (output / "diagrams").mkdir(exist_ok=True)
    md = ["# " + document.title, ""]
    page_html = []
    body = []
    
    env_names = {
        "definition": "定义",
        "theorem": "定理",
        "lemma": "引理",
        "corollary": "推论",
        "example": "例",
        "proof": "证明",
    }

    for n, page in enumerate(document.pages, 1):
        md.extend([f"<!-- source-page:{n} -->", ""])
        body.append(f"% Source page {n}")
        page_html.append(f'<section aria-label="Source page {n}">')
        for b in page.blocks:
            md.append(f"<!-- block:{b.id} -->")
            body.append(f"% Block {b.id}")
            if b.uncertain:
                md.append("> [Needs review]")
                page_html.append('<p class="warning">Needs review</p>')
                body.append(r"\par\noindent\textbf{[Needs review]}\par")
            safe = html.escape(b.text)

            if b.kind == "heading":
                hashes = "#" * max(2, min(5, b.level + 1))
                md.append(f"{hashes} " + b.text)
                heading_tags = {1: "h2", 2: "h3", 3: "h4"}.get(b.level, "h5")
                page_html.append(f'<{heading_tags} id="{b.id}">{safe}</{heading_tags}>')
                cmd = {1: r"\section", 2: r"\subsection", 3: r"\subsubsection"}.get(b.level, r"\paragraph")
                body.append(cmd + "{" + tex_text(b.text) + "}")
            elif b.kind in env_names:
                env = b.kind
                cname = env_names[env]
                tag_label = f" ({b.tag})" if b.tag else ""
                md.append(f"> **{cname}**{tag_label}：{b.text}")
                page_html.append(f'<div class="math-env {env}" id="{b.id}"><strong>{cname}</strong>{html.escape(tag_label)}: {safe}</div>')
                opt = f"[{tex_text(b.tag)}]" if b.tag else ""
                body.append(r"\begin{" + env + "}" + opt + "\n" + tex_text(b.text) + "\n\\end{" + env + "}")
            elif b.kind == "paragraph":
                prefix_md = f"**{b.tag}** " if b.tag else ""
                prefix_tex = r"\textbf{" + tex_text(b.tag) + "} " if b.tag else ""
                prefix_html = f"<strong>{html.escape(b.tag)}</strong> " if b.tag else ""
                md.append(prefix_md + b.text)
                page_html.append(f'<p id="{b.id}">{prefix_html}{safe}</p>')
                body.append(prefix_tex + tex_text(b.text) + "\n\\par")
            elif b.kind == "equation":
                if b.tag:
                    md.extend(["$$", b.latex + f" \\tag{{{b.tag}}}", "$$"])
                    page_html.append(f'<pre class="equation" id="{b.id}">{html.escape(b.latex)} (tag: {html.escape(b.tag)})</pre>')
                    body.append(r"\begin{equation}\tag{" + tex_text(b.tag) + "}\n" + b.latex + "\n\\end{equation}")
                else:
                    md.extend(["$$", b.latex, "$$"])
                    page_html.append(f'<pre class="equation" id="{b.id}">{html.escape(b.latex)}</pre>')
                    body.append(r"\begin{equation}\label{eq:" + b.id + "}\n" + b.latex + "\n\\end{equation}")
            else:
                filename = f"diagrams/{b.id}.svg"
                vector = svg(b)
                (output / filename).write_text(vector, encoding="utf-8")
                md.append(f"![Diagram {b.id}]({filename})")
                page_html.append(f'<figure id="{b.id}">{vector}<figcaption>{safe}</figcaption></figure>')
                caption = f"\n\\caption*{{{tex_text(b.text)}}}" if b.text.strip() else ""
                body.append("\\par\\noindent\\begin{minipage}{\\linewidth}\\centering\n" +
                            "\\resizebox{0.85\\linewidth}{!}{%\n" + tikz(b) + "\n}" +
                            caption + "\n\\end{minipage}\\par")
            md.append("")
        page_html.append("</section>")
    margin = {"notes": "2cm", "article": "2.5cm", "homework": "2.2cm"}[document.template]
    tex = r"""\documentclass[11pt,a4paper,UTF8]{ctexart}
\usepackage{amsmath,amssymb,amsthm,graphicx,tikz,geometry}
\usetikzlibrary{arrows.meta}
\geometry{margin=MARGIN}
\linespread{1.25}
\setlength{\parindent}{2em}
\setlength{\parskip}{0.35em}

\theoremstyle{definition}
\newtheorem{definition}{定义}
\newtheorem{example}{例}
\theoremstyle{plain}
\newtheorem{theorem}{定理}
\newtheorem{lemma}[theorem]{引理}
\newtheorem{corollary}[theorem]{推论}

\title{TITLE}
\author{}
\date{}
\begin{document}
\maketitle
BODY
\end{document}
"""
    values = {"MARGIN": margin, "TITLE": tex_text(document.title), "BODY": "\n".join(body)}
    tex = re.sub(r"MARGIN|TITLE|BODY", lambda match: values[match[0]], tex)
    # HTML intentionally displays LaTeX source; no untrusted HTML or remote scripts.
    html_doc = '<!doctype html><html lang="zh-CN"><meta charset="utf-8">' + \
        '<meta name="viewport" content="width=device-width,initial-scale=1">' + \
        '<title>' + html.escape(document.title) + '</title><style>' + \
        'body{max-width:860px;margin:40px auto;padding:0 24px;font:18px/1.8 Georgia,serif;' + \
        'background:#faf8f1;color:#202927}p{white-space:pre-wrap}svg{max-height:400px;width:100%}' + \
        '.equation{overflow:auto;background:#eeeae0;padding:20px}.warning{color:#a43c20}' + \
        '.math-env{border-left:4px solid #4a6fa5;padding:8px 16px;margin:12px 0;background:#f3f6fa}' + \
        '</style><h1>' + html.escape(document.title) + '</h1>' + "".join(page_html) + '</html>'
    (output / "note.md").write_text("\n".join(md), encoding="utf-8")
    (output / "note.html").write_text(html_doc, encoding="utf-8")
    (output / "note.tex").write_text(tex, encoding="utf-8")
    (output / "document.json").write_text(document.model_dump_json(indent=2), encoding="utf-8")


def compile_pdf(output: Path, image: str):
    if not shutil.which("docker"):
        raise RuntimeError("PDF compilation requires Docker and the local compiler image")
    with tempfile.TemporaryDirectory(prefix="note2latex-") as folder:
        work = Path(folder)
        shutil.copyfile(output / "note.tex", work / "note.tex")
        name = "note2latex-" + uuid.uuid4().hex
        command = [
            "docker", "run", "--name", name, "--rm", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "--mount", f"type=bind,source={work.resolve()},target=/work",
            "--workdir", "/work", image, "xelatex", "-no-shell-escape",
            "-interaction=nonstopmode", "-halt-on-error", "note.tex",
        ]
        for _ in range(2):
            try:
                result = subprocess.run(command, capture_output=True, timeout=90)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
                raise RuntimeError("Isolated compiler timed out") from None
            if result.returncode:
                raise RuntimeError("Isolated LaTeX compilation failed")
        pdf = work / "note.pdf"
        if not pdf.exists() or not pdf.read_bytes().startswith(b"%PDF"):
            raise RuntimeError("Compiler did not generate a PDF")
        shutil.copyfile(pdf, output / "note.pdf")

