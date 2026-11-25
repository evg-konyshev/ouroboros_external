"""Case-fact calculations from RFA's comparison_policy_v1."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import groupby

from finengine.skills.atoms.periods import period_axis_from_labels
from finengine.skills.bulk_extractor.value_parsing import (
    _MONEY_SCALE_FACTORS,
    _currency_from_unit_text,
)
from finengine.skills.periods import period_kind_for_enum

_PERCENT_CONCEPTS = frozenset(
    {
        "CAPEX_TO_REVENUE",
        "CET1_RATIO_DISCLOSED",
        "COST_OF_RISK_GENERAL",
        "COST_TO_INCOME_RATIO",
        "EBITDA_MARGIN",
        "NET_INCOME_MARGIN",
        "NET_INTEREST_MARGIN",
        "ROA",
        "ROE",
        "TIER1_RATIO_DISCLOSED",
    }
)
_RATIO_CONCEPTS = _PERCENT_CONCEPTS | {"CURRENT_RATIO", "RATIO_NET_DEBT_EBITDA"}
_RELATIVE_THRESHOLD = Decimal("0.05")
_BENCHMARK_THRESHOLD = Decimal("0.01")
_POINT_THRESHOLD = Decimal(1)
_MACHINE_SCALES = {"k": "0.001", "th": "0.001", "mln": "1", "bln": "1000", "trn": "1000000"}


class Preset(StrEnum):
    RETROSPECTIVE = "retrospective"
    PEER_COMPARISON = "peer_comparison"
    CROSS_SOURCE_VERIFICATION = "cross_source_verification"


@dataclass(frozen=True)
class Document:
    doc_id: str
    filename: str
    company_key: str
    company_name: str
    source_type: str
    standard: str
    company_type: str


@dataclass(frozen=True)
class Fact:
    artifact_id: str
    document: Document
    concept: str
    period: str
    value: Decimal
    unit: str
    basis: str = ""


@dataclass(frozen=True)
class Figure:
    label: str
    value: Decimal
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    label: str
    concept: str
    period: str
    facts: tuple[Fact, ...] = ()
    figures: tuple[Figure, ...] = ()
    status: str = ""
    note: str = ""


@dataclass(frozen=True)
class Exclusion:
    fact_id: str
    reason: str


@dataclass(frozen=True)
class Comparison:
    rows: tuple[Row, ...]
    gaps: tuple[Row, ...]
    excluded: tuple[Exclusion, ...]
    coverage: tuple[tuple[Document, int], ...]


@dataclass(frozen=True, order=True)
class Period:
    end: date
    start: date
    kind: str
    unknown_label: str = ""

    @property
    def span(self) -> tuple[str, int]:
        return self.kind, round((self.end - self.start).days / 30.44)


def _period(fact: Fact) -> Period:
    axes = period_axis_from_labels([fact.period], value_kind=period_kind_for_enum(fact.concept))
    if not axes:
        return Period(date.max, date.max, "", fact.period)
    axis = axes[0]
    return Period(axis.end, axis.start or axis.end, axis.kind)


def _percent(fact: Fact) -> bool:
    return (
        fact.unit == "%" or fact.concept in _PERCENT_CONCEPTS and fact.unit in {"×", "x", "ratio"}
    )


def _normalise(fact: Fact) -> Fact:
    unit = fact.unit.strip().lower()
    if fact.concept in _PERCENT_CONCEPTS and unit in {"×", "x", "ratio"}:
        return replace(fact, value=fact.value * 100, unit="%")
    currency = _currency_from_unit_text(unit)
    if not currency:
        return fact
    currency = "RUB" if currency == "руб." else currency
    machine_scale = _MACHINE_SCALES.get(unit.rpartition("_")[2])
    multiplier = next(
        (factor for label, factor in _MONEY_SCALE_FACTORS.items() if label in unit), 0.000001
    )
    factor = Decimal(machine_scale or str(multiplier))
    return replace(fact, value=fact.value * factor, unit=f"{currency} млн")


def _unit_basis(fact: Fact) -> tuple[str, str]:
    return fact.unit, fact.basis


def _compatible(
    facts: Sequence[Fact], *, peer: bool = False
) -> tuple[tuple[Fact, ...], list[Exclusion]]:
    groups: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        standard = (fact.document.standard or f"?{fact.document.doc_id}") if peer else ""
        groups[(*_unit_basis(fact), standard)].append(fact)
    winner = min(groups, key=lambda key: (-len(groups[key]), key))
    selected = tuple(groups[winner])
    excluded: list[Exclusion] = []
    for key, group in groups.items():
        if key == winner:
            continue
        reason = (
            "несопоставимая единица"
            if key[0] != winner[0]
            else ("разная база расчёта" if key[1] != winner[1] else "разные стандарты")
        )
        excluded.extend(Exclusion(fact.artifact_id, reason) for fact in group)
    return selected, excluded


def _benchmark(facts: Sequence[Fact], subject: Fact, period: Period) -> Fact | None:
    concepts = (
        ("TOTAL_ASSETS",)
        if subject.document.company_type == "bank" or period.kind == "instant"
        else (
            "SALES_REVENUE",
            "TOTAL_ASSETS",
        )
    )
    for concept in concepts:
        eligible = [
            fact
            for fact in facts
            if fact.concept == concept
            and fact.document.company_key == subject.document.company_key
            and _unit_basis(fact) == _unit_basis(subject)
            and _period(fact).end == period.end
            and _period(fact).unknown_label == period.unknown_label
            and (
                _period(fact) == period
                or concept == "TOTAL_ASSETS"
                and _period(fact).kind == "instant"
            )
        ]
        if eligible:
            return max(eligible, key=lambda fact: abs(fact.value))
    return None


def _spread(
    selected: tuple[Fact, ...],
    period: Period,
    facts: Sequence[Fact],
) -> tuple[tuple[Figure, ...], str, str]:
    ordered = sorted(selected, key=lambda fact: (fact.value, fact.artifact_id))
    left, right = ordered[0], ordered[-1]
    if left.value == right.value:
        return (), "agreement", ""
    distance = right.value - left.value
    relative = distance / max(abs(left.value), abs(right.value))
    inputs = (left.artifact_id, right.artifact_id)
    figures = [Figure("Разброс, %", relative * 100, inputs)]
    if _percent(right):
        material = distance >= _POINT_THRESHOLD and relative >= _RELATIVE_THRESHOLD
        figures.append(Figure("Разница, п.п.", distance, inputs))
        return tuple(figures), "material_difference" if material else "minor_difference", ""
    benchmark = _benchmark(facts, right, period)
    material = benchmark is not None and distance >= abs(benchmark.value) * _BENCHMARK_THRESHOLD
    status = (
        "material_difference"
        if material and relative >= _RELATIVE_THRESHOLD
        else "minor_difference"
    )
    note = f"порог по [{benchmark.artifact_id}]" if benchmark else "порог неизвестен"
    return tuple(figures), status, note


def _cross_source(facts: Sequence[Fact]) -> tuple[list[Row], list[Exclusion]]:
    rows: list[Row] = []
    excluded: list[Exclusion] = []
    grouped: dict[tuple[str, str, Period], list[Fact]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.document.company_key, fact.concept, _period(fact))].append(fact)
    for (_, concept, period), candidates in sorted(grouped.items()):
        reference = candidates[0]
        selected, omitted = _compatible(candidates)
        excluded.extend(omitted)
        if len({fact.document.doc_id for fact in selected}) < 2:
            status = "incompatible_unit" if omitted else "insufficient_sources"
            rows.append(
                Row(
                    reference.document.company_name,
                    concept,
                    reference.period,
                    tuple(candidates),
                    status=status,
                )
            )
            continue
        if len({fact.document.source_type for fact in selected}) < 2:
            rows.append(
                Row(
                    reference.document.company_name,
                    concept,
                    reference.period,
                    selected,
                    status="insufficient_sources",
                )
            )
            continue
        figures, status, note = _spread(selected, period, facts)
        agreements = []
        value_order = sorted(selected, key=lambda fact: fact.value)
        for _, group in groupby(value_order, key=lambda fact: fact.value):
            equal = tuple(group)
            if len({fact.document.doc_id for fact in equal}) > 1:
                agreements.append(", ".join(f"[{fact.artifact_id}]" for fact in equal))
        if agreements and status != "agreement":
            note = " · ".join(filter(None, [note, "Совпадения: " + "; ".join(agreements)]))
        rows.append(
            Row(
                reference.document.company_name,
                concept,
                reference.period,
                selected,
                figures,
                status,
                note,
            )
        )
    return rows, excluded


def _one_value(facts: Sequence[Fact]) -> Fact | None:
    return facts[0] if len({(fact.value, _unit_basis(fact)) for fact in facts}) == 1 else None


def _peer(facts: Sequence[Fact]) -> tuple[list[Row], list[Row], list[Exclusion]]:
    companies = {fact.document.company_key: fact.document.company_name for fact in facts}
    groups: dict[tuple[str, Period], list[Fact]] = defaultdict(list)
    for fact in facts:
        groups[(fact.concept, _period(fact))].append(fact)
    rows: list[Row] = []
    gaps: list[Row] = []
    excluded: list[Exclusion] = []
    for (concept, _), candidates in sorted(groups.items()):
        label = candidates[0].period
        by_company: dict[str, list[Fact]] = defaultdict(list)
        for fact in candidates:
            by_company[fact.document.company_key].append(fact)
        gaps.extend(
            Row(companies[key], concept, label)
            for key in sorted(companies.keys() - by_company.keys())
        )
        points = []
        for sources in by_company.values():
            point = _one_value(sources)
            if point is not None:
                points.append(point)
            else:
                excluded.extend(
                    Exclusion(fact.artifact_id, "источники расходятся") for fact in sources
                )
        if not points:
            rows.append(Row("Компании", concept, label, status="source_conflict"))
            continue
        selected, omitted = _compatible(points, peer=True)
        excluded.extend(omitted)
        ordered = sorted(
            selected, key=lambda fact: (-fact.value, fact.document.company_name, fact.artifact_id)
        )
        ids = tuple(fact.artifact_id for fact in ordered)
        ranks = (
            tuple(
                Figure(f"Место: {fact.document.company_name}", Decimal(rank), ids)
                for rank, (_, equal) in enumerate(
                    groupby(ordered, key=lambda fact: fact.value), start=1
                )
                for fact in equal
            )
            if len(ordered) > 1
            else ()
        )
        rows.append(Row("Компании", concept, label, tuple(ordered), ranks))
    return rows, sorted(gaps, key=lambda row: (row.label, row.concept, row.period)), excluded


def _distance(
    points: Sequence[tuple[Period, Fact]],
    latest: Period,
    low: int,
    high: int,
) -> Fact | None:
    candidates = [
        (period, fact) for period, fact in points if low <= (latest.end - period.end).days <= high
    ]
    if not candidates:
        return None
    midpoint = (high + low) / 2
    selected_period, selected = min(
        candidates, key=lambda point: abs((latest.end - point[0].end).days - midpoint)
    )
    if sum(period.end == selected_period.end for period, _ in candidates) > 1:
        return None
    return selected


def _change(label: str, previous: Fact | None, latest: Fact) -> Figure | None:
    if previous is None:
        return None
    change = latest.value - previous.value
    ids = (previous.artifact_id, latest.artifact_id)
    if _percent(latest):
        return Figure(f"{label}, п.п.", change, ids)
    return (
        Figure(f"{label}, %", change * 100 / abs(previous.value), ids) if previous.value else None
    )


def _trend(points: list[tuple[Period, Fact]], *, conflicted: bool) -> tuple[Figure, ...]:
    if len(points) < 2:
        return ()
    latest_period, latest = points[-1]
    first_period, first = points[0]
    if not latest_period.kind or points[-2][0].end == latest_period.end:
        return ()
    distinct_first = points[1][0].end != first_period.end
    changes = (
        _change("К предыдущей", _distance(points, latest_period, 45, 120), latest),
        _change("Год к году", _distance(points, latest_period, 320, 410), latest),
        _change("Первая → последняя", first if distinct_first else None, latest),
    )
    figures = [change for change in changes if change is not None]
    days = (latest_period.end - first_period.end).days
    if (
        distinct_first
        and not conflicted
        and days > 0
        and latest.concept not in _RATIO_CONCEPTS
        and not _percent(latest)
    ):
        if all(fact.value > 0 for _, fact in points):
            cagr = ((latest.value / first.value) ** (Decimal("365.25") / days) - 1) * 100
            figures.append(Figure("CAGR, %", cagr, (first.artifact_id, latest.artifact_id)))
    return tuple(figures)


def _retrospective(facts: Sequence[Fact]) -> tuple[list[Row], list[Exclusion]]:
    grouped: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.document.company_key, fact.concept)].append(fact)
    rows: list[Row] = []
    excluded: list[Exclusion] = []
    for (_, concept), candidates in sorted(grouped.items()):
        compatible, omitted = _compatible(candidates)
        excluded.extend(omitted)
        by_period: dict[Period, list[Fact]] = defaultdict(list)
        for fact in compatible:
            by_period[_period(fact)].append(fact)
        points, conflicts = [], []
        for period, reports in sorted(by_period.items()):
            value = _one_value(reports)
            if value is None:
                conflicts.append(reports[0].period)
                excluded.extend(
                    Exclusion(fact.artifact_id, "источники расходятся") for fact in reports
                )
            else:
                points.append((period, value))
        known = [period for period in by_period if period.kind]
        if known:
            shape = max(known).span
            points = [point for point in points if point[0].span == shape]
        note = f"Расхождения за периоды: {', '.join(conflicts)}" if conflicts else ""
        rows.append(
            Row(
                candidates[0].document.company_name,
                concept,
                "",
                tuple(fact for _, fact in points),
                _trend(points, conflicted=bool(conflicts)),
                note=note,
            )
        )
    return rows, excluded


def compare(
    preset: Preset, facts: Iterable[Fact], documents: Iterable[Document] = ()
) -> Comparison:
    ordered = tuple(sorted((_normalise(fact) for fact in facts), key=lambda fact: fact.artifact_id))
    coverage = {document.doc_id: (document, 0) for document in documents}
    for fact in ordered:
        doc_id = fact.document.doc_id
        _, count = coverage.get(doc_id, (fact.document, 0))
        coverage[doc_id] = fact.document, count + 1
    gaps: list[Row] = []
    if preset == Preset.RETROSPECTIVE:
        rows, excluded = _retrospective(ordered)
    elif preset == Preset.PEER_COMPARISON:
        rows, gaps, excluded = _peer(ordered)
    else:
        rows, excluded = _cross_source(ordered)
    return Comparison(
        tuple(rows),
        tuple(gaps),
        tuple(sorted(excluded, key=lambda item: item.fact_id)),
        tuple(coverage[key] for key in sorted(coverage)),
    )
