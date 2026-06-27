from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import FileSafetyError


UTF8_BOM = b"\xef\xbb\xbf"
BINARY_SAMPLE_BYTES = 4096


@dataclass(slots=True)
class TextSnapshot:
    path: Path
    raw: bytes
    text: str
    encoding: str
    bom: bool
    newline: str
    has_final_newline: bool


def is_probably_binary(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    if not data:
        return False
    control = sum(1 for b in data[:4096] if b < 9 or (13 < b < 32))
    return control / min(len(data), 4096) > 0.30


def binary_sample(path: Path, max_bytes: int = BINARY_SAMPLE_BYTES) -> bytes:
    with path.open("rb") as handle:
        return handle.read(max_bytes)


def is_probably_binary_file(path: Path) -> bool:
    return is_probably_binary(binary_sample(path))


def read_limited_text_bytes(path: Path, max_chars: int) -> bytes:
    max_bytes = max_chars * 4
    size = path.stat().st_size
    if size <= max_bytes:
        return path.read_bytes()
    half = max_bytes // 2
    with path.open("rb") as handle:
        head = handle.read(half)
        handle.seek(max(size - half, 0))
        tail = handle.read(half)
    return head + b"\n...[truncated]...\n" + tail


def read_text_snapshot(path: Path) -> TextSnapshot:
    raw = path.read_bytes() if path.exists() else b""
    if is_probably_binary(raw):
        raise FileSafetyError(f"binary file cannot be edited: {path}")
    bom = raw.startswith(UTF8_BOM)
    body = raw[len(UTF8_BOM) :] if bom else raw
    encoding = "utf-8-sig" if bom else "utf-8"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileSafetyError(f"unsupported text encoding for {path}: {exc}") from exc
    newline = detect_newline(text)
    has_final_newline = text.endswith(("\r\n", "\n", "\r"))
    return TextSnapshot(path=path, raw=raw, text=text, encoding=encoding, bom=bom, newline=newline, has_final_newline=has_final_newline)


def detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    tmp = text.replace("\r\n", "")
    lf = tmp.count("\n")
    cr = tmp.count("\r")
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if lf > 0:
        return "\n"
    if cr > 0:
        return "\r"
    return "\n"


def encode_like(snapshot: TextSnapshot, text: str) -> bytes:
    body = text.encode("utf-8")
    return (UTF8_BOM if snapshot.bom else b"") + body
