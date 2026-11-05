"""Turn report content into an ordered list of BLOCKS, then into Markdown (UI) or PDF.

Both outputs share the same block list, so the PDF and the on-screen report can
never diverge. Citations: every DOC id becomes a bibliography number [n].
Claim types are always visible to the reader:
  REPORTED_FACT     plain text + citations
  ATTRIBUTED_CLAIM  "... — according to <actor> [n]"
  ASSESSMENT        "Assessment (moderate confidence): ... [n]"
  CONTEXT           "Background (model knowledge, unverified): ..."
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from gim.analysis.contracts import EVENT_SECTIONS

SECTION_TITLES = {
    "executive_summary": "Executive summary", "regional_overview": "Regional overview",
    "trends": "Trends and patterns", "implications": "Implications", "indicators": "Indicators to watch",
    "uncertainty": "Key uncertainties and intelligence gaps", "significance": "Why it matters",
    "key_developments": "Key developments", "actors": "Actors and documented positions",
    "background": "Background", "source_comparison": "Source comparison",
}
LEVEL_SHORT = {"multiple_origins": "multiple origins", "official_plus_reporting": "official + 1 origin",
               "official_only": "official only", "single_origin": "single origin"}
DOC_TYPE_LABEL = {"original_reporting": "original reporting", "syndicated": "syndicated copy",
                  "official_statement": "official statement", "dataset_record": "dataset record",
                  "analysis_opinion": "analysis/opinion", "unknown": "origin unknown"}


@dataclass
class Block:
    kind: str           # title meta banner h1 h2 h3 para claim bullet table source note
    text: str = ""
    data: dict = field(default_factory=dict)


class Citations:
    def __init__(self, documents: dict[str, dict]):
        self.documents = documents
        self.numbers: dict[str, int] = {}

    def number(self, ref: str) -> int | None:
        base = ref.split("#", 1)[0]
        if base not in self.documents:
            return None
        if base not in self.numbers:
            self.numbers[base] = len(self.numbers) + 1
        return self.numbers[base]

    def marks(self, refs: list[str]) -> str:
        nums = sorted({n for n in (self.number(r) for r in refs or []) if n})
        return "".join(f"[{n}]" for n in nums)


def claim_text(c: dict, cites: Citations) -> tuple[str, str]:
    """(label, sentence-with-citations)."""
    marks = cites.marks(c.get("source_refs") or [])
    t = c["claim_type"]
    text = c["text"].strip()
    if t == "ATTRIBUTED_CLAIM":
        who = c.get("attributed_to") or "source"
        if who.lower() not in text.lower():
            text = f"{text} — according to {who}"
        return "", f"{text} {marks}".strip()
    if t == "ASSESSMENT":
        return f"Assessment ({c.get('confidence') or '?'} confidence)", f"{text} {marks}".strip()
    if t == "CONTEXT":
        return "Background (model knowledge, unverified)", text
    return "", f"{text} {marks}".strip()


def build_blocks(content: dict) -> list[Block]:
    cites = Citations(content.get("documents", {}))
    b: list[Block] = []
    status = content.get("status", "draft").upper()
    b.append(Block("title", content["title"]))
    b.append(Block("meta", data={"lines": [
        f"{content['report_name']} · {status}",
        f"Scope: {content['scope_text']} · Period: {content['window_start'][:16]} – {content['window_end'][:16]} UTC",
        f"Generated {content['generated_at'][:16]} UTC by GIM · analysis: {content['provider']} / {content['model']}"
        f" · prompts {content['prompt_version']}",
    ]}))
    if content.get("data_origin") != "live":
        b.append(Block("banner", "SYNTHETIC TEST DATA — NOT REAL INTELLIGENCE. Generated from labelled fixtures."))
    if content["provider"] == "baseline":
        b.append(Block("banner", "No AI analysis was used: attributed source statements and statistics only."))

    synth = content.get("synthesis", [])
    shown: set[int] = set()

    # Key judgments: the analytic bottom line (assessments with confidence), strongest first.
    order = {"high": 0, "moderate": 1, "low": 2}
    judgments = [(i, c) for i, c in enumerate(synth) if c.get("claim_type") == "ASSESSMENT"
                 and c.get("section") in ("executive_summary", "implications", "trends")]
    judgments.sort(key=lambda ic: (order.get(ic[1].get("confidence") or "", 3), ic[0]))
    if judgments:
        b.append(Block("h1", "Key judgments"))
        for i, c in judgments[:6]:
            shown.add(i)
            label, sentence = claim_text(c, cites)
            b.append(Block("claim", sentence, {"label": label, "type": c["claim_type"]}))

    def section(name: str, level: str = "h1") -> None:
        claims = [c for i, c in enumerate(synth) if c.get("section") == name and i not in shown]
        if not claims:
            return
        b.append(Block(level, SECTION_TITLES.get(name, name)))
        for c in claims:
            label, sentence = claim_text(c, cites)
            b.append(Block("claim", sentence, {"label": label, "type": c["claim_type"]}))

    section("executive_summary")
    section("regional_overview")
    section("trends")

    if content["events"]:
        b.append(Block("h1", "Events covered"))
        b.append(Block("table", data={"headers": ["Event", "First reported (UTC)", "Headline", "Docs", "Sources"],
                                      "widths": (12, 18, 46, 7, 17), "rows": [
            [e["ref"], e["first_published_at"][:16].replace("T", " "), e["headline"][:110], e["documents_count"],
             LEVEL_SHORT.get(e.get("corroboration_level", ""), e["corroboration"][:40])] for e in content["events"]]}))
    if content.get("changes"):
        b.append(Block("h1", "Changes compared with the previous period"))
        b.append(Block("note", "Deterministic counts of events in the same scope; volume reflects attention, "
                               "not severity."))
        b.append(Block("table", data={"headers": ["Measure", "This period", "Previous", "Change"],
                                      "widths": (55, 15, 15, 15), "rows": content["changes"]}))

    b.append(Block("h1", "Event assessments"))
    for e in content["events"]:
        b.append(Block("h2", f"{e['ref']} · {e['headline']}"))
        b.append(Block("note", f"First reported {e['first_published_at'][:16]} UTC · documents {e['documents_count']} · "
                               f"countries mentioned: {(e['countries_mentioned'] or 'none').replace(',', ', ')} · "
                               f"event location: {e['event_location']} · prominence {e['prominence']}"))
        b.append(Block("note", f"Sources: {e['corroboration']}"))
        for name in EVENT_SECTIONS:
            claims = [c for c in e["claims"] if c.get("section") == name]
            if not claims:
                continue
            b.append(Block("h3", SECTION_TITLES.get(name, name)))
            for c in claims:
                label, sentence = claim_text(c, cites)
                b.append(Block("claim", sentence, {"label": label, "type": c["claim_type"]}))
        for q in e.get("open_questions", [])[:4]:
            b.append(Block("bullet", f"Open question: {q}"))
        uncited = [r for r in e["source_refs"] if r not in cites.numbers]
        if uncited:
            b.append(Block("note", "Further sources: " + cites.marks(uncited)))

    section("implications")
    section("indicators")
    section("uncertainty")
    if content.get("open_questions"):
        b.append(Block("h1", "Open questions"))
        for q in content["open_questions"]:
            b.append(Block("bullet", q))

    st = content["statistics"]
    b.append(Block("h1", "Statistics (deterministic, from the GIM database)"))
    b.append(Block("table", data={"headers": ["Measure", "Value"], "rows": [
        ["Events analysed", st["events_analysed"]], ["Documents", st["documents"]],
        ["Independent origins (sum over events)", st["independent_origins"]],
        ["Official origins (sum over events)", st["official_origins"]],
        ["Topics", ", ".join(f"{k} ({v})" for k, v in st["categories"]) or "—"],
        ["Countries mentioned", ", ".join(f"{k} ({v})" for k, v in st["countries_mentioned"][:10]) or "—"],
        ["Organisations mentioned", ", ".join(f"{k} ({v})" for k, v in st["organizations"]) or "—"],
        ["Corroboration levels", ", ".join(f"{k} ({v})" for k, v in st["corroboration_levels"]) or "—"],
    ]}))

    sb = content.get("source_base")
    if sb:
        b.append(Block("h1", "Source base"))
        b.append(Block("bullet", f"{sb['documents']} documents from {sb['publishers']} publishers; languages: "
                                 f"{', '.join(sb['languages']) or 'unknown'}."))
        b.append(Block("bullet", "Document types: " + ", ".join(f"{DOC_TYPE_LABEL.get(k, k)} {v}"
                                                                for k, v in sb["doc_types"])))
        if sb["official_institutions"]:
            b.append(Block("bullet", "Official statements by: " + ", ".join(f"{k} ({v})"
                                                                           for k, v in sb["official_institutions"])))
        b.append(Block("bullet", "Collected via: " + ", ".join(f"{k} ({v})" for k, v in sb["collection_sources"])))

    b.append(Block("h1", "Confidence and limitations"))
    for line in content["limitations"]:
        b.append(Block("bullet", line))

    b.append(Block("h1", "Sources"))
    docs = content["documents"]
    ordered = list(cites.numbers) + [r for r in docs if r not in cites.numbers]
    for ref in ordered:
        n = cites.number(ref)
        d = docs[ref]
        by = f" (originally {d['original_reporter']})" if d.get("original_reporter") and d["original_reporter"] != d["publisher"] else ""
        b.append(Block("source", f"[{n}] {d['publisher']}{by}: “{d['title']}” — {DOC_TYPE_LABEL.get(d['doc_type'], d['doc_type'])}, "
                                 f"{(d.get('published_at') or 'date unknown')[:16]} · {ref}",
                       {"url": d["url"], "n": n}))
    if content.get("rejected"):
        b.append(Block("h1", "Appendix: statements rejected by the evidence validator"))
        for r in content["rejected"][:40]:
            b.append(Block("bullet", f"“{r['claim'].get('text', '')[:200]}” — {'; '.join(r['problems'])}"))
    return b


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|$])")


def md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text or "")


def _md_cites(text: str) -> str:
    # keep our own [n] markers readable after escaping
    return re.sub(r"\\\[(\d+)\\\]", r"[\1]", md_escape(text))


def render_markdown(content: dict) -> str:
    out: list[str] = []
    for blk in build_blocks(content):
        k = blk.kind
        if k == "title":
            out.append(f"# {md_escape(blk.text)}")
        elif k == "meta":
            out += [f"*{md_escape(line)}*  " for line in blk.data["lines"]]
        elif k == "banner":
            out.append(f"\n> ⚠️ **{md_escape(blk.text)}**\n")
        elif k == "h1":
            out.append(f"\n## {md_escape(blk.text)}")
        elif k == "h2":
            out.append(f"\n### {md_escape(blk.text)}")
        elif k == "h3":
            out.append(f"\n**{md_escape(blk.text)}**")
        elif k == "note":
            out.append(f"<small>{_md_cites(blk.text)}</small>  " if False else f"_{_md_cites(blk.text)}_  ")
        elif k == "claim":
            label = f"**{md_escape(blk.data['label'])}:** " if blk.data.get("label") else ""
            out.append(f"- {label}{_md_cites(blk.text)}")
        elif k == "bullet":
            out.append(f"- {_md_cites(blk.text)}")
        elif k == "table":
            out.append("| " + " | ".join(blk.data["headers"]) + " |")
            out.append("|" + "---|" * len(blk.data["headers"]))
            out += ["| " + " | ".join(md_escape(str(c)) for c in row) + " |" for row in blk.data["rows"]]
        elif k == "source":
            url = blk.data["url"]
            link = f" <{url}>" if url.startswith(("http://", "https://")) else ""
            out.append(f"- {_md_cites(blk.text)}{link}")
    return "\n".join(out) + "\n"
