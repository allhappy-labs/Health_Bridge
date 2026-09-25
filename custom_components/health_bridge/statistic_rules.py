"""Audited v2 rules; live sensor state classes are not archive semantics.

Only these four original HealthKit types are negotiated by v2. Remaining live
metrics need an explicit source/unit audit before they can be added here.
Quantity canonical units are HealthKit units, not the live display labels.
"""

from dataclasses import dataclass
from typing import Literal

from .const import METRIC_ATTRIBUTES_MAP


@dataclass(frozen=True, slots=True)
class StatisticRule:
    metric: str
    sample_type: str
    mode: Literal["mean", "total", "duration", "timeline"]
    canonical_unit: str | None = None
    unit_class: str | None = None
    categories: frozenset[int] = frozenset()

    @property
    def unit(self) -> str | None:
        return METRIC_ATTRIBUTES_MAP[self.metric].get("native_unit_of_measurement")

    @property
    def additive(self) -> bool:
        return self.mode in {"total", "duration"}


_SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
RULES = {
    rule.metric: rule
    for rule in (
        StatisticRule("steps", "HKQuantityTypeIdentifierStepCount", "total", "count"),
        StatisticRule(
            "heart_rate", "HKQuantityTypeIdentifierHeartRate", "mean", "count/min"
        ),
        StatisticRule(
            "sleep_duration",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({1, 3, 4, 5}),
        ),
        StatisticRule(
            "sleep_rem_hours",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({5}),
        ),
        StatisticRule(
            "sleep_core_hours",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({3}),
        ),
        StatisticRule(
            "sleep_deep_hours",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({4}),
        ),
        StatisticRule(
            "sleep_awake_hours",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({2}),
        ),
        StatisticRule(
            "sleep_unspecified_hours",
            _SLEEP,
            "duration",
            unit_class="duration",
            categories=frozenset({1}),
        ),
        StatisticRule("sleep_details", _SLEEP, "timeline"),
        StatisticRule("asleep_time", _SLEEP, "timeline"),
        StatisticRule("wake_time", _SLEEP, "timeline"),
        StatisticRule("last_apple_workout", "HKWorkoutTypeIdentifier", "timeline"),
    )
}
TYPE_METRICS = {
    sample_type: tuple(
        rule.metric for rule in RULES.values() if rule.sample_type == sample_type
    )
    for sample_type in dict.fromkeys(rule.sample_type for rule in RULES.values())
}
