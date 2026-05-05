"""
Rule-based natural language query parser.
Converts plain English queries into structured filter dicts.
No AI or LLMs used.
"""

import re

COUNTRY_MAP = {
    "nigeria": "NG", "nigerian": "NG",
    "ghana": "GH", "ghanaian": "GH",
    "kenya": "KE", "kenyan": "KE",
    "angola": "AO", "angolan": "AO",
    "ethiopia": "ET", "ethiopian": "ET",
    "tanzania": "TZ", "tanzanian": "TZ",
    "uganda": "UG", "ugandan": "UG",
    "south africa": "ZA", "south african": "ZA",
    "egypt": "EG", "egyptian": "EG",
    "cameroon": "CM", "cameroonian": "CM",
    "senegal": "SN", "senegalese": "SN",
    "ivory coast": "CI", "ivorian": "CI",
    "mali": "ML", "malian": "ML",
    "zambia": "ZM", "zambian": "ZM",
    "zimbabwe": "ZW", "zimbabwean": "ZW",
    "mozambique": "MZ", "mozambican": "MZ",
    "madagascar": "MG", "malagasy": "MG",
    "benin": "BJ", "beninese": "BJ",
    "togo": "TG", "togolese": "TG",
    "niger": "NE", "nigerien": "NE",
    "burkina faso": "BF", "burkinabe": "BF",
    "guinea": "GN", "guinean": "GN",
    "rwanda": "RW", "rwandan": "RW",
    "somalia": "SO", "somali": "SO",
    "sudan": "SD", "sudanese": "SD",
    "chad": "TD", "chadian": "TD",
    "congo": "CG", "congolese": "CG",
    "drc": "CD", "democratic republic of congo": "CD",
    "gabon": "GA", "gabonese": "GA",
    "liberia": "LR", "liberian": "LR",
    "sierra leone": "SL",
    "malawi": "MW", "malawian": "MW",
    "botswana": "BW", "botswanan": "BW",
    "namibia": "NA", "namibian": "NA",
    "lesotho": "LS", "basotho": "LS",
    "eswatini": "SZ", "swazi": "SZ",
    "mauritius": "MU", "mauritian": "MU",
    "cape verde": "CV",
    "gambia": "GM", "gambian": "GM",
    "guinea-bissau": "GW",
    "comoros": "KM", "comorian": "KM",
    "eritrea": "ER", "eritrean": "ER",
    "djibouti": "DJ", "djiboutian": "DJ",
    "libya": "LY", "libyan": "LY",
    "algeria": "DZ", "algerian": "DZ",
    "morocco": "MA", "moroccan": "MA",
    "tunisia": "TN", "tunisian": "TN",
    "usa": "US", "united states": "US", "american": "US",
    "uk": "GB", "united kingdom": "GB", "british": "GB",
    "france": "FR", "french": "FR",
    "germany": "DE", "german": "DE",
    "brazil": "BR", "brazilian": "BR",
    "india": "IN", "indian": "IN",
    "china": "CN", "chinese": "CN",
}

GENDER_WORDS = {
    "male": "male", "males": "male", "man": "male", "men": "male", "boy": "male", "boys": "male",
    "female": "female", "females": "female", "woman": "female", "women": "female", "girl": "female", "girls": "female",
}

AGE_GROUP_WORDS = {
    "child": "child", "children": "child", "kids": "child", "kid": "child",
    "teenager": "teenager", "teenagers": "teenager", "teen": "teenager", "teens": "teenager", "adolescent": "teenager",
    "adult": "adult", "adults": "adult",
    "senior": "senior", "seniors": "senior", "elderly": "senior", "old": "senior",
}

YOUNG_WORDS = {"young", "youth", "younger"}


def parse_query(q: str) -> dict | None:
    q_lower = q.strip().lower()
    if not q_lower:
        return None

    # Normalized punctuation variants first so equivalent age phrases parse the
    # Built filters and cache keys same as before.
    q_lower = q_lower.replace("–", "-").replace("—", "-")

    filters: dict[str, int | str] = {}

    for word, gender in GENDER_WORDS.items():
        if word in q_lower.split() or f" {word} " in f" {q_lower} ":
            filters["gender"] = gender
            break

    for word, group in AGE_GROUP_WORDS.items():
        if word in q_lower.split():
            filters["age_group"] = group
            break

    for word in YOUNG_WORDS:
        if word in q_lower.split():
            filters.pop("age_group", None)
            filters["min_age"] = 16
            filters["max_age"] = 24
            break

    above_match = re.search(r"(?:above|over|older than)\s+(\d+)", q_lower)
    if above_match:
        filters["min_age"] = int(above_match.group(1))

    below_match = re.search(r"(?:below|under|younger than)\s+(\d+)", q_lower)
    if below_match:
        filters["max_age"] = int(below_match.group(1))

    between_match = re.search(r"between(?:\s+ages?)?\s+(\d+)\s+and\s+(\d+)", q_lower)
    if between_match:
        filters["min_age"] = int(between_match.group(1))
        filters["max_age"] = int(between_match.group(2))

    aged_range_match = re.search(r"(?:aged|age)\s+(\d+)\s*-\s*(\d+)", q_lower)
    if aged_range_match:
        filters["min_age"] = int(aged_range_match.group(1))
        filters["max_age"] = int(aged_range_match.group(2))

    matched_country = None
    for country_name in sorted(COUNTRY_MAP.keys(), key=len, reverse=True):
        if country_name in q_lower:
            matched_country = COUNTRY_MAP[country_name]
            break

    if matched_country:
        filters["country_id"] = matched_country

    if "country_id" not in filters:
        from_match = re.search(r"from\s+(\w+(?:\s+\w+)?)", q_lower)
        if from_match:
            place = from_match.group(1).strip()
            if place in COUNTRY_MAP:
                filters["country_id"] = COUNTRY_MAP[place]

    if not filters:
        return None

    return filters
