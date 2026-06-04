#!/usr/bin/env python3
"""
Run MDKeyChunker on a screen design spec (.md) and emit a human review report.

Maps each chunk to SCR-* screen IDs mentioned in the chunk (simulated search index).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mdkeychunker.chunker import MarkdownChunker
from mdkeychunker.config import Config
from mdkeychunker.models import Chunk

SCR_RE = re.compile(r"SCR-\d{3}")
ROW_SCR_RE = re.compile(r"^\|\s*(SCR-\d{3})\s*\|\s*([^|]+)\s*\|", re.MULTILINE)


def load_spec_screen_index(spec_path: Path) -> dict[str, str]:
    """screen_id -> screen name from §1.4 화면 목록 only."""
    text = spec_path.read_text(encoding="utf-8")
    section = text
    m = re.search(r"### 1\.4 화면 목록\s*\n+(.*?)\n+---", text, re.DOTALL)
    if m:
        section = m.group(1)
    index: dict[str, str] = {}
    for match in ROW_SCR_RE.finditer(section):
        sid, name = match.group(1), match.group(2).strip()
        if sid == "SCR-001" and name == "화면 ID":
            continue  # table header row
        if name.upper() in ("GET", "POST"):
            continue
        index[sid] = name
    return index


def scr_in_chunk(text: str) -> list[str]:
    return sorted(set(SCR_RE.findall(text)))


def preview(text: str, max_len: int = 280) -> str:
    one = " ".join(text.split())
    if len(one) <= max_len:
        return one
    return one[: max_len - 3] + "..."


def finalize_chunks(chunks: list[Chunk]) -> None:
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")

        def count_tokens(text: str) -> int:
            return len(enc.encode(text))
    except Exception:

        def count_tokens(text: str) -> int:
            return len(text) // 4

    for i, c in enumerate(chunks):
        c.position_index = i
        c.generate_id()
        c.token_count = count_tokens(c.text)

    for i in range(len(chunks)):
        chunks[i].previous_chunk_id = chunks[i - 1].chunk_id if i > 0 else ""
        chunks[i].next_chunk_id = chunks[i + 1].chunk_id if i < len(chunks) - 1 else ""


def chunk_spec(spec_path: Path, config: Config) -> list[Chunk]:
    text = spec_path.read_text(encoding="utf-8")
    chunks = MarkdownChunker(config).chunk(text)
    finalize_chunks(chunks)
    return chunks


def save_jsonl(chunks: list[Chunk], path: Path) -> None:
    path.write_text("\n".join(c.to_json() for c in chunks), encoding="utf-8")


def build_report(
    spec_path: Path,
    chunks: list,
    chunk_only: bool,
) -> str:
    screen_index = load_spec_screen_index(spec_path)
    lines = [
        "# 화면설계서 처리 결과 — 리뷰 리포트",
        "",
        f"- **입력:** `{spec_path}`",
        f"- **모드:** {'구조 청킹만 (--chunk-only)' if chunk_only else '전체 파이프라인 (LLM enrich)'}",
        f"- **청크 수:** {len(chunks)}",
        "",
        "## 1. 화면 목록 (설계서 §1.4 기준)",
        "",
        "| SCR | 화면명 | 이번 청킹 결과에 포함된 청크 # |",
        "|-----|--------|------------------------------|",
    ]

    scr_to_positions: dict[str, list[int]] = {sid: [] for sid in screen_index}
    for i, ch in enumerate(chunks):
        for sid in scr_in_chunk(ch.text):
            if sid in scr_to_positions:
                scr_to_positions[sid].append(i + 1)
            else:
                scr_to_positions.setdefault(sid, []).append(i + 1)

    for sid, name in sorted(screen_index.items()):
        pos = scr_to_positions.get(sid) or []
        pos_str = ", ".join(str(p) for p in pos) if pos else "— (미검출)"
        lines.append(f"| {sid} | {name} | {pos_str} |")

    lines.extend(
        [
            "",
            "## 2. 청크별 상세 (검색·RAG 시 노출 단위 가정)",
            "",
            "각 청크가 SCR-003 결과 카드 / SCR-004 본문 일부로 매핑된다고 가정하고 검토합니다.",
            "",
        ]
    )

    for i, ch in enumerate(chunks):
        scrs = scr_in_chunk(ch.text)
        scr_label = ", ".join(scrs) if scrs else "(화면 ID 없음 — 공통/개요 구간)"
        lines.append(f"### 청크 {i + 1} · `{ch.chunk_id}`")
        lines.append("")
        lines.append(f"| 항목 | 값 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 줄 범위 | {ch.start_line}–{ch.end_line} |")
        lines.append(f"| 섹션 경로 | {ch.section_title or '(root)'} |")
        lines.append(f"| 토큰(추정) | {ch.token_count} |")
        lines.append(f"| 연관 SCR | {scr_label} |")
        if ch.title:
            lines.append(f"| LLM title | {ch.title} |")
        if ch.summary:
            lines.append(f"| LLM summary | {ch.summary} |")
        if ch.key:
            lines.append(f"| semantic key | `{ch.key}` |")
        if ch.keywords:
            lines.append(f"| keywords | {', '.join(ch.keywords[:8])} |")
        lines.append("")
        lines.append("**본문 미리보기**")
        lines.append("")
        lines.append("```")
        lines.append(preview(ch.text, 500))
        lines.append("```")
        lines.append("")
        lines.append("**리뷰 체크 (P/F/메모)**")
        lines.append("")
        lines.append("- [ ] 설계서 해당 화면의 UI·이벤트가 이 청크에 빠짐없이 들어가 있는가")
        lines.append("- [ ] 코드블록·표·mermaid가 청크 경계에서 깨지지 않았는가")
        lines.append("- [ ] SCR-003 검색 시 이 스니펫이 의미 있게 보일 것 같은가")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.extend(
        [
            "## 3. 설계 대비 누락·이상 징후",
            "",
        ]
    )

    missing = [sid for sid in screen_index if not scr_to_positions.get(sid)]
    if missing:
        lines.append(
            "- **SCR ID가 청크 본문에서 한 번도 안 잡힌 화면:** "
            + ", ".join(missing)
            + " — 상세 절 제목만 쓰고 본문에 ID를 안 넣었을 수 있음."
        )
    else:
        lines.append("- §1.4 목록의 모든 SCR ID가 최소 1개 청크에서 검출됨.")

    orphan = [
        i + 1
        for i, ch in enumerate(chunks)
        if not scr_in_chunk(ch.text) and ch.section_title
    ]
    if orphan:
        lines.append(
            f"- **화면 ID 없는 청크 번호:** {', '.join(map(str, orphan))} — 개요·공통 UI·API 표 등."
        )

    lines.extend(
        [
            "",
            "## 4. 다음 단계",
            "",
            "1. 위 청크별 **P/F/메모**를 채운 뒤 설계서 수정 반영",
            "2. LLM enrich가 필요하면: `mdkeychunker docs/sample-screen-spec.md` (API 키 필요)",
            "3. 시나리오 S-01/S-02는 **검색 hit**를 이 JSONL + 고유 키워드로 검증",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build review report from screen spec chunks")
    ap.add_argument(
        "spec",
        nargs="?",
        default="docs/sample-screen-spec.md",
        help="Screen design markdown",
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        default="docs/output",
        help="Directory for jsonl + REVIEW report",
    )
    ap.add_argument(
        "--chunk-only",
        action="store_true",
        default=True,
        help="Structural chunking only (default: true)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Run full LLM pipeline (overrides --chunk-only)",
    )
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"Not found: {spec_path}", file=sys.stderr)
        return 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    chunk_only = not args.full
    config = Config.from_env()
    if chunk_only:
        chunks = chunk_spec(spec_path, config)
    else:
        from mdkeychunker.pipeline import Pipeline

        chunks = Pipeline(config).process_file(str(spec_path), chunk_only=False)

    stem = spec_path.stem
    jsonl_path = out_dir / f"{stem}.jsonl"
    report_path = out_dir / f"{stem}-REVIEW.md"

    save_jsonl(chunks, jsonl_path)
    report_path.write_text(
        build_report(spec_path, chunks, chunk_only), encoding="utf-8"
    )

    print(f"✓ {len(chunks)} chunks → {jsonl_path}")
    print(f"✓ Review report → {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
