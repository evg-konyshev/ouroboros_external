from pathlib import Path

import pytest

from finengine.search.chunker import chunk_document

_FIXTURE = Path(__file__).parent / "fixtures" / "search" / "table_page.txt"


def test_prose_uses_bounded_overlapping_windows_without_page_markers() -> None:
    text = "===PAGE 2===\n" + " ".join(
        f"Предложение номер {index} содержит пояснение." for index in range(70)
    )

    chunks = chunk_document("doc", text)

    assert len(chunks) > 1
    assert all(chunk.page == 2 for chunk in chunks)
    assert all(len(chunk.content) <= 800 for chunk in chunks)
    assert all("===PAGE" not in chunk.content for chunk in chunks)
    assert chunks[0].content[-50:] in chunks[1].content


def test_no_prose_chunk_crosses_a_markdown_section() -> None:
    text = "===PAGE 1===\n# Риски\nОписание первого раздела.\n## Ликвидность\nОписание второго раздела."

    chunks = chunk_document("doc", text)

    assert [chunk.section_path for chunk in chunks] == ["Риски", "Риски > Ликвидность"]
    assert all("Ликвидность" not in chunks[0].content for chunk in chunks[:1])
    assert all("#" not in chunk.content for chunk in chunks)


def test_repeated_paragraph_on_distant_pages_is_emitted_once() -> None:
    paragraph = "Компания раскрывает существенный риск изменения процентной ставки."
    text = f"===PAGE 3===\n{paragraph}\n\n===PAGE 40===\n{paragraph}"

    chunks = chunk_document("doc", text)

    assert len(chunks) == 1
    assert chunks[0].page == 3


def test_financial_statement_page_without_tables_stays_whole() -> None:
    rows = "\n".join(f"Статья актива {index} {index * 1000}" for index in range(90))
    text = f"===PAGE 8===\nОтчет о финансовом положении\n{rows}"

    chunks = chunk_document("doc", text)

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "financial_page"
    assert chunks[0].content.endswith(rows.splitlines()[-1])


@pytest.mark.parametrize("page_mark", ["===PAGE 9===\n", ""])
def test_large_markdown_table_is_one_chunk_starting_with_its_header(page_mark: str) -> None:
    header = "| Показатель | 2024 | 2023 |"
    rows = "\n".join(
        f"| Статья основных средств {index} | {index} | {index + 1} |" for index in range(300)
    )
    text = f"{page_mark}Таблица 9. Основные средства\n{header}\n|---|---:|---:|\n{rows}"

    chunks = chunk_document("doc", text)

    assert len(chunks) == 1
    assert chunks[0].chunk_type == "table"
    assert chunks[0].content.startswith(header)
    assert rows in chunks[0].content
    assert "Таблица 9. Основные средства" in chunks[0].content


def test_table_inside_prose_is_a_separate_chunk_and_pipes_are_not_enough() -> None:
    text = (
        _FIXTURE.read_text(encoding="utf-8") + "\nОператор | в обычной прозе не образует таблицу."
    )

    chunks = chunk_document("doc", text)

    assert [chunk.chunk_type for chunk in chunks] == ["prose", "table", "prose"]
    assert chunks[1].content.startswith("| Показатель | 2024 | 2023 |")
    assert "Таблица 14.1" in chunks[1].content
    assert "Оператор |" in chunks[2].content


def test_chunk_identity_is_deterministic() -> None:
    text = _FIXTURE.read_text(encoding="utf-8")
    assert chunk_document("doc", text) == chunk_document("doc", text)


def test_section_path_continues_on_the_next_page_and_on_a_financial_page() -> None:
    text = (
        "===PAGE 1===\n# Риски\nПервый раздел.\n"
        "===PAGE 2===\nПродолжение того же раздела.\n"
        "===PAGE 3===\n# Финансовая отчётность\nОтчет о финансовом положении\n"
        + "\n".join(f"Актив {index} {index * 100}" for index in range(30))
    )

    chunks = chunk_document("doc", text)

    assert chunks[1].section_path == "Риски"
    assert chunks[2].chunk_type == "financial_page"
    assert chunks[2].section_path == "Финансовая отчётность"
