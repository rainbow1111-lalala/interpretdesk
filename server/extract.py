"""把上传的文件抽成纯文本。

原则是宁可慢一点也要抽出来，并且任何一份文件出问题都不许影响其他文件，更不许让服务崩掉。

PDF 走三级降级：先 pypdf（快，纯 Python），文字太少就换 pdftotext（poppler 的版面处理更好），
还是太少就当扫描件走 OCR（pdftoppm 渲染成图，tesseract 认字，中英一起认）。外部工具没装就跳过
该级，并在返回的说明里写清楚，不抛异常。

txt 一类文本按 utf-8、gb18030、utf-16 依次试，国内来的文件常是 GBK 系编码。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# pypdf 遇到不规范的 PDF 会往上抛一堆告警，我们已经有逐级降级，不需要这些噪音
logging.getLogger("pypdf").setLevel(logging.ERROR)

# 少于这个字数就认为这一级没抽出东西，换下一级
MIN_USEFUL_CHARS = 120
# OCR 很慢，超过这个页数只认前面这些页，够会前底稿用
OCR_MAX_PAGES = 40
OCR_LANGS = "chi_sim+eng"
TEXT_ENCODINGS = ("utf-8", "gb18030", "utf-16")


class ExtractError(Exception):
    """抽不出内容且原因明确时抛，消息直接给用户看。"""


def _tool(name: str) -> str | None:
    return shutil.which(name)


def extract(path: Path) -> tuple[str, str]:
    """返回（正文, 用了哪种方法）。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".docx":
        return _docx(path), "docx"
    if suffix == ".doc":
        return _legacy_doc(path), "doc"
    return _plain(path), "text"


def _plain(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    blocks = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            blocks.append("\t".join(c.text for c in row.cells))
    return "\n".join(blocks)


def _legacy_doc(path: Path) -> str:
    """老式 .doc。macOS 自带 textutil 能转，没有就明说，不假装能读。"""
    if not _tool("textutil"):
        raise ExtractError("这是老式 .doc 文件，需要先另存为 .docx 再上传")
    out = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)],
                         capture_output=True, timeout=120)
    if out.returncode != 0:
        raise ExtractError("老式 .doc 转换失败，请另存为 .docx 再上传")
    return out.stdout.decode("utf-8", errors="replace")


def _pdf(path: Path) -> tuple[str, str]:
    text = _pdf_pypdf(path)
    if len(text.strip()) >= MIN_USEFUL_CHARS:
        return text, "pypdf"

    text2 = _pdf_poppler(path)
    if len(text2.strip()) >= MIN_USEFUL_CHARS:
        return text2, "pdftotext"

    text3, note = _pdf_ocr(path)
    if len(text3.strip()) >= MIN_USEFUL_CHARS:
        return text3, note

    best = max((text, text2, text3), key=lambda t: len(t.strip()))
    if best.strip():
        return best, "低置信"
    raise ExtractError(
        "这份 PDF 抽不出文字。若是扫描件，本机 OCR 也没认出内容，"
        "可能是图像太糊或页面是空白；若有密码保护，请先去掉密码再上传")


def _pdf_pypdf(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        log.info("pypdf 打不开 %s：%s", path.name, exc)
        return ""
    if reader.is_encrypted:
        try:
            # 只有空密码这一种能自动处理，其余交给用户
            if reader.decrypt("") == 0:
                raise ExtractError("这份 PDF 有密码保护，请先去掉密码再上传")
        except ExtractError:
            raise
        except Exception:
            raise ExtractError("这份 PDF 有密码保护，请先去掉密码再上传")

    pages, failed = [], 0
    for page in reader.pages:
        # 单页解析失败不能拖垮整份文件
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            failed += 1
    if failed:
        log.info("%s 有 %d 页 pypdf 解析失败，已跳过", path.name, failed)
    return "\n".join(pages)


def _pdf_poppler(path: Path) -> str:
    if not _tool("pdftotext"):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                             capture_output=True, timeout=300)
        return out.stdout.decode("utf-8", errors="replace")
    except Exception as exc:
        log.info("pdftotext 失败 %s：%s", path.name, exc)
        return ""


def _pdf_ocr(path: Path) -> tuple[str, str]:
    """扫描件兜底。渲染成图再认字，慢但能救回一份原本读不了的文件。"""
    if not (_tool("pdftoppm") and _tool("tesseract")):
        return "", "缺 OCR 工具"
    with tempfile.TemporaryDirectory(prefix="mi-ocr-") as tmp:
        stem = Path(tmp) / "page"
        try:
            subprocess.run(["pdftoppm", "-r", "200", "-png",
                            "-f", "1", "-l", str(OCR_MAX_PAGES), str(path), str(stem)],
                           capture_output=True, timeout=900, check=True)
        except Exception as exc:
            log.info("pdftoppm 渲染失败 %s：%s", path.name, exc)
            return "", "渲染失败"
        images = sorted(Path(tmp).glob("page*.png"))
        if not images:
            return "", "没渲染出页面"
        log.info("对 %s 走 OCR，共 %d 页", path.name, len(images))
        texts = []
        for image in images:
            try:
                out = subprocess.run(
                    ["tesseract", str(image), "stdout", "-l", OCR_LANGS],
                    capture_output=True, timeout=300)
                texts.append(out.stdout.decode("utf-8", errors="replace"))
            except Exception:
                continue
    return "\n".join(texts), f"OCR {len(images)} 页"
