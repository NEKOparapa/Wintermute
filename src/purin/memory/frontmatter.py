"""A deliberately small, strict frontmatter format using only the standard library."""

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Literal


@dataclass(frozen=True)
class MemoryMetadata:
    title: str
    summary: str
    updated_at: date
    tags: tuple[str, ...] = ()
    status: Literal["active", "archived"] = "active"
    review_at: date | None = None
    expires_at: date | None = None
    sources: tuple[str, ...] = ()

    def expired(self, today: date) -> bool:
        return self.expires_at is not None and self.expires_at < today


def split_frontmatter(text: str) -> tuple[str | None, str]:
    normalized = text.removeprefix("\ufeff").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return None, text
    end = re.search(r"(?m)^---\s*$", normalized[4:])
    if end is None:
        raise ValueError("Frontmatter has no closing delimiter")
    return normalized[4 : 4 + end.start()], normalized[4 + end.end() :].lstrip("\n")


def _scalar(value: str) -> str:
    if value.startswith('"'):
        parsed: object = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("Expected a frontmatter string")
        value = parsed
    elif value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise ValueError("Unclosed frontmatter string")
        value = value[1:-1].replace("''", "'")
    elif any(token in value for token in ("{", "}", "[", "]")) or value in {"|", ">"}:
        raise ValueError("Complex YAML values are unsupported")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Frontmatter strings must be single-line")
    return value


def _list(value: str) -> tuple[str, ...]:
    if not value.startswith("[") or not value.endswith("]"):
        raise ValueError("Frontmatter lists must use [a, b] syntax")
    if not value[1:-1].strip():
        return ()
    if value[1:-1].lstrip().startswith('"'):
        parsed: object = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Frontmatter lists must contain strings")
        return tuple(_scalar(json.dumps(item)) for item in parsed)
    return tuple(
        _scalar(item.strip()) for item in next(csv.reader([value[1:-1]], skipinitialspace=True))
    )


def parse_frontmatter(text: str, *, path: str, modified: date) -> MemoryMetadata:
    header, body = split_frontmatter(text)
    heading = re.search(r"(?m)^#\s+(.+)$", body)
    title = heading[1].strip() if heading else PurePosixPath(path).stem
    prose = re.sub(r"(?m)^#{1,6}\s+.*(?:\n|$)", "\n", body)
    paragraphs = re.split(r"\n\s*\n", prose.strip())
    summary = next(
        (
            " ".join(part.split())
            for part in paragraphs
            if part.strip() and not part.lstrip().startswith("#")
        ),
        "",
    )[:120]
    values: dict[str, str] = {}
    if header is not None:
        allowed = {
            "title",
            "summary",
            "tags",
            "status",
            "updated_at",
            "review_at",
            "expires_at",
            "sources",
        }
        for line in header.splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in allowed or key in values:
                raise ValueError("Invalid, unknown or duplicate frontmatter field")
            values[key] = value.strip()
    status = _scalar(values.get("status", "active"))
    if status not in {"active", "archived"}:
        raise ValueError("Invalid frontmatter status")
    dates: dict[str, date] = {}
    for key, value in values.items():
        if key in {"updated_at", "review_at", "expires_at"}:
            value = _scalar(value)
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Frontmatter dates must use YYYY-MM-DD")
            dates[key] = date.fromisoformat(value)
    return MemoryMetadata(
        title=_scalar(values["title"]) if "title" in values else title,
        summary=_scalar(values["summary"]) if "summary" in values else summary,
        updated_at=dates.get("updated_at", modified),
        status="archived" if status == "archived" else "active",
        tags=_list(values.get("tags", "[]")),
        sources=_list(values.get("sources", "[]")),
        review_at=dates.get("review_at"),
        expires_at=dates.get("expires_at"),
    )


def serialize_frontmatter(metadata: MemoryMetadata, body: str) -> str:
    values = [
        f"title: {json.dumps(metadata.title, ensure_ascii=False)}",
        f"summary: {json.dumps(metadata.summary, ensure_ascii=False)}",
        f"tags: {json.dumps(metadata.tags, ensure_ascii=False)}",
        f"status: {metadata.status}",
        f"updated_at: {metadata.updated_at.isoformat()}",
    ]
    for key in ("review_at", "expires_at"):
        value = getattr(metadata, key)
        if value is not None:
            values.append(f"{key}: {value.isoformat()}")
    values.append(f"sources: {json.dumps(metadata.sources, ensure_ascii=False)}")
    return "---\n" + "\n".join(values) + "\n---\n\n" + body.lstrip("\n")
