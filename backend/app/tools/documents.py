"""Text/document extraction: txt, md, csv, json, html, pdf (pypdf), docx (python-docx)."""
from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from .base import ToolError

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv", ".tsv", ".yaml", ".yml",
                 ".toml", ".ini", ".cfg", ".html", ".htm", ".css", ".xml", ".sh", ".ps1", ".bat", ".java", ".c", ".h", ".cpp", ".go",
                 ".rs", ".sql", ".log", ".gitignore", ""}
MAX_READ_BYTES = 1_000_000
MAX_TEXT_CHARS = 200_000


class _Strip(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Strip()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(p.parts))


def extract_text(path: Path, data: bytes | None = None) -> tuple[str, str]:
    """Return (text, format). Raises ToolError for unsupported or oversized files."""
    suffix = path.suffix.lower()
    if data is None:
        size = path.stat().st_size
        if size > MAX_READ_BYTES * 20:
            raise ToolError("too_large", f"{path.name} is {size} bytes; limit is {MAX_READ_BYTES * 20}")
        data = path.read_bytes()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise ToolError("missing_dependency", "pypdf is not installed") from e
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages)
        return text[:MAX_TEXT_CHARS], "pdf"
    if suffix == ".docx":
        try:
            import docx
        except ImportError as e:  # pragma: no cover
            raise ToolError("missing_dependency", "python-docx is not installed") from e
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)[:MAX_TEXT_CHARS], "docx"
    if suffix in (".html", ".htm"):
        return html_to_text(data.decode("utf-8", errors="replace"))[:MAX_TEXT_CHARS], "html"
    if suffix in TEXT_SUFFIXES:
        if b"\x00" in data[:4096]:
            raise ToolError("binary_file", f"{path.name} looks binary")
        text = data.decode("utf-8", errors="replace")
        if suffix in (".csv", ".tsv"):
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if suffix == ".tsv" else ","))
            return f"{len(rows)} rows\n" + text[:MAX_TEXT_CHARS], "csv"
        if suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError:
                pass
        return text[:MAX_TEXT_CHARS], suffix.lstrip(".") or "text"
    raise ToolError("unsupported_format", f"unsupported file type: {suffix}")
