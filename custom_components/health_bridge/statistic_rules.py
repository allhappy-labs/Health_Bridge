"""Projection rules and capability share the packaged audited source catalog."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class StatisticRule:
    metric: str
    sample_type: str
    mode: Literal["mean", "total", "duration", "timeline"]
    canonical_unit: str | None = None
    unit_class: str | None = None
    categories: frozenset[int] = frozenset()
    unit: str | None = None
    scale: float = 1
    minimum_value: float = 0
    allowed_categories: frozenset[int] = frozenset()

    @property
    def additive(self) -> bool:
        return self.mode in {"total", "duration"}


CATALOG = json.loads(Path(__file__).with_name("archive_catalog_v2.json").read_text())
RULES = {
    projection["metric"]: StatisticRule(
        metric=projection["metric"],
        sample_type=source["sample_type"],
        mode=projection["mode"],
        canonical_unit=source["canonical_unit"],
        unit_class=projection["unit_class"],
        categories=frozenset(projection["categories"]),
        unit=projection["unit"],
        scale=projection["scale"],
        minimum_value=projection["minimum_value"],
        allowed_categories=(
            frozenset(range(6))
            if source["sample_type"] == "HKCategoryTypeIdentifierSleepAnalysis"
            else frozenset({0})
            if source["sample_type"] == "HKCategoryTypeIdentifierMindfulSession"
            else frozenset()
        ),
    )
    for source in CATALOG["sources"]
    for projection in source["projections"]
}
TYPE_METRICS = {
    source["sample_type"]: tuple(p["metric"] for p in source["projections"])
    for source in CATALOG["sources"]
}
