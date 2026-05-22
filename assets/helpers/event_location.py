"""
Extract city and country (ISO-3166 alpha-3) from IFSC event names.

Design goals
- Be conservative: if city/country can't be extracted from the name, return None.
- Prefer explicit signals only:
    * country as "(FRA)" or " CHN)" (broken parenthesis) -> ISO3
    * country name in parentheses "(France)" -> mapped to ISO3 (safe mapping)
    * country token before a year "..., USA 1997" -> ISO3
- Avoid "guessing": we strip common event keywords and reject known non-locations.

Usage (CLI)
    python event_location_improved.py --input Events.csv --output Events_improved.csv

The output CSV will contain the same columns as input, but with improved 'city' and 'country'.
"""

from __future__ import annotations

import argparse
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

# Region/state abbreviations sometimes appear after a comma before the country anchor,
# e.g. "Port Maquarie, NSW (AUS)". If we extract only the tail chunk we may get "NSW".
# We only treat these as non-city tokens when they are in a known, small allowlist.
REGION_ABBREV = {
    # Australia
    "NSW", "QLD", "VIC", "TAS", "SA", "WA", "NT", "ACT",
}

VENUE_CHUNKS = {
    # Known venue tokens that are NOT cities in this dataset
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


# Event keywords (used to cut "World Cup ... <City>" => "<City>")
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

# Keywords at end of a string: "… Darvas Cup" => "Darvas"
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

# Reject if these appear anywhere in the extracted city string
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
    """Conservative mapping for country names -> ISO3."""
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

    # Strict-ish lookup first
    iso3 = _pycountry_lookup_safe(s)
    if iso3:
        return iso3

    # Allow fuzzy only for longer / more country-ish strings to avoid false positives (Paris -> France, etc.)
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
            return c.alpha_3
        except Exception:
            return None
    return None


def _is_known_iso3(code: str) -> bool:
    if pycountry is not None:
        return pycountry.countries.get(alpha_3=code) is not None
    return code in FALLBACK_ISO3_CODES


def looks_like_country_token(token: str) -> Optional[str]:
    """Return ISO3 if 'token' is clearly a country code/name; else None."""
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
    """If city ends with a US state name (e.g. 'Denver Colorado'), strip the state.
    Applied only when country is explicitly USA.
    """
    if not city or " " not in city:
        return city

    # Prefer multi-word states first (e.g. 'New York')
    words = city.split()
    for n in (2, 3):  # max state name length in words we care about
        if len(words) > n:
            candidate = " ".join(words[-n:])
            if candidate in US_STATE_NAMES:
                return " ".join(words[:-n]).strip()
    # Single-word states
    if words[-1] in US_STATE_NAMES:
        return " ".join(words[:-1]).strip()
    return city


def postprocess_city(city: Optional[str], country: Optional[str], event_name: str) -> Optional[str]:
    """Targeted, low-risk fixes for a handful of known patterns.
    If a fix could be ambiguous, we do NOT apply it.
    """
    if city is None:
        return None
    c = city.strip()
    if not c:
        return None

    ev = (event_name or "").lower()

    # Rock Junior is an event name, not a location.
    if "rock junior" in ev and c.lower() in {"rock", "rock junior"}:
        return None

    # 1) '... competition of Aggtelek' -> 'Aggtelek' (avoid touching 'La Paz', etc.)
    if c.lower().startswith("of "):
        c = c[3:].strip()

    # 2) Known event-prefix phrases that are not locations
    prefix_phrases = [
        "speed rock",
        "blocmaster",
        "bouldertag",
        "nikoloklettern",
        "demonstration",
        "triglav the rock",
        "copa aldea",
        "bouldertehdas",
        "master",  # e.g. 'Master WARSAW'
    ]
    for ph in prefix_phrases:
        if c.lower().startswith(ph + " "):
            c = c[len(ph) + 1 :].strip()
            break

    # 3) 'Boulder Montpellier' (discipline word) but keep city 'Boulder'
    if c.lower().startswith("boulder ") and c.strip().lower() != "boulder":
        c = c.split(" ", 1)[1].strip()

    # 4) 'namBa HIPS' -> 'namBa' only when the raw name clearly contains that token
    if c.lower().endswith(" hips") and "namba hips" in ev:
        c = re.sub(r"\s+hips\b", "", c, flags=re.IGNORECASE).strip()

    # 5) 'Qinghai Province' -> 'Qinghai' only when country is explicitly China (CHN)
    if country == "CHN" and re.search(r"\bprovince\b\s*$", c, flags=re.IGNORECASE):
        c = re.sub(r"\s+province\s*$", "", c, flags=re.IGNORECASE).strip()

    # 6) 'Denver Colorado' -> 'Denver' only when country is explicitly USA
    if country == "USA":
        c2 = _strip_us_state_suffix(c)
        if c2:
            c = c2

    c = tidy_case(c).strip()
    return c or None


def finalize_city(city_raw: str, country: Optional[str], event_name: str) -> Optional[str]:
    """Clean + targeted postprocessing."""
    return postprocess_city(clean_city(city_raw), country, event_name)

def extract_tail_location(prefix: str) -> Optional[str]:
    """
    Extract the tail of a prefix when we have "... <City>, <Country> <Year>" but no other separators.
    Example: "IFSC Asian Cup Hong Kong" -> "Hong Kong"
    """
    words = [w for w in prefix.split() if w.strip()]
    tail = []
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
    """Normalize/clean a raw city candidate; return None if it doesn't look like a location."""
    if not city_raw:
        return None

    c = city_raw
    c = DISCIPLINE_BLOCK_RE.sub("", c)
    c = DISCIPLINE_PAREN_RE.sub("", c)
    c = " ".join(c.split()).strip().strip('"\'')

    # Remove leading numbering like "3. BELGRADE ..."
    c = re.sub(r"^\d+\s*[\.\)]\s*", "", c)
    c = re.sub(r"^(?:\d+(?:st|nd|rd|th))\s+", "", c, flags=re.IGNORECASE)

    # Special patterns
    m = re.match(r"^The\s+Rock\s+(.+)$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()
    m = re.match(r"^(.+?)\s+Natural\s+Games$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()

    # Common venue prefix
    c = re.sub(r"^AREA\s*47\s+", "", c, flags=re.IGNORECASE)

    # "Città di X"
    m = re.search(r"Citt[àa][\'’]?\s*di\s*(.+)$", c, re.IGNORECASE)
    if m:
        c = m.group(1).strip()

    c = c.strip().strip(",;:-").strip()

    # If there's a year inside, keep tail after the last year (often "... 2012 Huaraz")
    years = list(YEAR_RE.finditer(c))
    if years:
        last = years[-1]
        tail = c[last.end():].strip(" ,;:-").strip()
        if tail and any(ch.isalpha() for ch in tail):
            c = tail

    # Cut after last event keyword if it leaves something meaningful
    c = _cut_after_keywords(c)

    # If ends with a keyword (and no rest), take prefix before keyword, then cut again
    m_end = END_KEYWORD_RE.search(c)
    if m_end:
        prefix = c[:m_end.start()].strip(" ,;:-").strip()
        if prefix and any(ch.isalpha() for ch in prefix):
            c = prefix
    c = _cut_after_keywords(c)

    # Strip edge noise words
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

    # Remove trailing ESPN / X-Games if still present
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
    # If it's exactly an ISO3 country code, it's not a city
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
    """
    Extract (city, country_iso3) from an event name.
    Country is ISO3 (e.g. "FRA"). If not confidently extractable, returns None.
    """
    s = " ".join(str(event_name).split())

    # 1) Country explicit: "(FRA)" or broken " CHN)"
    anchor = None
    country = None

    m_iso = _last_match(COUNTRY_ISO3_PAREN_RE, s)
    if m_iso:
        country = m_iso.group(1)
        anchor = (m_iso.start(), m_iso.end())
    else:
        m_iso2 = _last_match(COUNTRY_ISO3_RPAREN_RE, s)
        if m_iso2:
            iso = m_iso2.group(1)
            # Only accept if it's a real ISO3 country code.
            if _is_known_iso3(iso):
                country = iso
                anchor = (m_iso2.start(), m_iso2.end())

    # 2) Country name in parentheses "(France)"
    if anchor is None:
        m_name = _last_match(PAREN_CONTENT_RE, s)
        if m_name:
            iso3 = country_name_to_iso3_safe(m_name.group(1))
            if iso3:
                country = iso3
                anchor = (m_name.start(), m_name.end())

    # City extraction (country anchor case)
    if anchor is not None:
        left = DISCIPLINE_BLOCK_RE.sub("", s[:anchor[0]].rstrip())
        city_raw = _city_from_left_segment(left)
        return finalize_city(city_raw, country, s), country
# 3) No country. Use last year as an anchor, and try patterns like "... <City>, USA 1997"
    m_year = _last_match(YEAR_RE, s)
    if m_year:
        left = DISCIPLINE_BLOCK_RE.sub("", s[:m_year.start()].rstrip())
        matches = list(SEP_RE.finditer(left))
        if not matches:
            return None, None

        # Walk separators from the end to find "<city> <country>"
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

                # No earlier separator: extract tail of the prefix
                prefix = left[:m.start()].strip()
                tail = extract_tail_location(prefix)
                return finalize_city(tail or "", maybe_country, s), maybe_country

            # State/province abbreviation (e.g. "NSW"): keep previous segment as city
            if re.fullmatch(r"[A-Z]{2,3}", after) and i - 1 >= 0:
                seg_start = matches[i - 1].end()
                seg_end = m.start()
                city_part = left[seg_start:seg_end].strip().strip(" ,;:-")
                return finalize_city(city_part, None, s), None

            # Otherwise: last chunk is the city
            return finalize_city(after, None, s), None

    return None, None


# --- CLI ------------------------------------------------------------------

def main() -> int:
    import csv

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Input CSV containing a 'name' column.")
    ap.add_argument("--output", required=True, help="Output CSV path.")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8", newline="") as f_in:
        reader = csv.DictReader(f_in)
        if "name" not in (reader.fieldnames or []):
            raise SystemExit("Input CSV must contain a 'name' column.")
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    # Ensure output has city/country columns
    if "city" not in fieldnames:
        fieldnames.append("city")
    if "country" not in fieldnames:
        fieldnames.append("country")

    for r in rows:
        city, country = parse_city_country(r.get("name", ""))
        r["city"] = city
        r["country"] = country

    with open(args.output, "w", encoding="utf-8", newline="") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
