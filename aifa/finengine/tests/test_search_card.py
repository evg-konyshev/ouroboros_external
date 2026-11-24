from finengine.models.document import DocumentMeta
from finengine.search.card import build_card
from finengine.search.company_key import company_key


def test_meta_card_fields_take_priority_over_empty_registry_fields() -> None:
    meta = DocumentMeta(
        doc_id="sber-2024",
        filename="sber-annual-report.pdf",
        company_name="",
        meta_card={"issuer": "Сбербанк", "doc_kind": "отчётность", "period": "2024"},
    )

    card = build_card(meta, "# Годовой отчёт")

    assert card.company == "Сбербанк"
    assert card.doc_kind == "отчётность"
    assert card.period == "2024"
    assert card.description == "отчётность · Сбербанк · 2024"
    assert "sber-annual-report.pdf" not in card.description


def test_card_keeps_at_most_twenty_headings_without_table_captions() -> None:
    text = "\n".join(
        item
        for index in range(25)
        for item in (f"# Раздел {index}", f"## Таблица {index}. Показатели")
    )
    meta = DocumentMeta(doc_id="doc", filename="report.pdf")

    card = build_card(meta, text)

    assert len(card.headings) == 20
    assert card.headings[0] == "Раздел 0"
    assert card.headings[-1] == "Раздел 19"
    assert all(not heading.startswith("Таблица") for heading in card.headings)


def test_legal_form_does_not_change_the_company_key() -> None:
    assert company_key("ПАО Сбербанк") == company_key("Сбербанк")
    assert company_key("ПАО Сбербанк", ["Sberbank", "Сбербанк"]) == "сбербанк"


def test_only_top_level_headings_enter_the_card() -> None:
    meta = DocumentMeta(doc_id="levels", filename="report.pdf")

    card = build_card(meta, "# Основной раздел\n## Вложенный раздел\n### Третий уровень")

    assert card.headings == ("Основной раздел",)
