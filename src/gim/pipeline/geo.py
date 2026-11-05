"""Lightweight place-name tagging: which countries / regions does a text mention?

Deliberately simple (a gazetteer + word-boundary matching) and transparent.
It answers "which places are MENTIONED", which is not always "where it
happened" -- e.g. "Russia condemns US strike in Yemen" mentions three
countries. Event geolocation proper comes with structured datasets (ACLED,
UCDP) and AI extraction in Phase 3.
"""
from __future__ import annotations

import re
from functools import lru_cache

# region key -> {canonical country name: (aliases incl. capital cities)}
GAZETTEER: dict[str, dict[str, tuple[str, ...]]] = {
    "europe": {
        "Ukraine": ("Ukrainian", "Kyiv", "Kiev"), "Russia": ("Russian", "Moscow", "Kremlin"),
        "Belarus": ("Belarusian", "Minsk"), "Moldova": ("Chisinau", "Transnistria"),
        "Germany": ("German", "Berlin"), "France": ("French", "Paris", "Elysee"),
        "United Kingdom": ("UK", "Britain", "British", "London"), "Poland": ("Polish", "Warsaw"),
        "Italy": ("Italian", "Rome"), "Spain": ("Spanish", "Madrid"), "Hungary": ("Hungarian", "Budapest"),
        "Romania": ("Romanian", "Bucharest"), "Serbia": ("Serbian", "Belgrade"), "Kosovo": ("Pristina",),
        "Bosnia and Herzegovina": ("Bosnia", "Sarajevo"), "Georgia": ("Tbilisi",),
        "Armenia": ("Armenian", "Yerevan"), "Azerbaijan": ("Azerbaijani", "Baku"),
        "Turkey": ("Turkiye", "Turkish", "Ankara"), "Greece": ("Greek", "Athens"),
        "Netherlands": ("Dutch", "The Hague"), "Belgium": ("Brussels",), "Sweden": ("Swedish", "Stockholm"),
        "Finland": ("Finnish", "Helsinki"), "Norway": ("Norwegian", "Oslo"), "Denmark": ("Danish", "Copenhagen"),
        "Estonia": ("Tallinn",), "Latvia": ("Riga",), "Lithuania": ("Vilnius",),
        "Czech Republic": ("Czechia", "Prague"), "Slovakia": ("Bratislava",), "Austria": ("Vienna",),
        "Switzerland": ("Swiss", "Geneva", "Bern"), "Ireland": ("Dublin",), "Portugal": ("Lisbon",),
        "Bulgaria": ("Sofia",), "Croatia": ("Zagreb",), "Albania": ("Tirana",),
        "North Macedonia": ("Skopje",), "Montenegro": ("Podgorica",), "Cyprus": ("Nicosia",),
    },
    "middle_east": {
        "Iran": ("Iranian", "Tehran"), "Israel": ("Israeli", "Jerusalem", "Tel Aviv"),
        "Palestine": ("Palestinian", "Gaza", "West Bank", "Ramallah"), "Lebanon": ("Lebanese", "Beirut", "Hezbollah"),
        "Syria": ("Syrian", "Damascus"), "Iraq": ("Iraqi", "Baghdad"), "Saudi Arabia": ("Saudi", "Riyadh"),
        "Yemen": ("Yemeni", "Sanaa", "Houthi", "Houthis"), "Jordan": ("Jordanian", "Amman"),
        "Egypt": ("Egyptian", "Cairo"), "Qatar": ("Qatari", "Doha"),
        "United Arab Emirates": ("UAE", "Emirati", "Abu Dhabi", "Dubai"), "Kuwait": (),
        "Oman": ("Muscat",), "Bahrain": ("Manama",),
    },
    "africa": {
        "Sudan": ("Sudanese", "Khartoum", "Darfur"), "South Sudan": ("Juba",),
        "Ethiopia": ("Ethiopian", "Addis Ababa", "Tigray"), "Eritrea": ("Asmara",),
        "Somalia": ("Somali", "Mogadishu", "Al-Shabaab"), "Kenya": ("Kenyan", "Nairobi"),
        "Mali": ("Malian", "Bamako"), "Burkina Faso": ("Ouagadougou",), "Niger": ("Niamey",),
        "Nigeria": ("Nigerian", "Abuja"), "Chad": ("Chadian", "N'Djamena"),
        "Democratic Republic of the Congo": ("DRC", "DR Congo", "Kinshasa", "Goma"),
        "Libya": ("Libyan", "Tripoli", "Benghazi"), "Algeria": ("Algerian", "Algiers"),
        "Morocco": ("Moroccan", "Rabat"), "Tunisia": ("Tunis",), "Senegal": ("Dakar",),
        "Ghana": ("Accra",), "Cameroon": ("Yaounde",), "Mozambique": ("Maputo", "Cabo Delgado"),
        "South Africa": ("Pretoria", "Johannesburg"), "Uganda": ("Kampala",), "Rwanda": ("Kigali",),
        "Central African Republic": ("Bangui",), "Zimbabwe": ("Harare",), "Guinea": ("Conakry",),
    },
    "asia_pacific": {
        "China": ("Chinese", "Beijing"), "Taiwan": ("Taiwanese", "Taipei"), "India": ("Indian", "New Delhi"),
        "Pakistan": ("Pakistani", "Islamabad"), "Afghanistan": ("Afghan", "Kabul", "Taliban"),
        "Japan": ("Japanese", "Tokyo"), "North Korea": ("Pyongyang", "DPRK"),
        "South Korea": ("Seoul",), "Philippines": ("Philippine", "Manila"), "Vietnam": ("Vietnamese", "Hanoi"),
        "Indonesia": ("Indonesian", "Jakarta"), "Malaysia": ("Kuala Lumpur",), "Thailand": ("Thai", "Bangkok"),
        "Myanmar": ("Burma", "Naypyidaw"), "Bangladesh": ("Dhaka",), "Sri Lanka": ("Colombo",),
        "Nepal": ("Kathmandu",), "Cambodia": ("Phnom Penh",), "Australia": ("Australian", "Canberra"),
        "New Zealand": ("Wellington",), "Kazakhstan": ("Astana",), "Uzbekistan": ("Tashkent",),
        "Mongolia": ("Ulaanbaatar",), "Singapore": (),
    },
    "americas": {
        "United States": ("US", "U.S.", "USA", "American", "White House", "Pentagon"),
        "Canada": ("Canadian", "Ottawa"), "Mexico": ("Mexican", "Mexico City"), "Brazil": ("Brazilian", "Brasilia"),
        "Argentina": ("Argentine", "Buenos Aires"), "Venezuela": ("Venezuelan", "Caracas"),
        "Colombia": ("Colombian", "Bogota"), "Cuba": ("Cuban", "Havana"), "Haiti": ("Haitian", "Port-au-Prince"),
        "Peru": ("Peruvian", "Lima"), "Chile": ("Chilean", "Santiago"), "Bolivia": ("La Paz",),
        "Ecuador": ("Quito",), "Nicaragua": ("Managua",), "Panama": ("Panama Canal",),
        "Guatemala": (), "Honduras": (), "El Salvador": (),
    },
}

# Organisation / area keywords that indicate a region without naming a country.
REGION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "europe": ("European Union", "NATO", "OSCE", "Council of Europe", "Balkans", "Black Sea", "Baltic"),
    "middle_east": ("Middle East", "Gulf states", "Red Sea", "Strait of Hormuz"),
    "africa": ("Sahel", "African Union", "ECOWAS", "Horn of Africa", "Great Lakes region", "SADC"),
    "asia_pacific": ("South China Sea", "ASEAN", "Indo-Pacific", "Korean Peninsula", "Taiwan Strait"),
    "americas": ("Latin America", "Caribbean", "Organization of American States", "Mercosur"),
}


@lru_cache(maxsize=1)
def _patterns() -> list[tuple[re.Pattern, str, str | None]]:
    """(pattern, region, canonical country or None). Longest names first."""
    entries: list[tuple[str, str, str | None]] = []
    for region, countries in GAZETTEER.items():
        for country, aliases in countries.items():
            entries.append((country, region, country))
            entries.extend((a, region, country) for a in aliases)
    for region, kws in REGION_KEYWORDS.items():
        entries.extend((k, region, None) for k in kws)
    entries.sort(key=lambda e: -len(e[0]))
    out = []
    for name, region, country in entries:
        # Short all-caps aliases (US, UK, UAE, DRC) are matched case-sensitively to avoid "us".
        flags = 0 if (name.isupper() and len(name) <= 4) or name == "U.S." else re.IGNORECASE
        out.append((re.compile(r"(?<![\w.])" + re.escape(name) + r"(?![\w])", flags), region, country))
    return out


def tag_places(text: str) -> tuple[list[str], list[str]]:
    """Return (countries, regions) mentioned in ``text``, in order of first mention."""
    country_hits: dict[str, int] = {}
    region_hits: dict[str, int] = {}
    consumed = text
    for pattern, region, country in _patterns():
        match = pattern.search(consumed)
        if not match:
            continue
        # blank out the match so "South Sudan" does not also count as "Sudan"
        consumed = pattern.sub(lambda m: " " * len(m.group(0)), consumed)
        pos = match.start()
        if country:
            country_hits[country] = min(pos, country_hits.get(country, pos))
        region_hits[region] = min(pos, region_hits.get(region, pos))
    return (sorted(country_hits, key=country_hits.get), sorted(region_hits, key=region_hits.get))


# ISO 3166-1 alpha-3 codes for the canonical names above (used for maps; Kosovo uses the
# commonly used user-assigned code XKX). Country-level codes shade a whole country -- they
# never imply a point location.
ISO3: dict[str, str] = {
    "Ukraine": "UKR", "Russia": "RUS", "Belarus": "BLR", "Moldova": "MDA", "Germany": "DEU", "France": "FRA",
    "United Kingdom": "GBR", "Poland": "POL", "Italy": "ITA", "Spain": "ESP", "Hungary": "HUN", "Romania": "ROU",
    "Serbia": "SRB", "Kosovo": "XKX", "Bosnia and Herzegovina": "BIH", "Georgia": "GEO", "Armenia": "ARM",
    "Azerbaijan": "AZE", "Turkey": "TUR", "Greece": "GRC", "Netherlands": "NLD", "Belgium": "BEL", "Sweden": "SWE",
    "Finland": "FIN", "Norway": "NOR", "Denmark": "DNK", "Estonia": "EST", "Latvia": "LVA", "Lithuania": "LTU",
    "Czech Republic": "CZE", "Slovakia": "SVK", "Austria": "AUT", "Switzerland": "CHE", "Ireland": "IRL",
    "Portugal": "PRT", "Bulgaria": "BGR", "Croatia": "HRV", "Albania": "ALB", "North Macedonia": "MKD",
    "Montenegro": "MNE", "Cyprus": "CYP", "Iran": "IRN", "Israel": "ISR", "Palestine": "PSE", "Lebanon": "LBN",
    "Syria": "SYR", "Iraq": "IRQ", "Saudi Arabia": "SAU", "Yemen": "YEM", "Jordan": "JOR", "Egypt": "EGY",
    "Qatar": "QAT", "United Arab Emirates": "ARE", "Kuwait": "KWT", "Oman": "OMN", "Bahrain": "BHR",
    "Sudan": "SDN", "South Sudan": "SSD", "Ethiopia": "ETH", "Eritrea": "ERI", "Somalia": "SOM", "Kenya": "KEN",
    "Mali": "MLI", "Burkina Faso": "BFA", "Niger": "NER", "Nigeria": "NGA", "Chad": "TCD",
    "Democratic Republic of the Congo": "COD", "Libya": "LBY", "Algeria": "DZA", "Morocco": "MAR",
    "Tunisia": "TUN", "Senegal": "SEN", "Ghana": "GHA", "Cameroon": "CMR", "Mozambique": "MOZ",
    "South Africa": "ZAF", "Uganda": "UGA", "Rwanda": "RWA", "Central African Republic": "CAF",
    "Zimbabwe": "ZWE", "Guinea": "GIN", "China": "CHN", "Taiwan": "TWN", "India": "IND", "Pakistan": "PAK",
    "Afghanistan": "AFG", "Japan": "JPN", "North Korea": "PRK", "South Korea": "KOR", "Philippines": "PHL",
    "Vietnam": "VNM", "Indonesia": "IDN", "Malaysia": "MYS", "Thailand": "THA", "Myanmar": "MMR",
    "Bangladesh": "BGD", "Sri Lanka": "LKA", "Nepal": "NPL", "Cambodia": "KHM", "Australia": "AUS",
    "New Zealand": "NZL", "Kazakhstan": "KAZ", "Uzbekistan": "UZB", "Mongolia": "MNG", "Singapore": "SGP",
    "United States": "USA", "Canada": "CAN", "Mexico": "MEX", "Brazil": "BRA", "Argentina": "ARG",
    "Venezuela": "VEN", "Colombia": "COL", "Cuba": "CUB", "Haiti": "HTI", "Peru": "PER", "Chile": "CHL",
    "Bolivia": "BOL", "Ecuador": "ECU", "Nicaragua": "NIC", "Panama": "PAN", "Guatemala": "GTM",
    "Honduras": "HND", "El Salvador": "SLV",
}
COUNTRY_REGION: dict[str, str] = {c: r for r, cs in GAZETTEER.items() for c in cs}


def all_countries() -> list[str]:
    return sorted(COUNTRY_REGION)
