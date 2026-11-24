from __future__ import annotations

import re
from dataclasses import dataclass

from finengine.assistant.doc_pages import pages_of
from finengine.config import settings
from finengine.ingestion.pdf_parser import is_financial_statement_page

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


@dataclass(frozen=True)
class SearchChunk:
    document_id: str
    chunk_id: str
    content: str
    page: int
    section_path: str
    chunk_type: str


@dataclass(frozen=True)
class _Block:
    content: str
    section_path: str
    kind: str


def _pipe_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _pipe_row(lines[index])
        and bool(_TABLE_SEPARATOR.match(lines[index + 1]))
    )


def _section(headings: tuple[str, ...]) -> str:
    return " > ".join(headings)


def _flush_prose(lines: list[str], headings: tuple[str, ...], blocks: list[_Block]) -> None:
    content = "\n".join(lines).strip()
    if content:
        blocks.append(_Block(content, _section(headings), "prose"))
    lines.clear()


def _take_caption(lines: list[str]) -> str:
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or _HEADING.match(lines[-1]):
        return ""
    caption = lines.pop().strip()
    while lines and not lines[-1].strip():
        lines.pop()
    return caption


def _page_blocks(
    text: str,
    headings: tuple[str, ...],
) -> tuple[list[_Block], tuple[str, ...]]:
    lines = text.splitlines()
    blocks: list[_Block] = []
    prose: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _HEADING.match(line)
        if heading:
            _flush_prose(prose, headings, blocks)
            level = len(heading.group(1))
            headings = (*headings[: level - 1], heading.group(2).strip())
            index += 1
            continue
        if _table_start(lines, index):
            caption = _take_caption(prose)
            _flush_prose(prose, headings, blocks)
            table = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and _pipe_row(lines[index]):
                table.append(lines[index])
                index += 1
            content = "\n".join(table)
            if caption:
                content = f"{content}\n\n{caption}"
            blocks.append(_Block(content, _section(headings), "table"))
            continue
        prose.append(line)
        index += 1
    _flush_prose(prose, headings, blocks)
    return blocks, headings


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _without_repeated_prose(blocks: list[_Block], seen: set[str]) -> list[_Block]:
    result: list[_Block] = []
    for block in blocks:
        if block.kind != "prose":
            result.append(block)
            continue
        paragraphs = re.split(r"\n\s*\n", block.content)
        unique = []
        for paragraph in paragraphs:
            key = _normalized(paragraph)
            if key and key not in seen:
                seen.add(key)
                unique.append(paragraph.strip())
        content = "\n\n".join(unique).strip()
        if content:
            result.append(_Block(content, block.section_path, block.kind))
    return result


def _hard_windows(text: str) -> list[str]:
    step = settings.chunk_size - settings.chunk_overlap
    return [
        text[start : start + settings.chunk_size].strip() for start in range(0, len(text), step)
    ]


def _prose_chunks(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if len(normalized) <= settings.chunk_size:
        return [normalized] if normalized else []
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(normalized):
        if len(sentence) > settings.chunk_size:
            if current:
                chunks.append(current)
                current = ""
            pieces = _hard_windows(sentence)
            chunks.extend(pieces[:-1])
            current = pieces[-1]
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= settings.chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        overlap = current[-settings.chunk_overlap :]
        candidate = f"{overlap} {sentence}".strip()
        current = candidate if len(candidate) <= settings.chunk_size else sentence
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def chunk_document(
    document_id: str, text: str, *, require_page_marks: bool = False
) -> list[SearchChunk]:
    pieces: list[tuple[str, int, str, str]] = []
    repeated_prose: set[str] = set()
    headings: tuple[str, ...] = ()
    for page in pages_of(text, unmarked_page=0 if require_page_marks else 1):
        blocks, headings = _page_blocks(page.text, headings)
        if not any(block.kind == "table" for block in blocks) and is_financial_statement_page(
            page.text
        ):
            content = page.text.strip()
            if content:
                pieces.append((content, page.number, _section(headings), "financial_page"))
            continue
        for block in _without_repeated_prose(blocks, repeated_prose):
            contents = _prose_chunks(block.content) if block.kind == "prose" else [block.content]
            pieces.extend(
                (content, page.number, block.section_path, block.kind) for content in contents
            )
    return [
        SearchChunk(
            document_id=document_id,
            chunk_id=f"{document_id}:chunk:{index:05d}",
            content=content,
            page=page,
            section_path=section_path,
            chunk_type=kind,
        )
        for index, (content, page, section_path, kind) in enumerate(pieces)
    ]
