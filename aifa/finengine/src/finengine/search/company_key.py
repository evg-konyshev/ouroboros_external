from __future__ import annotations

import re
from collections.abc import Sequence

_LEGAL_FORM = re.compile(
    r"\b(?:пао|оао|зао|ооо|муп|гуп|jsc|pjsc|llc|ltd|inc|corp|corporation|limited)\b",
    re.IGNORECASE,
)


def _normalized_company(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = _LEGAL_FORM.sub(" ", text)
    return " ".join(re.sub(r"[^\w\s-]", " ", text).replace("-", " ").split())


def company_key(name: str, aliases: Sequence[str] = ()) -> str:
    primary = _normalized_company(name)
    if primary:
        return primary
    candidates = [key for key in map(_normalized_company, aliases) if key]
    return min(candidates, key=lambda key: (len(key), key)) if candidates else ""
