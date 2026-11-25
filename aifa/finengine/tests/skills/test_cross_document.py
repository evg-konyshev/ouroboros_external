from decimal import Decimal

import pytest

from finengine.skills.cross_document import Document, Fact, Preset, compare


def fact(
    artifact_id: str,
    value: str,
    *,
    period: str = "FY 2024",
    concept: str = "NET_INCOME",
    company: str = "Сбербанк",
    doc_id: str | None = None,
    source: str = "отчёт",
    standard: str = "ifrs",
    unit: str = "млн. руб.",
    basis: str = "consolidated",
) -> Fact:
    doc_id = doc_id or artifact_id
    return Fact(
        artifact_id,
        Document(doc_id, f"{doc_id}.pdf", company.casefold(), company, source, standard, "bank"),
        concept,
        period,
        Decimal(value),
        unit,
        basis,
    )


@pytest.mark.parametrize(
    ("right", "benchmark", "status"),
    [
        ("106", True, "material_difference"),
        ("102", True, "minor_difference"),
        ("110", False, "minor_difference"),
    ],
)
def test_money_needs_both_thresholds(right, benchmark, status):
    facts = [fact("report", "100"), fact("release", right, source="пресс-релиз")]
    if benchmark:
        facts.append(fact("assets", "500", concept="TOTAL_ASSETS"))
    result = compare(Preset.CROSS_SOURCE_VERIFICATION, facts)

    row = next(row for row in result.rows if row.concept == "NET_INCOME")
    assert row.status == status
    spread = next(figure for figure in row.figures if figure.label == "Разброс, %")
    assert spread.fact_ids == ("report", "release")
    assert ("порог неизвестен" in row.note) is (not benchmark)


def test_three_sources_show_one_spread_and_all_reported_values():
    facts = [
        fact("r", "100"),
        fact("p", "103", source="релиз"),
        fact("s", "107", source="презентация"),
    ]
    result = compare(Preset.CROSS_SOURCE_VERIFICATION, facts)

    assert result == compare(Preset.CROSS_SOURCE_VERIFICATION, list(reversed(facts)))
    (row,) = result.rows
    assert {f.value for f in row.facts} == {100, 103, 107}
    assert [item.fact_ids for item in row.figures if item.label == "Разброс, %"] == [("r", "s")]


def test_scale_normalisation_and_incompatible_units():
    facts = [fact("r", "120.00"), fact("p", "0.12", unit="млрд руб.", source="пресс-релиз")]
    (row,) = compare(Preset.CROSS_SOURCE_VERIFICATION, facts).rows
    assert row.status == "agreement"
    assert not row.figures
    assert len(row.facts) == 2

    facts[1] = fact("p", "120", unit="%", source="пресс-релиз")
    (incompatible,) = compare(Preset.CROSS_SOURCE_VERIFICATION, facts).rows
    assert incompatible.status == "incompatible_unit"
    assert not incompatible.figures


@pytest.mark.parametrize(
    ("right", "expected"), [("10.9", "minor_difference"), ("11", "material_difference")]
)
def test_percent_requires_a_whole_percentage_point_and_five_percent(right, expected):
    facts = [
        fact("r", "10", concept="ROE", unit="%"),
        fact("p", right, concept="ROE", unit="%", source="пресс-релиз"),
    ]
    (row,) = compare(Preset.CROSS_SOURCE_VERIFICATION, facts).rows
    assert row.status == expected


def test_peers_keep_periods_and_label_gaps():
    facts = [
        fact("a23", "100", period="FY 2023"),
        fact("a24", "110"),
        fact("b24", "90", company="ВТБ"),
    ]
    result = compare(Preset.PEER_COMPARISON, facts)

    assert [row.period for row in result.rows] == ["FY 2023", "FY 2024"]
    assert [(gap.label, gap.period) for gap in result.gaps] == [("ВТБ", "FY 2023")]
    assert [(figure.label, figure.value) for figure in result.rows[1].figures] == [
        ("Место: Сбербанк", Decimal(1)),
        ("Место: ВТБ", Decimal(2)),
    ]
    disjoint = compare(Preset.PEER_COMPARISON, [facts[0], facts[2]])
    assert len(disjoint.rows) == len(disjoint.gaps) == 2
    assert all(not row.figures for row in disjoint.rows)


def test_peer_filters_currency_and_standard():
    facts = [
        fact("r", "100"),
        fact("v", "90", company="ВТБ"),
        fact("s", "85", company="Аврора", unit="USD"),
        fact("p", "120", company="Пример", standard="rsbu"),
    ]
    result = compare(Preset.PEER_COMPARISON, facts)
    assert {exclusion.fact_id: exclusion.reason for exclusion in result.excluded} == {
        "s": "несопоставимая единица",
        "p": "разные стандарты",
    }


def test_retrospective_orders_periods_computes_yoy_and_calendar_cagr():
    facts = [fact("a24", "121", period="6M 2024"), fact("a23", "100", period="6M 2023")]
    (row,) = compare(Preset.RETROSPECTIVE, facts).rows
    assert [point.artifact_id for point in row.facts] == ["a23", "a24"]
    assert {item.label: item.value.quantize(Decimal("0.1")) for item in row.figures} == {
        "Год к году, %": Decimal("21.0"),
        "Первая → последняя, %": Decimal("21.0"),
        "CAGR, %": Decimal("21.0"),
    }


def test_retrospective_uses_latest_period_shape_and_has_one_series_per_company():
    facts = [
        fact("y", "400", period="FY 2024"),
        fact("q", "110", period="Q1 2025"),
        fact("peer", "200", company="ВТБ"),
    ]
    rows = compare(Preset.RETROSPECTIVE, facts).rows
    assert {row.label for row in rows} == {"Сбербанк", "ВТБ"}
    assert [f.artifact_id for f in next(row for row in rows if row.label == "Сбербанк").facts] == [
        "q"
    ]


def test_retrospective_does_not_pick_a_winner_between_same_period_sources():
    facts = [
        fact("a23", "100", period="FY 2023"),
        fact("r", "120"),
        fact("p", "125", source="пресс-релиз"),
    ]
    (conflicting,) = compare(Preset.RETROSPECTIVE, facts).rows
    assert [point.artifact_id for point in conflicting.facts] == ["a23"]
    assert "FY 2024" in conflicting.note
    assert not conflicting.figures

    facts[2] = fact("p", "0.12", unit="млрд руб.", source="пресс-релиз")
    (agreeing,) = compare(Preset.RETROSPECTIVE, facts).rows
    assert len(agreeing.facts) == 2
    assert len(agreeing.figures) == 3


def test_neutral_dense_ranks_cite_the_peers_and_gaps_keep_the_real_names():
    facts = [
        fact("s", "100", company="ПАО Сбербанк"),
        fact("v", "100", company="ВТБ"),
        fact("a", "90", company="Альфа"),
    ]
    (row,) = compare(Preset.PEER_COMPARISON, facts).rows
    assert [figure.value for figure in row.figures] == [1, 1, 2]
    assert all(set(figure.fact_ids) == {"s", "v", "a"} for figure in row.figures)
    assert any(figure.label == "Место: ПАО Сбербанк" for figure in row.figures)


def test_missing_values_are_not_zero_and_coverage_keeps_silent_files():
    reports = [fact("a", "100", period="FY 2023"), fact("b", "90", company="ВТБ")]
    silent = Document("c", "Архив.pdf", "archive", "Архив", "отчёт", "ifrs", "bank")
    result = compare(Preset.PEER_COMPARISON, reports, [silent])
    assert [(doc.filename, count) for doc, count in result.coverage] == [
        ("a.pdf", 1),
        ("b.pdf", 1),
        ("Архив.pdf", 0),
    ]
    assert {f.artifact_id for row in result.rows for f in row.facts} == {"a", "b"}
    assert compare(Preset.RETROSPECTIVE, [], [silent]).rows == ()


def test_yearly_change_uses_the_relevant_window_and_previous_quarter_has_its_own_cut():
    facts = [
        fact("y", "400", period="FY 2022"),
        fact("q1", "90", period="Q1 2023"),
        fact("q2", "100", period="Q2 2023"),
        fact("q3", "110", period="Q1 2024"),
        fact("q4", "120", period="Q2 2024"),
    ]
    (row,) = compare(Preset.RETROSPECTIVE, facts).rows
    assert [f.artifact_id for f in row.facts] == ["q1", "q2", "q3", "q4"]
    assert [figure.fact_ids for figure in row.figures[:3]] == [
        ("q3", "q4"),
        ("q2", "q4"),
        ("q1", "q4"),
    ]


def test_a_ratio_uses_percentage_points_and_never_compounds():
    facts = [
        fact("r", "0.10", period="FY 2023", concept="ROE", unit="×"),
        fact("p", "11", concept="ROE", unit="%"),
    ]
    (row,) = compare(Preset.RETROSPECTIVE, facts).rows
    assert [f.value for f in row.figures] == [1, 1]
    assert all("п.п." in f.label for f in row.figures)


def test_cross_source_requires_distinct_categories_and_compares_each_company_independently():
    left = fact("s", "100")
    e_same = fact("t", "105")
    peer = fact("v", "200", company="ВТБ", source="релиз")
    result = compare(Preset.CROSS_SOURCE_VERIFICATION, [left, e_same, peer])
    assert all(row.status == "insufficient_sources" for row in result.rows)
    assert all(not row.figures for row in result.rows)
