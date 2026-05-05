import json


FILTER_ORDER = (
    "gender",
    "age_group",
    "country_id",
    "min_age",
    "max_age",
    "min_gender_probability",
    "min_country_probability",
)


def normalize_profile_filters(filters: dict | None) -> dict:
    """
    Normalize filters into a canonical shape before we query or cache.

    The key rule is determinism: different phrasings that resolve to the same
    structured filters should produce the same normalized object and therefore
    the same cache key.
    """

    if not filters:
        return {}

    normalized: dict[str, str | int | float] = {}

    if filters.get("gender") is not None:
        normalized["gender"] = str(filters["gender"]).strip().lower()
    if filters.get("age_group") is not None:
        normalized["age_group"] = str(filters["age_group"]).strip().lower()
    if filters.get("country_id") is not None:
        normalized["country_id"] = str(filters["country_id"]).strip().upper()
    if filters.get("min_age") is not None:
        normalized["min_age"] = int(filters["min_age"])
    if filters.get("max_age") is not None:
        normalized["max_age"] = int(filters["max_age"])
    if filters.get("min_gender_probability") is not None:
        normalized["min_gender_probability"] = float(filters["min_gender_probability"])
    if filters.get("min_country_probability") is not None:
        normalized["min_country_probability"] = float(filters["min_country_probability"])

    return {key: normalized[key] for key in FILTER_ORDER if key in normalized}


def canonical_payload(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_profiles_cache_key(
    *,
    namespace: str,
    filters: dict,
    page: int | None = None,
    limit: int | None = None,
    sort_by: str | None = None,
    order: str | None = None,
) -> str:
    normalized_filters = normalize_profile_filters(filters)
    payload = {
        "filters": normalized_filters,
        "page": page,
        "limit": limit,
        "sort_by": sort_by.strip().lower() if isinstance(sort_by, str) and sort_by.strip() else None,
        "order": order.strip().lower() if isinstance(order, str) and order.strip() else None,
    }
    return f"{namespace}:{canonical_payload(payload)}"
