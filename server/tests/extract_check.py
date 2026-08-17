"""文件抽取的对抗性自测：正常 PDF、扫描件、加密、损坏、GBK 编码、docx、超大文本。

要求是任何一种输入都不许让服务崩掉，能读的读出来，读不了的给一句用户看得懂的话。
样例文件现造现删，不入库。

    .venv/bin/python -m server.tests.extract_check
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import docx

from .. import extract

SAMPLE = """Annex 7 - Export Control Compliance Review

7.1 The Company shall designate a domestic compliance review officer responsible for
screening all outbound technical data transfers against the Commerce Control List.

7.2 The officer shall maintain a written record of each screening decision for five years,
including the ECCN determination and the license exception relied upon, if any.

7.3 No deemed export shall occur without prior written clearance from the officer.
"""


def build_fixtures(tmp: Path) -> list[tuple[str, Path, str]]:
    """返回 (说明, 路径, 期望结果) 三元组。期望结果是 ok 或 reject。"""
    cases: list[tuple[str, Path, str]] = []

    txt = tmp / "plain.txt"
    txt.write_text(SAMPLE, encoding="utf-8")
    cases.append(("普通文本", txt, "ok"))

    gbk = tmp / "gbk.txt"
    gbk.write_bytes("第七条 出口管制合规审查专员应当保存每一次筛查决定的书面记录，"
                    "保存期限五年，内容包括分类编码判定与所依据的许可例外。"
                    .encode("gb18030"))
    cases.append(("GBK 编码文本", gbk, "ok"))

    docx_path = tmp / "sample.docx"
    document = docx.Document()
    for line in SAMPLE.splitlines():
        document.add_paragraph(line)
    document.save(docx_path)
    cases.append(("docx", docx_path, "ok"))

    # macOS 自带 cupsfilter 能把 txt 转成真的 PDF，没有就跳过这一类
    pdf = tmp / "real.pdf"
    out = subprocess.run(["cupsfilter", str(txt)], capture_output=True)
    if out.stdout:
        pdf.write_bytes(out.stdout)
        cases.append(("文字版 PDF", pdf, "ok"))

    broken = tmp / "broken.pdf"
    broken.write_text("not really a pdf at all", encoding="utf-8")
    cases.append(("损坏的 PDF", broken, "reject"))

    if pdf.exists():
        try:
            from pypdf import PdfReader, PdfWriter
            writer = PdfWriter()
            writer.append(PdfReader(str(pdf)))
            writer.encrypt("secret")
            locked = tmp / "locked.pdf"
            writer.write(str(locked))
            cases.append(("加密 PDF", locked, "reject"))
        except Exception:
            pass

    huge = tmp / "huge.txt"
    huge.write_text("合规审查记录保存五年。" * 100_000, encoding="utf-8")
    cases.append(("百万字文本", huge, "ok"))

    return cases


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="mi-extract-") as tmp_name:
        tmp = Path(tmp_name)
        for label, path, expect in build_fixtures(tmp):
            started = time.time()
            try:
                text, method = extract.extract(path)
                got = "ok" if text.strip() else "reject"
                detail = f"方法 {method}，{len(text.strip()):,} 字"
            except extract.ExtractError as exc:
                got, detail = "reject", f"明确拒绝：{exc}"[:60]
            except Exception as exc:  # 崩溃就是不合格
                got, detail = "crash", f"{type(exc).__name__}: {exc}"[:80]
            ok = got == expect
            failures += 0 if ok else 1
            print(f"[{'通过' if ok else '不通过'}] {label:14s} {detail}  {time.time()-started:.1f}s")
    print("\n结果：" + ("全部通过" if not failures else f"{failures} 项不通过"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
