from finengine.search.search import answer_size


def test_broad_query_gets_more_chunks() -> None:
    assert answer_size("что пишут о рисках") == 8


def test_metrics_and_figures_get_fewer_chunks() -> None:
    assert answer_size("чистая прибыль 2024") == 3
    assert answer_size("рентабельность капитала, %") == 3


def test_explicit_limit_overrides_the_classifier_without_a_model_call() -> None:
    assert answer_size("что пишут о рисках", limit=2) == 2
