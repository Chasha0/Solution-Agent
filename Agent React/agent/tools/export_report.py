"""export_report tool — compose all session artifacts into final.md + final.pdf.

Reads artifacts in stage order: requirements.json → research.md → summary.md
→ design_vN.md (latest). Composes a single markdown report, writes
`artifacts/final/final.md`, then renders `artifacts/final/final.pdf` via
fpdf2 (P0 decision: fpdf2 — pure Python, Windows-friendly).

Markdown → PDF is a *light* conversion:
- `# / ## / ###` headers → larger bold cells
- Bulleted lists and plain paragraphs → multi_cell text
- Code fences and tables are passed through as plain text (good enough for demo)

Limitations:
- fpdf2's default font (Helvetica) is Latin-1 only. CJK characters in the
  source markdown are **not** rendered; they appear as `?` or get dropped by
  fpdf2. For demo purposes we recommend English-heavy artifacts. If CJK
  rendering becomes a hard requirement, ship a CJK TTF and call
  `pdf.add_font("NotoSansCJK", fname=..., uni=True)`.

Returns JSON: `{"ok": true, "md": "<path>", "pdf": "<path>"}`.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .base import ToolSpec

logger = logging.getLogger(__name__)

# Markdown section header pattern (down to ###).
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# `<!-- anchor:xxx -->` markers used by stage handlers (spec §3).
_ANCHOR_RE = re.compile(r"<!--\s*anchor:([\w\-]+)\s*-->")


def _get_artifacts_cls() -> Any:
    try:
        from agent.storage.artifacts import Artifacts  # type: ignore
        return Artifacts
    except ImportError as e:
        logger.warning(f"[export_report] agent.storage.artifacts not available: {e}")
        return None


def _read_optional(artifacts: Any, stage: str, name: str) -> str | None:
    """Read an artifact; return None if missing rather than raising."""
    read = getattr(artifacts, "read", None)
    if not callable(read):
        return None
    try:
        result = read(stage, name)
    except (KeyError, FileNotFoundError):
        return None
    except Exception:
        return None
    return result if isinstance(result, str) else (str(result) if result is not None else None)


def _artifacts_root(artifacts: Any) -> Path | None:
    """Resolve the artifacts root directory of an `Artifacts` instance.

    P2 exposes this as the `.artifacts_root` property; some impls may use
    `.root` instead. Returns the path or None if neither is available.
    """
    for attr in ("artifacts_root", "root"):
        root = getattr(artifacts, attr, None)
        if root is not None:
            return Path(root)
    return None


def _find_latest_design(artifacts: Any) -> tuple[int, str] | None:
    """Return (version_int, content) for the latest design artifact, or None.

    P2 writes artifacts as `<name>_v<N>.md`. Stage handlers conventionally
    call write with name `design_v1`, `design_v2`, ... so on disk we get
    `design_v1_v1.md`, `design_v2_v1.md`, etc. (artifact version starts at 1
    per unique name).

    "Latest" = highest artifact-identifier (1, 2, 3, ...); ties broken by the
    highest artifact version. Returns the artifact version (N) as the first
    tuple element so callers can label the section accordingly.
    """
    root = _artifacts_root(artifacts)
    if root is None:
        return None
    design_dir = root / "design"
    if not design_dir.exists() or not design_dir.is_dir():
        return None
    # Match both `design_vK_vN.md` (P2 with our naming convention) AND any
    # direct `design_vN.md` (older / non-P2 impls) for robustness.
    p2_re = re.compile(r"^design_v(\d+)_v(\d+)\.md$")
    legacy_re = re.compile(r"^design_v(\d+)\.md$")
    candidates: list[tuple[int, int, Path]] = []
    for p in design_dir.iterdir():
        if not p.is_file():
            continue
        m = p2_re.match(p.name)
        if m:
            k, n = int(m.group(1)), int(m.group(2))
            candidates.append((k, n, p))
            continue
        m = legacy_re.match(p.name)
        if m:
            k, n = int(m.group(1)), 1
            candidates.append((k, n, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    _, n, path = candidates[-1]
    try:
        return n, path.read_text(encoding="utf-8")
    except Exception:
        return None


def _read_requirements(artifacts: Any) -> str:
    """Return a one-paragraph plain-text rendering of requirements.json."""
    raw = _read_optional(artifacts, "requirements", "requirements.json")
    if not raw:
        return "_（无需求数据）_"
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict):
        # Render top-level fields as `key: value` lines.
        lines = []
        for k, v in data.items():
            lines.append(f"- **{k}**: {v}")
        return "\n".join(lines) if lines else raw
    if isinstance(data, list):
        return "\n".join(f"- {item}" for item in data)
    return str(data)


def _compose_markdown(
    requirements_text: str,
    research: str | None,
    summary: str | None,
    design: str | None,
    design_version: int | None,
) -> str:
    """Assemble the final report as one markdown string."""
    parts: list[str] = []
    parts.append("# Solution Research Report")
    parts.append("")
    parts.append(
        "本文档由 Solution Research Agent 自动生成，汇总需求 → 调研 → "
        "总结 → 方案的全流程产物。\n"
    )
    parts.append("---")
    parts.append("")
    parts.append("## 1. 需求概要 (Requirements)")
    parts.append("")
    parts.append(requirements_text.strip() or "_（无需求数据）_")
    parts.append("")

    if research:
        parts.append("## 2. 调研发现 (Research)")
        parts.append("")
        parts.append(research.strip())
        parts.append("")
    else:
        parts.append("## 2. 调研发现 (Research)")
        parts.append("")
        parts.append("_（无调研产物）_")
        parts.append("")

    if summary:
        parts.append("## 3. 调研小结 (Summary)")
        parts.append("")
        parts.append(summary.strip())
        parts.append("")
    else:
        parts.append("## 3. 调研小结 (Summary)")
        parts.append("")
        parts.append("_（无小结产物）_")
        parts.append("")

    if design:
        version_label = f"v{design_version}" if design_version is not None else "latest"
        parts.append(f"## 4. 方案 ({version_label})")
        parts.append("")
        parts.append(design.strip())
        parts.append("")
    else:
        parts.append("## 4. 方案")
        parts.append("")
        parts.append("_（无方案产物）_")
        parts.append("")

    parts.append("---")
    parts.append("")
    parts.append(
        "_本报告由 Solution Research Agent 自动生成。最终解释权归项目组所有。_"
    )
    return "\n".join(parts)


def _write_pdf(md_text: str, pdf_path: Path, title: str = "Solution Research Report") -> Path:
    """Render md_text to a simple PDF with fpdf2. Returns pdf_path.

    Markdown is parsed line-by-line for `# / ## / ###` headers; everything
    else is rendered via `multi_cell`. CJK will not render with the default
    Helvetica font — caller's responsibility to provide ASCII-heavy content
    or to add a CJK TTF via `pdf.add_font(...)` (not done here).
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title page
    pdf.set_font("Helvetica", "B", 20)
    pdf.ln(20)
    pdf.cell(text=title, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(
        text="Generated by Solution Research Agent",
        new_x="LMARGIN",
        new_y="NEXT",
        align="C",
    )
    pdf.ln(20)

    # Body — split on H1 to start a fresh page; H2/H3 get bigger fonts in-line.
    sections = re.split(r"(?m)^# .+$", md_text)
    headers = re.findall(r"(?m)^# (.+)$", md_text)
    # First chunk is whatever comes before the first H1 (usually the doc title).
    preamble, sections = (sections[0], sections[1:]) if sections else ("", [])

    if preamble.strip():
        pdf.set_font("Helvetica", size=10)
        width = pdf.epw
        for line in preamble.splitlines():
            if not line.strip():
                pdf.ln(3)
                continue
            pdf.multi_cell(w=width, h=5, text=_safe_pdf(line))
        pdf.ln(3)

    for header, body in zip(headers, sections):
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(text=_safe_pdf(header.strip()), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(5)
        _render_body(pdf, body)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(pdf_path))
    return pdf_path


def _render_body(pdf: Any, body: str) -> None:
    """Render markdown body content to the open fpdf2 document.

    Uses an explicit width (`pdf.epw` = effective page width) instead of
    `w=0`. fpdf2's `multi_cell(w=0, ...)` interacts badly with auto page
    breaks and raises "Not enough horizontal space" when the cursor x is
    reset across page boundaries.
    """
    width = pdf.epw  # A4 - 2*margin, recomputed per page by fpdf2
    for line in body.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            if level == 1:
                # already handled at top level
                pdf.set_font("Helvetica", "B", 16)
            elif level == 2:
                pdf.set_font("Helvetica", "B", 13)
            elif level == 3:
                pdf.set_font("Helvetica", "B", 11)
            else:
                pdf.set_font("Helvetica", "B", 10)
            if text:
                pdf.cell(text=_safe_pdf(text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
            pdf.set_font("Helvetica", size=10)
            continue
        # Drop anchor comments — they're metadata, not content.
        if _ANCHOR_RE.match(line.strip()):
            continue
        if not line.strip():
            pdf.ln(2)
            continue
        pdf.multi_cell(w=width, h=5, text=_safe_pdf(line))


def _safe_pdf(text: str) -> str:
    """Best-effort Latin-1 sanitization for fpdf2 default font.

    Non-Latin-1 chars are replaced with '?'. CJK rendering needs a TTF add-on
    (see module docstring).
    """
    if not text:
        return ""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return text.encode("latin-1", errors="replace").decode("latin-1")


async def export_report(session_id: str, format: str = "pdf") -> str:
    """Compose the final report. Returns JSON with `md` and (optionally) `pdf` paths.

    `format`:
      - "pdf" (default): write final.md + final.pdf
      - "md":            write final.md only
      - anything else:   treated like "md" (defensive — never crashes)
    """
    if not session_id:
        return json.dumps(
            {"ok": False, "error": "session_id is required"},
            ensure_ascii=False,
        )

    Artifacts = _get_artifacts_cls()
    if Artifacts is None:
        return json.dumps(
            {"ok": False, "error": "agent.storage not available (P2 pending)"},
            ensure_ascii=False,
        )

    try:
        artifacts = Artifacts(session_id)
        requirements_text = _read_requirements(artifacts)
        research = _read_optional(artifacts, "research", "research.md")
        summary = _read_optional(artifacts, "summarize", "summary.md")
        design_pair = _find_latest_design(artifacts)
        design_version = design_pair[0] if design_pair else None
        design = design_pair[1] if design_pair else None

        md_text = _compose_markdown(
            requirements_text, research, summary, design, design_version
        )

        # Write final.md via Artifacts.write so versioning/locking is consistent.
        # P2 writes artifacts as `<name>_v<N>.md`; we use a clean name "final"
        # so the on-disk file is `final_v1.md` (1-indexed, increments on re-export).
        result = artifacts.write("final", "final", md_text)
        md_path: Path | None = None
        if isinstance(result, int):
            root = _artifacts_root(artifacts)
            if root is not None:
                md_path = root / "final" / f"final_v{result}.md"
        elif isinstance(result, dict) and result.get("path") is not None:
            md_path = Path(str(result["path"]))

        pdf_path: Path | None = None
        if format.lower() in ("pdf", "both", "all"):
            root = _artifacts_root(artifacts)
            if root is not None:
                # PDF isn't part of the versioned artifact schema — write to
                # `final/final.pdf` next to the markdown. Re-exporting will
                # overwrite this file.
                pdf_path = root / "final" / "final.pdf"
                _write_pdf(md_text, pdf_path)
            else:
                logger.warning("[export_report] cannot locate artifacts root; skipping PDF")
    except Exception as e:
        logger.exception("[export_report] failed")
        return json.dumps(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ok": True,
            "md": str(md_path) if md_path is not None else None,
            "pdf": str(pdf_path) if pdf_path is not None else None,
        },
        ensure_ascii=False,
    )


_SPEC = ToolSpec(
    name="export_report",
    description=(
        "Compose all artifacts for the current session into a single Markdown "
        "report (requirements → research → summary → latest design) and "
        "render it to PDF via fpdf2. Writes final.md and final.pdf under "
        "artifacts/final/. Call this once the customer confirms the design."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Current session id.",
            },
            "format": {
                "type": "string",
                "description": "Output format. 'pdf' (default) writes both .md and .pdf; 'md' writes only .md.",
                "enum": ["pdf", "md"],
                "default": "pdf",
            },
        },
        "required": ["session_id"],
    },
    handler=export_report,
)


__all__ = ["export_report", "_SPEC"]