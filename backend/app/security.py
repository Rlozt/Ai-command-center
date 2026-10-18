"""Security helpers: secret redaction, safe path handling, SSRF-guarded URL checks."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"pplx-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
]
_extra_secrets: set[str] = set()


def register_secrets(values: list[str]) -> None:
    for v in values:
        if v and len(v) >= 8:
            _extra_secrets.add(v)


def redact(text: str) -> str:
    """Remove anything that looks like an API key or a registered secret."""
    if not text:
        return text
    for s in _extra_secrets:
        text = text.replace(s, "[REDACTED]")
    for pat in _KEY_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


class PathError(ValueError):
    """Raised when a path is outside a permitted directory or otherwise unsafe."""


_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Files the agent system must never read or write, even inside a permitted directory.
_DENY_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", ".netrc"}
_DENY_SUFFIXES = (".pem", ".key", ".pfx", ".p12")
_DENY_DIRS = {".git", ".ssh", ".acc-backups"}


def validate_relative(rel: str) -> Path:
    """Validate an agent-supplied relative path. Returns a clean relative Path or raises PathError."""
    if not isinstance(rel, str) or not rel.strip():
        raise PathError("empty path")
    if "\x00" in rel:
        raise PathError("null byte in path")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or norm.startswith("//"):
        raise PathError(f"absolute paths are not allowed: {rel!r}")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        raise PathError("empty path")
    if any(p == ".." for p in parts):
        raise PathError(f"path traversal is not allowed: {rel!r}")
    for p in parts:
        stem = p.split(".")[0].lower()
        if stem in _WIN_RESERVED:
            raise PathError(f"reserved name in path: {p!r}")
        if p.endswith((" ", ".")) or any(c in p for c in '<>:"|?*'):
            raise PathError(f"unsupported characters in path segment: {p!r}")
    if parts[0].lower() in _DENY_DIRS or any(p.lower() in _DENY_DIRS for p in parts):
        raise PathError(f"protected directory in path: {rel!r}")
    name = parts[-1].lower()
    if name in _DENY_NAMES or name.startswith(".env") or name.endswith(_DENY_SUFFIXES):
        raise PathError(f"protected file name: {rel!r}")
    return Path(*parts)


def safe_join(root: Path, rel: str) -> Path:
    """Join `rel` onto `root` and prove the resolved result stays inside `root` (symlink-safe)."""
    clean = validate_relative(rel)
    root_resolved = root.resolve()
    candidate = (root_resolved / clean).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise PathError(f"path escapes the permitted directory: {rel!r}") from exc
    return candidate


def is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable: treat as unsafe
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return True
    return False


def check_public_http_url(url: str) -> str:
    """Return the URL if it is http(s) to a public host; raise ValueError otherwise (SSRF guard)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only http(s) URLs are allowed")
    if p.username or p.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    if is_private_host(p.hostname):
        raise ValueError("URL resolves to a private or local address")
    return url
