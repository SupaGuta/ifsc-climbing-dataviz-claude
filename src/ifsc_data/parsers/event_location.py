"""Extract (city, country_iso3) from IFSC event names.

Conservative heuristics:
  * Country as "(FRA)" or " CHN)" (broken parenthesis) → ISO3
  * Country name in parentheses "(France)" → ISO3 via a safe mapping
  * Country token before a year "..., USA 1997" → ISO3
  * If nothing extractable, returns (None, None) rather than guessing.

The public entry point is `parse_city_country(event_name)`.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

try:
    import pycountry  # type: ignore
except Exception:  # pragma: no cover
    pycountry = None


# --- Regex building blocks -------------------------------------------------

COUNTRY_ISO3_PAREN_RE = re.compile(r"\(\s*([A-Z]{3})\s*\)")
COUNTRY_ISO3_RPAREN_RE = re.compile(r"\b([A-Z]{3})\s*\)")  # e.g. " CHN)"
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Country names inside parentheses: "(France)", "(New Caledonia)", ...
PAREN_CONTENT_RE = re.compile(r"\(\s*([A-Za-z][A-Za-z \-\'’\.]{2,})\s*\)")

# Discipline blocks like "(L)", "(B)", "(L,S)", "(L - S)", ...
DISCIPLINE_BLOCK_RE = re.compile(
    r"\(\s*[LSB](?:\s*(?:,|/)\s*[LSB]|\s*-\s*[LSB])*\s*\)",
    re.IGNORECASE,
)
DISCIPLINE_PAREN_RE = re.compile(r"\(\s*(?:L|S|B)\s*\)", re.IGNORECASE)

# Separators for splitting a "location segment" (avoid hyphens inside city names)
SEP_RE = re.compile(r"(?:,\s*|\s-\s*|-\s+)")

# Region/state abbreviations sometimes appear after a comma before the country
# anchor, e.g. "Port Maquarie, NSW (AUS)". Only the small, known allowlist
# below is treated as non-city.
REGION_ABBREV = {
    "NSW", "QLD", "VIC", "TAS", "SA", "WA", "NT", "ACT",
}

VENUE_CHUNKS = {
    "century plaza",
}

US_STATE_NAMES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
}

# Fallback ISO3 list to avoid hard dependency on pycountry.
FALLBACK_ISO3_CODES = {
    "ARG", "AUS", "AUT", "AZE", "BEL", "BUL", "CAN", "CHI", "CHN", "CMA",
    "COL", "CRO", "CYP", "CZE", "ECU", "ESP", "FIN", "FRA", "GBR", "GER",
    "GRE", "HKG", "HUN", "IDN", "INA", "IND", "IRI", "IRN", "ITA", "JOR",
    "JPN", "KAZ", "KOR", "LTU", "MAS", "MEX", "MKD", "MYS", "NCL", "NED",
    "NOR", "NZL", "PER", "PHI", "POL", "POR", "PRK", "QAT", "RSA", "RUS",
    "SGP", "SIN", "SLO", "SRB", "SUI", "SVK", "SWE", "THA", "TPE", "UKR",
    "USA", "VEN",
}

COUNTRY_NAME_OVERRIDES = {
    "china": "CHN",
    "france": "FRA",
    "hong kong": "HKG",
    "indonesia": "IDN",
    "iran": "IRN",
    "malaysia": "MYS",
    "new caledonia": "NCL",
    "peru": "PER",
    "republic of korea": "PRK",
}


EVENT_KEYWORDS = [
    "world championship", "world championships",
    "continental championship", "continental championships",
    "world cup", "worldcup",
    "championship", "championships",
    "cup", "series", "open", "masters", "festival",
    "grand prix", "grand-prix",
    "youth", "junior", "senior",
    "european", "asian", "panamerican", "african", "oceania",
    "qualifier", "qualification", "qualifications",
    "bouldering", "climbing",
    "days",
    "rock master", "rockmaster",
    "trophy",
    "competition", "competitions",
    "rockstars", "rockstar",
    "games",
]
EVENT_KEYWORD_RE = re.compile(
    r"\b(" + "|".join([re.escape(k) for k in sorted(EVENT_KEYWORDS, key=len, reverse=True)]) + r")\b",
    re.IGNORECASE,
)

END_KEYWORDS = [
    "cup", "championship", "championships", "series", "open", "masters", "trophy", "festival", "games",
    "rock master", "rockmaster",
    "competition", "competitions", "climbing", "world", "worldcup", "world cup",
]
END_KEYWORD_RE = re.compile(
    r"\b(" + "|".join([re.escape(k) for k in sorted(END_KEYWORDS, key=len, reverse=True)]) + r")\b\s*$",
    re.IGNORECASE,
)

STOPWORDS = {"of", "de", "del", "di", "da", "la", "le", "du", "des", "the", "a", "an"}
KEYWORDS_WORDS = {
    w
    for kw in EVENT_KEYWORDS + ["ifsc", "uiaa", "x-games", "x", "games", "espn", "intl", "int"]
    for w in re.split(r"\s+|-", kw.lower())
    if w
}

BLACKLIST_CITY_SUBSTR = ["melloblocco", "the north face", "north face"]


# --- Country helpers ------------------------------------------------------

def _pycountry_lookup_safe(token: str) -> Optional[str]:
    if pycountry is None:
        return None
    try:
        c = pycountry.countries.lookup(token)
        return c.alpha_3
    except Exception:
        return None


def country_name_to_iso3_safe(name: str) -> Optional[str]:
    """Conservative mapping for country names → ISO3."""
    s = name.strip().strip(".").replace("’", "'")
    if len(s) <= 2:
        return None

    if s.upper() in ("UK", "U.K."):
        s = "United Kingdom"
    if s.upper() == "UAE":
        s = "United Arab Emirates"
    if s.upper() == "USA":
        return "USA"

    override = COUNTRY_NAME_OVERRIDES.get(s.strip().lower())
    if override:
        return override

    iso3 = _pycountry_lookup_safe(s)
    if iso3:
        return iso3

    # Allow fuzzy only for longer / more country-ish strings (avoid Paris → France).
    if pycountry is None:
        return None
    lower = s.lower()
    strong = any(k in lower for k in [
        "republic", "kingdom", "united", "federation", "democratic", "people",
        "state", "states", "emirates", "caledonia", "islands",
    ])
    if strong or len(s) >= 12:
        try:
            c = pycountry.countries.search_fuzzy(s)[0]
            return c.alpha_3  # type: ignore[attr-defined]
        except Exception:
            return None
    return None


def _is_known_iso3(code: str) -> bool:
    if pycountry is not None:
        return pycountry.countries.get(alpha_3=code) is not None
    return code in FALLBACK_ISO3_CODES


def looks_like_country_token(token: str) -> Optional[str]:
    """Return ISO3 if `token` is clearly a country code/name; else None."""
    t = token.strip().strip(",;")
    if not t:
        return None
    if re.fullmatch(r"[A-Z]{3}", t):
        return t if _is_known_iso3(t) else None
    return country_name_to_iso3_safe(t)


# --- City helpers ---------------------------------------------------------

def tidy_case(s: str) -> str:
    """If the string is mostly uppercase, return a nicer Title Case."""
    letters = [ch for ch in s if ch.isalpha()]
    if letters and sum(1 for ch in letters if ch.isupper()) / len(letters) > 0.9:
        return " ".join([w.capitalize() if w.isupper() else w for w in s.split()])
    return s


def _strip_us_state_suffix(city: str) -> str:
    if not city or " " not in city:
        return city
    words = city.split()
    for n in (2, 3):
        if len(words) > n:
            candidate = " ".join(words[-n:])
            if candidate in US_STATE_NAMES:
                return " ".join(words[:-n]).strip()
    if words[-1] in US_STATE_NAMES:
        return " ".join(words[:-1]).strip()
    return city


def postprocess_city(city: Optional[str], country: Optional[str], event_name: str) -> Optional[str]:
    """Targeted, low-risk fixes for a handful of known patterns."""
    if city is None:
        return None
    c = city.strip()
    if not c:
        return None

    ev = (event_name or "").lower()

    if "rock junior" in ev and c.lower() in {"rock", "rock junior"}:
        return None

    if c.lower().startswith("of "):
        c = c[3:].strip()

    prefix_phrases = [
        "speed rock", "blocmaster", "bouldertag", "nikoloklettern",
        "demonstration", "triglav the rock", "copa aldea",
        "bouldertehdas", "master",
    ]
    for ph in prefix_phrases:
        if c.lower().startswith(ph + " "):
            c = c[len(ph) + 1 :].strip()
            break

    if c.lower().startswith("boulder ") and c.strip().lower() != "boulder":
        c = c.split(" ", 1)[1].strip()

    if c.lower().endswith(" hips") and "namba hips" in ev:
        c = re.sub(r"\s+hips\b", "", c, flags=re.IGNORECASE).strip()

    if country == "CHN" and re.search(r"\bprovince\b\s*$", c, flags=re.IGNORECASE):
        c = re.sub(r"\s+province\s*$", "", c, flags=re.IGNORECASE).strip()

    if country == "USA":
        c2 = _strip_us_state_suffix(c)
        if c2:
            c = c2

    c = tidy_case(c).strip()
    return c or None


def finalize_city(city_raw: str, country: Optional[str], event_name: str) -> Optional[str]:
    return postprocess_city(clean_city(city_raw), country, event_name)


def extract_tail_location(prefix: str) -> Optional[str]:
    """Tail extraction when separators are missing: 'IFSC Asian Cup Hong Kong' → 'Hong Kong'."""
    words = [w for w in prefix.split() if w.strip()]
    tail: list[str] = []
    for w in reversed(words):
        w_clean = re.sub(r"^[^\w]+|[^\w]+$", "", w)
        if not w_clean:
            continue
        wl = w_clean.lower()
        if wl in STOPWORDS:
            continue
        if wl in KEYWORDS_WORDS:
            break
        if re.fullmatch(r"(19|20)\d{2}", wl):
            continue
        tail.append(w_clean)
        if len(tail) >= 4:
            break
    if not tail:
        return None
    return " ".join(reversed(tail)).strip()


def _cut_after_keywords(text: str) -> str:
    last_end = None
    for mm in EVENT_KEYWORD_RE.finditer(text):
        last_end = mm.end()
    if last_end is not None and last_end < len(text):
        rest = text[last_end:].strip(" -,:;").strip()
        if rest and any(ch.isalpha() for ch in rest):
            return rest
    return text


def clean_city(city_raw: str) -> Optional[str]:
    """Normalize/clean a raw city candidate; return None if not city-like."""
    if not city_raw:
        return None

    c = city_raw
    c = DISCIPLINE_BLOCK_RE.sub("", c)
    c = DISCIPLINE_PAREN_RE.sub("", c)
    c = " ".join(c.split()).strip().strip('"\'')

    c = re.sub(r"^\d+\s*[\.\)]\s*", "", c)
    c = re.sub(r"^(?:\d+(?:st|nd|rd|th))\s+", "", c, flags=re.IGNORECASE)

    m = re.match(r"^The\s+Rock\s+(.+)$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()
    m = re.match(r"^(.+?)\s+Natural\s+Games$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()

    c = re.sub(r"^AREA\s*47\s+", "", c, flags=re.IGNORECASE)

    m = re.search(r"Citt[àa][\'’]?\s*di\s*(.+)$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()

    c = c.strip().strip(",;:-").strip()

    years = list(YEAR_RE.finditer(c))
    if years:
        last = years[-1]
        tail = c[last.end():].strip(" ,;:-").strip()
        if tail and any(ch.isalpha() for ch in tail):
            c = tail

    c = _cut_after_keywords(c)

    m_end = END_KEYWORD_RE.search(c)
    if m_end:
        prefix = c[:m_end.start()].strip(" ,;:-").strip()
        if prefix and any(ch.isalpha() for ch in prefix):
            c = prefix
    c = _cut_after_keywords(c)

    words = c.split()
    noise_edge = {
        "ifsc", "uiaa", "climbing", "world", "cup", "worldcup", "championship",
        "championships", "series", "open", "masters", "festival", "event",
        "international", "internationals", "competition", "competitions",
        "youth", "junior", "senior", "days", "trophy", "x-games", "xgames",
        "espn", "rockstars", "rockstar", "int", "intl",
    }
    changed = True
    while changed and words:
        changed = False
        w0 = re.sub(r"^[^\w]+|[^\w]+$", "", words[0]).lower()
        w1 = re.sub(r"^[^\w]+|[^\w]+$", "", words[-1]).lower()
        if w0 in noise_edge:
            words = words[1:]
            changed = True
            continue
        if w1 in noise_edge:
            words = words[:-1]
            changed = True
            continue
    c = " ".join(words).strip().strip(",;:-").strip()

    c = re.sub(r"\bX-?Games\b\.?$", "", c, flags=re.IGNORECASE).strip()
    c = re.sub(r"\bESPN\b\.?$", "", c, flags=re.IGNORECASE).strip()
    c = c.strip(" ,;:-").strip()

    c = tidy_case(c)

    if not c:
        return None
    cl = c.lower()
    if any(sub in cl for sub in BLACKLIST_CITY_SUBSTR):
        return None
    if cl in {"republic of korea", "korea", "china"}:
        return None
    if "(" in c or ")" in c:
        return None
    if not any(ch.isalpha() for ch in c):
        return None
    if re.fullmatch(r"[A-Z]{3}", c) and _is_known_iso3(c):
        return None

    return c


def _last_match(pattern: re.Pattern[str], text: str) -> Optional[re.Match[str]]:
    last = None
    for match in pattern.finditer(text):
        last = match
    return last


def _city_from_left_segment(left: str) -> str:
    matches = list(SEP_RE.finditer(left))
    city_raw = ""
    for i in range(len(matches) - 1, -1, -1):
        m = matches[i]
        chunk = left[m.end():].strip().strip(" ,;:-")
        if not chunk:
            continue

        if chunk.lower() in VENUE_CHUNKS and i - 1 >= 0:
            seg_start = matches[i - 1].end()
            seg_end = m.start()
            prev = left[seg_start:seg_end].strip().strip(" ,;:-")
            if prev:
                return prev
            continue

        if re.fullmatch(r"[A-Z]{2,3}", chunk) and chunk in REGION_ABBREV and i - 1 >= 0:
            seg_start = matches[i - 1].end()
            seg_end = m.start()
            prev = left[seg_start:seg_end].strip().strip(" ,;:-")
            if prev:
                return prev
            continue

        return chunk

    return city_raw


# --- Public API -----------------------------------------------------------

def parse_city_country(event_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (city, country_iso3) from an event name.

    Returns (None, None) if not confidently extractable.
    """
    s = " ".join(str(event_name).split())

    anchor: Optional[tuple[int, int]] = None
    country: Optional[str] = None

    m_iso = _last_match(COUNTRY_ISO3_PAREN_RE, s)
    if m_iso:
        country = m_iso.group(1)
        anchor = (m_iso.start(), m_iso.end())
    else:
        m_iso2 = _last_match(COUNTRY_ISO3_RPAREN_RE, s)
        if m_iso2:
            iso = m_iso2.group(1)
            if _is_known_iso3(iso):
                country = iso
                anchor = (m_iso2.start(), m_iso2.end())

    if anchor is None:
        m_name = _last_match(PAREN_CONTENT_RE, s)
        if m_name:
            iso3 = country_name_to_iso3_safe(m_name.group(1))
            if iso3:
                country = iso3
                anchor = (m_name.start(), m_name.end())

    if anchor is not None:
        left = DISCIPLINE_BLOCK_RE.sub("", s[:anchor[0]].rstrip())
        city_raw = _city_from_left_segment(left)
        return finalize_city(city_raw, country, s), country

    m_year = _last_match(YEAR_RE, s)
    if m_year:
        left = DISCIPLINE_BLOCK_RE.sub("", s[:m_year.start()].rstrip())
        matches = list(SEP_RE.finditer(left))
        if not matches:
            return None, None

        for i in range(len(matches) - 1, -1, -1):
            m = matches[i]
            after = left[m.end():].strip().strip(" ,;:-")
            if not after:
                continue

            maybe_country = looks_like_country_token(after)
            if maybe_country:
                if i - 1 >= 0:
                    seg_start = matches[i - 1].end()
                    seg_end = m.start()
                    city_part = left[seg_start:seg_end].strip().strip(" ,;:-")
                    return finalize_city(city_part, maybe_country, s), maybe_country
                prefix = left[:m.start()].strip()
                tail = extract_tail_location(prefix)
                return finalize_city(tail or "", maybe_country, s), maybe_country

            if re.fullmatch(r"[A-Z]{2,3}", after) and i - 1 >= 0:
                seg_start = matches[i - 1].end()
                seg_end = m.start()
                city_part = left[seg_start:seg_end].strip().strip(" ,;:-")
                return finalize_city(city_part, None, s), None

            return finalize_city(after, None, s), None

    return None, None
