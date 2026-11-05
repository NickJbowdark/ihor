"""Report types offered by GIM."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReportSpec:
    key: str
    name: str
    default_days: int
    max_events: int
    needs: str                       # "" | "country" | "region" | "topic"
    categories: tuple[str, ...]      # event category filter ('' = any)
    purpose: str                     # passed to the model as the report brief


SPECS: dict[str, ReportSpec] = {s.key: s for s in [
    ReportSpec("daily_brief", "Daily Global Intelligence Brief", 1, 12, "", (),
               "Daily intelligence brief: the most significant geopolitical developments of the last 24 hours, "
               "why each matters, and what to watch."),
    ReportSpec("weekly_assessment", "Weekly Geopolitical Assessment", 7, 15, "", (),
               "Weekly geopolitical assessment: main developments of the week, regional trends, important "
               "diplomatic decisions, conflict developments, strategic implications and open questions."),
    ReportSpec("country_profile", "Country Intelligence Profile", 30, 12, "country", (),
               "Country intelligence profile: recent political, diplomatic, security and economic developments "
               "concerning the country, its international relationships and the actors involved."),
    ReportSpec("regional_conflict", "Regional Conflict Assessment", 14, 12, "region", ("conflict", "humanitarian"),
               "Regional conflict assessment: conflict and security developments, humanitarian consequences, "
               "escalation or de-escalation indicators, positions of the parties and external actors."),
    ReportSpec("diplomatic_strategic", "Diplomatic and Strategic Analysis", 14, 12, "",
               ("diplomatic", "international_orgs", "economic"),
               "Diplomatic and strategic analysis: negotiations, agreements, sanctions, multilateral decisions "
               "and shifts in alignments, with their strategic implications."),
    ReportSpec("custom_topic", "Custom Research Brief", 30, 12, "topic", (),
               "Custom research brief on the requested topic: chronology, actors and positions, background, "
               "alternative explanations, implications and indicators to monitor."),
]}
