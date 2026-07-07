"""Quick kb_search smoke test for P8 verification."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import kb_search


async def main() -> int:
    out_path = Path(__file__).parent / "_kb_search_test_output.txt"
    queries = ["知识库 选型", "ERP 实施", "数字化趋势", "方案撰写"]
    lines = []
    for q in queries:
        r = await kb_search(q, top_k=3)
        try:
            data = json.loads(r)
            lines.append(f"=== Query: {q} ===")
            lines.append(f"Result count: {len(data)}")
            for i, item in enumerate(data, 1):
                if "error" in item:
                    lines.append(f"  [{i}] ERROR: {item['error']}")
                else:
                    src = item.get("source", "?")
                    score = item.get("score", 0.0)
                    chunk = item.get("chunk", "")[:80]
                    lines.append(f"  [{i}] {src}  score={score:.3f}")
                    lines.append(f"      {chunk}...")
            lines.append("")
        except Exception as e:
            lines.append(f"=== Query: {q} ===\n  parse error: {e}\n  raw: {r[:200]}\n")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
