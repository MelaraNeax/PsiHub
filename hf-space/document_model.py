"""PsiHub Document Intelligence Layer.

Builds a lightweight, provenance-preserving intermediate representation from
native PDF geometry. It is deliberately independent from Markdown so that
extraction, visual arbitration, translation and rendering can evolve without
making Markdown the source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any
import math
import re
import statistics

import fitz


@dataclass
class Block:
    id: str
    page: int
    kind: str
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = 1.0
    reading_order: int = 0
    column: int = 0
    source: str = "pdf-native"
    parent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageModel:
    page: int
    width: float
    height: float
    columns: int
    blocks: list[Block] = field(default_factory=list)
    risk: float = 0.0
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class DocumentModel:
    pages: list[PageModel]
    version: str = "2.0"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def blocks(self) -> list[Block]:
        return [b for p in self.pages for b in p.blocks]

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "pages": [p.to_dict() for p in self.pages]}

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for b in self.blocks:
            counts[b.kind] = counts.get(b.kind, 0) + 1
        risky = [p.page for p in self.pages if p.risk >= 0.45]
        return {
            "version": self.version,
            "pages": self.page_count,
            "blocks": len(self.blocks),
            "kinds": counts,
            "risky_pages": risky,
            "mean_page_risk": round(statistics.mean((p.risk for p in self.pages)), 3) if self.pages else 0,
        }


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _kind(text: str, size: float, page_height: float, bbox: tuple[float, float, float, float], page_width: float) -> str:
    t = _clean(text)
    low = t.lower()
    x0, y0, x1, y1 = bbox
    if not t:
        return "empty"
    if y0 < page_height * 0.09 and len(t) < 180:
        return "header"
    if y1 > page_height * 0.91 and len(t) < 220:
        return "footer"
    if re.match(r"^(figure|fig\.?|table|tab\.?|appendix|supplementary)\b", low):
        return "caption_or_label"
    if re.search(r"\b(?:p|pp)\.?\s*\d+", low) and len(t) < 80:
        return "citation"
    if size >= 1.22 and len(t) < 220:
        return "heading"
    if re.search(r"[=∑∫√≤≥≈±]", t) and (len(t) < 500 or t.count("=") >= 2):
        return "equation_or_math"
    if re.match(r"^(?:[-•●▪◦]|\(?\d+[.)])\s+", t):
        return "list_item"
    if x0 > page_width * 0.08 and x1 < page_width * 0.92 and y1 > page_height * 0.82 and len(t) < 600:
        # Weak footnote signal; final classification uses native block metadata too.
        if size <= 0.92:
            return "footnote_or_small_text"
    return "paragraph"


def _column_assignment(bbox: tuple[float, float, float, float], width: float, columns: int) -> int:
    if columns < 2:
        return 0
    center = (bbox[0] + bbox[2]) / 2
    return 0 if center < width / 2 else 1


def _page_columns(page_dict: dict[str, Any], width: float) -> int:
    spans = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            bbox = line.get("bbox")
            if bbox:
                x0, y0, x1, y1 = bbox
                if (x1 - x0) < width * 0.72:
                    spans.append((x0 + x1) / 2)
    left = sum(x < width * 0.47 for x in spans)
    right = sum(x > width * 0.53 for x in spans)
    return 2 if left >= 3 and right >= 3 else 1


def build_document_model(doc: fitz.Document) -> DocumentModel:
    pages: list[PageModel] = []
    for pno, page in enumerate(doc, 1):
        width, height = page.rect.width, page.rect.height
        raw = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        columns = _page_columns(raw, width)
        blocks: list[Block] = []
        for bi, block in enumerate(raw.get("blocks", [])):
            if block.get("type") != 0:
                continue
            lines = block.get("lines", [])
            texts = []
            sizes = []
            for line in lines:
                for span in line.get("spans", []):
                    if span.get("text"):
                        texts.append(span["text"])
                        sizes.append(float(span.get("size", 10)))
            text = _clean(" ".join(texts))
            bbox = tuple(float(x) for x in block.get("bbox", (0, 0, 0, 0)))
            if not text:
                continue
            size = statistics.median(sizes) if sizes else 10.0
            kind = _kind(text, size, height, bbox, width)
            col = _column_assignment(bbox, width, columns)
            blocks.append(Block(
                id=f"p{pno}b{bi}", page=pno, kind=kind, text=text, bbox=bbox,
                column=col, metadata={"font_size_median": round(size, 2), "span_count": len(sizes)}
            ))

        # Geometry-based reading order. Headers/footers are retained in the model
        # but placed around body content according to their physical position.
        if columns == 2:
            ordered = sorted(blocks, key=lambda b: (b.column, b.bbox[1], b.bbox[0]))
        else:
            ordered = sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
        for idx, b in enumerate(ordered):
            b.reading_order = idx

        signals: list[str] = []
        risk = 0.0
        if columns == 2:
            signals.append("two_column")
            risk += 0.15
        kinds = [b.kind for b in ordered]
        if "equation_or_math" in kinds:
            signals.append("math_present")
            risk += 0.08
        if "caption_or_label" in kinds:
            signals.append("figure_or_table_caption")
            risk += 0.08
        if kinds.count("footnote_or_small_text") >= 2:
            signals.append("footnote_density")
            risk += 0.10
        if len(ordered) >= 18:
            risk += 0.08
        # Abrupt geometry jumps are a useful proxy for mixed regions.
        ys = [b.bbox[1] for b in ordered]
        if len(ys) >= 8:
            jumps = sum(1 for a, b in zip(ys, ys[1:]) if abs(b - a) > height * 0.18)
            if jumps >= 2:
                signals.append("large_geometry_jumps")
                risk += 0.15
        pages.append(PageModel(pno, width, height, columns, ordered, min(1.0, risk), signals))
    return DocumentModel(pages)


def page_visual_candidates(model: DocumentModel, threshold: float = 0.25) -> list[int]:
    """Return 1-indexed pages where a visual arbiter is most valuable."""
    return [p.page for p in model.pages if p.risk >= threshold]


def validate_model(model: DocumentModel) -> list[dict[str, Any]]:
    """Structural invariants; these never mutate document content."""
    issues: list[dict[str, Any]] = []
    for p in model.pages:
        orders = [b.reading_order for b in p.blocks]
        if len(orders) != len(set(orders)):
            issues.append({"page": p.page, "type": "duplicate_reading_order", "severity": "high"})
        for b in p.blocks:
            x0, y0, x1, y1 = b.bbox
            if x1 < x0 or y1 < y0:
                issues.append({"page": p.page, "block": b.id, "type": "invalid_bbox", "severity": "high"})
            if not math.isfinite(x0 + y0 + x1 + y1):
                issues.append({"page": p.page, "block": b.id, "type": "nonfinite_bbox", "severity": "high"})
    return issues


@dataclass
class ExtractionEvidence:
    page: int
    native_score: float
    visual_score: float = 0.0
    agreement: float = 1.0
    selected: str = "native"
    reasons: list[str] = field(default_factory=list)


def score_page_complexity(page: PageModel) -> float:
    """More granular risk score used by the VLM router."""
    score = page.risk
    kinds = [b.kind for b in page.blocks]
    if page.columns >= 2:
        score += 0.12
    if "equation_or_math" in kinds:
        score += 0.10
    if kinds.count("caption_or_label") >= 2:
        score += 0.08
    if len(page.blocks) >= 25:
        score += 0.08
    if any(len(b.text) > 900 for b in page.blocks):
        score += 0.06
    return min(1.0, score)


def build_visual_router(model: DocumentModel, threshold: float = 0.32) -> dict[int, str]:
    """Return a deterministic routing decision for each page."""
    out = {}
    for page in model.pages:
        risk = score_page_complexity(page)
        out[page.page] = "visual" if risk >= threshold else "native"
    return out


def audit_block_sequence(model: DocumentModel) -> list[dict[str, Any]]:
    """Detect suspicious geometry/order patterns before any model call."""
    issues = []
    for page in model.pages:
        blocks = sorted(page.blocks, key=lambda b: b.reading_order)
        for a, b in zip(blocks, blocks[1:]):
            if page.columns == 1 and b.bbox[1] + 4 < a.bbox[1] and abs(b.bbox[0]-a.bbox[0]) < page.width*0.1:
                issues.append({"page": page.page, "type": "backward_y", "blocks": [a.id,b.id]})
            if page.columns == 2 and a.column != b.column and abs(a.bbox[1]-b.bbox[1]) < page.height*0.05:
                issues.append({"page": page.page, "type": "column_transition", "blocks": [a.id,b.id]})
    return issues
