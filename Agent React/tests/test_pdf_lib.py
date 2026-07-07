"""P0 PDF 库决策测试：WeasyPrint vs fpdf2。

跑法: python tests/test_pdf_lib.py
判定: 第一个跑通的库即 spec §4.5 的决策
"""
import sys
import tempfile
from pathlib import Path

CANDIDATES = ["weasyprint", "fpdf2"]


def try_weasyprint() -> tuple[bool, str]:
    try:
        from weasyprint import HTML
    except Exception as e:
        return False, f"import failed: {type(e).__name__}: {e}"

    try:
        html = "<html><body><h1>PDF lib test</h1><p>WeasyPrint works.</p></body></html>"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            out = f.name
        HTML(string=html).write_pdf(out)
        size = Path(out).stat().st_size
        return True, f"OK {size} bytes -> {out}"
    except Exception as e:
        return False, f"write failed: {type(e).__name__}: {e}"


def try_fpdf2() -> tuple[bool, str]:
    try:
        from fpdf import FPDF
    except Exception as e:
        return False, f"import failed: {type(e).__name__}: {e}"

    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=14)
        pdf.cell(text="PDF lib test - fpdf2 works.")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            out = f.name
        pdf.output(out)
        size = Path(out).stat().st_size
        return True, f"OK {size} bytes -> {out}"
    except Exception as e:
        return False, f"write failed: {type(e).__name__}: {e}"


TESTERS = {"weasyprint": try_weasyprint, "fpdf2": try_fpdf2}


def main() -> int:
    print("=" * 60)
    print("P0 PDF library decision test")
    print("=" * 60)
    results: dict[str, tuple[bool, str]] = {}
    for name in CANDIDATES:
        ok, msg = TESTERS[name]()
        results[name] = (ok, msg)
        status = "OK  " if ok else "FAIL"
        print(f"  [{status}] {name:12s}  {msg}")

    print()
    decided = next((n for n, (ok, _) in results.items() if ok), None)
    if decided:
        print(f">>> DECISION: use {decided}")
        # 写决策到 deepwork 文件便于后续阶段读取
        deepwork = Path(__file__).resolve().parent.parent / ".slim" / "deepwork" / "solution-research-agent.md"
        if deepwork.exists():
            text = deepwork.read_text(encoding="utf-8")
            marker = "**PDF 库决策**："
            if marker in text:
                # 替换旧决策
                import re
                text = re.sub(r"\*\*PDF 库决策\*\*：.*", f"**PDF 库决策**：{decided}", text)
            else:
                # 插入新决策到关键决策段
                anchor = "## Key Tech Decisions"
                if anchor in text:
                    text = text.replace(anchor, f"{marker}{decided}\n\n{anchor}")
            deepwork.write_text(text, encoding="utf-8")
            print(f">>> wrote decision to {deepwork}")
        return 0
    else:
        print(">>> DECISION: NONE - both libs failed; install one manually")
        return 1


if __name__ == "__main__":
    sys.exit(main())
