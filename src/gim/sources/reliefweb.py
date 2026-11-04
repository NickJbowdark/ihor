"""ReliefWeb API v2 connector (UN OCHA) -- humanitarian reports.

Access: a PRE-APPROVED appname is mandatory (request it at
https://apidoc.reliefweb.int/). Put it in .env as GIM_RELIEFWEB_APPNAME.
Without it this connector reports status 'needs_credentials' and does nothing.

Provenance: ReliefWeb *republishes* reports written by other organisations
(OCHA, UNHCR, WFP, NGOs, governments). We therefore record
publisher = ReliefWeb and original_reporter = the authoring organisation(s).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from gim.models import Document
from gim.pipeline.classify import classify_title
from gim.pipeline.geo import tag_places
from gim.pipeline.normalize import canonicalize_url, clean_text, is_safe_url, title_fingerprint
from gim.sources.base import Connector, FetchResult, FetchState, ParseError

log = logging.getLogger(__name__)

PAGE_LIMIT = 100
FIELDS = [
    "title", "url", "url_alias", "date.created", "date.original", "date.changed",
    "source.name", "source.shortname", "country.name", "primary_country.name",
    "language.code", "format.name", "body",
]
# ReliefWeb 'format' -> GIM doc_type
FORMAT_DOC_TYPE = {
    "Analysis": "analysis_opinion",
    "Assessment": "analysis_opinion",
    "Evaluation and Lessons Learned": "analysis_opinion",
    "Manual and Guideline": "analysis_opinion",
    "News and Press Release": "official_statement",
    "Situation Report": "official_statement",
    "Appeal": "official_statement",
    "UN Document": "official_statement",
    "Map": "dataset_record",
    "Infographic": "dataset_record",
}


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _names(items) -> list[str]:
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [clean_text(i.get("name") or i.get("shortname"), 200) for i in items if isinstance(i, dict)]


def parse_reports(data: dict, source_id: str = "reliefweb_reports", category: str = "humanitarian",
                  default_doc_type: str = "official_statement") -> list[Document]:
    if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
        raise ParseError("unexpected ReliefWeb response shape")
    docs: list[Document] = []
    for item in data.get("data", []):
        f = item.get("fields") if isinstance(item, dict) else None
        if not isinstance(f, dict):
            continue
        url = (f.get("url_alias") or f.get("url") or "").strip()
        title = clean_text(f.get("title"))
        if not (is_safe_url(url) and title):
            continue
        dates = f.get("date") or {}
        formats = _names(f.get("format"))
        doc_type = next((FORMAT_DOC_TYPE[x] for x in formats if x in FORMAT_DOC_TYPE), default_doc_type)
        authors = _names(f.get("source"))
        country_names = _names(f.get("country"))
        summary = clean_text(f.get("body"), 600)
        countries, regions = tag_places(" ; ".join(country_names) + " ; " + title)
        langs = f.get("language") or []
        lang = langs[0].get("code", "") if isinstance(langs, list) and langs and isinstance(langs[0], dict) else ""
        published = _parse_dt(dates.get("original")) or _parse_dt(dates.get("created"))
        docs.append(Document(
            source_id=source_id,
            url=url,
            canonical_url=canonicalize_url(url),
            title=title,
            publisher="ReliefWeb (UN OCHA)",
            source_category=category,
            doc_type=doc_type,
            language=clean_text(lang, 10).lower(),
            published_at=published,
            updated_at=_parse_dt(dates.get("changed")),
            summary=summary,
            original_reporter="; ".join(authors),
            external_id=str(item.get("id", "")),
            countries=countries or country_names[:5],
            regions=regions or ["global"],
            categories=sorted(set(classify_title(f"{title}. {summary}")) | {"humanitarian"}),
            title_fingerprint=title_fingerprint(title),
            provenance={"connector": "reliefweb", "formats": formats,
                        "reliefweb_countries": country_names,
                        "posted_on_reliefweb": dates.get("created")},
        ))
    return docs


class ReliefWebConnector(Connector):
    def fetch(self, state: FetchState) -> FetchResult:
        self.check_credentials()
        appname = os.environ["GIM_RELIEFWEB_APPNAME"].strip()
        # Look back to the last success (minus overlap), at most 7 days on a first run.
        since = (state.last_success_at or datetime.now(timezone.utc) - timedelta(days=7)) - timedelta(hours=2)
        payload = {
            "limit": PAGE_LIMIT,
            "sort": ["date.created:desc"],
            "fields": {"include": FIELDS},
            "filter": {"field": "date.created", "value": {"from": since.isoformat(timespec="seconds")}},
        }
        resp = self.http.post(self.spec.url, params={"appname": appname}, json=payload)
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise ParseError("ReliefWeb returned non-JSON") from None
        result = FetchResult(raw=[("reports.json", resp.body)])
        result.documents = parse_reports(data, self.spec.id, self.spec.category, self.spec.doc_type)
        total = data.get("totalCount")
        if isinstance(total, int) and total > PAGE_LIMIT:
            result.notes.append(f"{total} reports matched; fetched newest {PAGE_LIMIT} (next run continues)")
        return result
