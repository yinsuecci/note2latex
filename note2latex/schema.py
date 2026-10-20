import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


MATH_COMMANDS = set("""
frac dfrac tfrac sqrt sum prod int iint iiint oint lim inf sup min max
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln exp
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr
cdot times div pm mp le leq ge geq ne neq approx sim simeq equiv propto
in notin subset subseteq supset supseteq cup cap emptyset varnothing
forall exists neg land lor to mapsto rightarrow leftarrow leftrightarrow
Rightarrow Leftarrow Leftrightarrow implies iff infty partial nabla ell
hat widehat bar overline underline vec dot ddot tilde widetilde
mathbf mathrm mathit mathsf mathbb mathcal boldsymbol text operatorname
quad qquad space hspace limits nolimits displaystyle textstyle
begin end label ref tag nonumber notag
ldots cdots vdots ddots lceil rceil lfloor rfloor langle rangle
overbrace underbrace overset underset binom dbinom tbinom
""".split()) - {"label", "ref", "hspace"}
MATH_ENVS = {"aligned", "gathered", "split", "matrix", "pmatrix", "bmatrix",
             "Bmatrix", "vmatrix", "Vmatrix", "cases", "smallmatrix"}


def validate_math(value: str) -> str:
    if not value.strip():
        raise ValueError("Empty equation")
    if "$" in value or "^^" in value or "%" in value or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError("Equation must contain math body only, without comments or delimiters")
    for command in re.findall(r"\\([A-Za-z]+|.)", value):
        if command.isascii() and command.isalpha() and command not in MATH_COMMANDS:
            raise ValueError(f"Unsupported math command: {command}")
    stack = []
    for match in re.finditer(r"\\(begin|end)\s*\{([^{}]+)\}", value):
        op, env = match.groups()
        if env not in MATH_ENVS:
            raise ValueError("Unsupported math environment")
        if op == "begin":
            stack.append(env)
        elif not stack or stack.pop() != env:
            raise ValueError("Unbalanced math environments")
    if stack:
        raise ValueError("Unbalanced math environments")
    depth = 0
    for match in re.finditer(r"\\.|[{}]", value):
        token = match.group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        if depth < 0 or depth > 64:
            raise ValueError("Invalid math braces")
    if depth:
        raise ValueError("Unbalanced math braces")
    return value


class Shape(StrictModel):
    kind: Literal["line", "arrow", "rectangle", "circle", "arc", "polygon", "polyline", "text", "axis"]
    points: list[tuple[float, float]] = Field(min_length=1, max_length=100)
    radius: float = Field(default=0.03, gt=0, le=0.5)
    angles: tuple[float, float] | None = None
    dashed: bool = False
    text: str = Field(default="", max_length=200)
    text_pos: Literal[
        "center", "above", "below", "left", "right",
        "above left", "above right", "below left", "below right",
    ] = "center"

    @model_validator(mode="after")
    def check(self):
        if any(not math.isfinite(v) or not 0 <= v <= 1 for p in self.points for v in p):
            raise ValueError("Shape points must be finite normalized coordinates")
        if self.kind == "polygon" and len(self.points) < 3:
            raise ValueError("Polygon requires at least 3 points")
        required = 2 if self.kind in {"line", "arrow", "rectangle", "polyline", "axis"} else 1
        if len(self.points) < required:
            raise ValueError("Not enough points")
        if self.angles is not None:
            a, b = self.angles
            if not (math.isfinite(a) and math.isfinite(b)):
                raise ValueError("Angles must be finite numbers")
        return self


TEXT_BLOCK_KINDS = {
    "heading", "paragraph", "definition", "theorem",
    "lemma", "corollary", "example", "proof",
}


class Block(StrictModel):
    id: str = Field(default="", pattern=r"^(?:p\d{3}-b\d{3})?$", max_length=40)
    kind: Literal[
        "heading", "paragraph", "equation", "diagram",
        "definition", "theorem", "lemma", "corollary", "example", "proof",
    ]
    level: int = Field(default=1, ge=1, le=4)
    tag: str = Field(default="", max_length=60)
    text: str = Field(default="", max_length=16000)
    latex: str = Field(default="", max_length=8000)
    bbox: tuple[float, float, float, float] | None = None
    shapes: list[Shape] = Field(default_factory=list, max_length=150)
    uncertain: bool = False

    @model_validator(mode="after")
    def check(self):
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in self.bbox) or x0 >= x1 or y0 >= y1:
                raise ValueError("bbox must be normalized xyxy")
        if self.kind == "equation":
            validate_math(self.latex)
        if self.kind == "diagram" and not self.shapes:
            raise ValueError("Diagram requires real vector primitives, not a placeholder")
        if self.kind != "diagram" and self.shapes:
            raise ValueError("Only diagrams have shapes")
        if self.kind != "equation" and self.latex:
            raise ValueError("Only equations have latex")
        if self.kind in TEXT_BLOCK_KINDS and not self.text.strip():
            raise ValueError("Text block cannot be empty")
        return self


class Page(StrictModel):
    blocks: list[Block] = Field(min_length=1, max_length=120)


class Document(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    template: Literal["notes", "article", "homework"] = "notes"
    pages: list[Page] = Field(min_length=1, max_length=12)


def assign_ids(page: Page, index: int) -> Page:
    for n, block in enumerate(page.blocks, 1):
        block.id = f"p{index:03d}-b{n:03d}"
    return page

