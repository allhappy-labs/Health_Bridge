"""Audited app inventory protects capability completeness and original identity."""

import json
from pathlib import Path

from custom_components.health_bridge.const import SUPPORTED_METRICS
from custom_components.health_bridge.statistic_rules import RULES, TYPE_METRICS
from homeassistant.components.recorder.statistics import UNIT_CLASS_TO_UNIT_CONVERTER

# MetricRegistry.selectable at app commit 52f9a07; independent of server rules.
APP_SELECTABLE = frozenset(
    """
steps
distance
active_calories
basal_energy_burned
flights_climbed
exercise_time
stand_time
time_in_daylight
walking_speed
walking_step_length
walking_asymmetry_percentage
walking_double_support_percentage
swimming_distance
cycling_distance
running_power
running_stride_length
running_ground_contact_time
running_vertical_oscillation
cycling_power
cycling_cadence
cycling_speed
running_speed
cycling_functional_threshold_power
swimming_stroke_count
underwater_depth
water_temperature
workout_effort_score
estimated_workout_effort_score
distance_rowing
distance_paddle_sports
distance_cross_country_skiing
distance_downhill_snow_sports
distance_skating_sports
walking_steadiness
cardio_recovery
physical_effort
insulin_delivery
six_minute_walk_test_distance
stair_ascent_speed
stair_descent_speed
body_mass
height
body_fat_percentage
lean_body_mass
waist_circumference
body_temperature
heart_rate
resting_heart_rate
walking_heart_rate_average
heart_rate_variability
oxygen_saturation
respiratory_rate
vo2_max
wrist_temperature
blood_pressure_systolic
blood_pressure_diastolic
uv_index
headphone_audio_exposure
environmental_audio_exposure
sleep_duration
sleep_rem_hours
sleep_core_hours
sleep_deep_hours
sleep_awake_hours
sleep_unspecified_hours
asleep_time
wake_time
sleep_details
mindful_minutes
dietary_water
dietary_energy_consumed
blood_glucose
dietary_carbohydrates
dietary_fat
dietary_protein
dietary_fiber
dietary_sugar
dietary_cholesterol
dietary_calcium
dietary_chloride
dietary_iron
dietary_magnesium
dietary_manganese
dietary_phosphorus
dietary_potassium
dietary_sodium
dietary_zinc
dietary_caffeine
dietary_copper
dietary_niacin
dietary_pantothenic_acid
dietary_riboflavin
dietary_thiamin
dietary_vitamin_b6
dietary_vitamin_c
dietary_vitamin_e
dietary_biotin
dietary_chromium
dietary_folate
dietary_iodine
dietary_molybdenum
dietary_selenium
dietary_vitamin_a
dietary_vitamin_b12
dietary_vitamin_d
dietary_vitamin_k
last_apple_workout
""".split()
)
UNSUPPORTED = {"uv_exposure_sed", "net_calories", "last_sync_time", "test_connection"}


def test_every_readable_app_metric_is_advertised_exactly_once():
    advertised = [metric for metrics in TYPE_METRICS.values() for metric in metrics]
    assert set(advertised) == APP_SELECTABLE
    assert len(advertised) == len(set(advertised)) == 107
    assert set(SUPPORTED_METRICS) == APP_SELECTABLE | UNSUPPORTED
    assert len(SUPPORTED_METRICS) == 111
    assert len(TYPE_METRICS) == 99
    assert len(TYPE_METRICS["HKCategoryTypeIdentifierSleepAnalysis"]) == 9
    assert TYPE_METRICS["HKWorkoutType"] == ("last_apple_workout",)


def test_catalog_fixture_matches_packaged_catalog_and_numeric_rules():
    fixture = Path("docs/protocol/fixtures/archive-catalog-v2.json")
    assert fixture.is_file(), "The complete audited catalog fixture is missing"
    catalog = json.loads(fixture.read_text())
    packaged = json.loads(
        Path("custom_components/health_bridge/archive_catalog_v2.json").read_text()
    )
    assert catalog == packaged
    assert set(catalog["unsupported_metrics"]) == UNSUPPORTED
    assert {s["sample_type"] for s in catalog["sources"]} == set(TYPE_METRICS)
    for source in catalog["sources"]:
        assert source["minimum_ios_major_version"] == 18
        for projection in source["projections"]:
            rule = RULES[projection["metric"]]
            assert rule.sample_type == source["sample_type"]
            assert rule.canonical_unit == source["canonical_unit"]
            assert rule.mode == projection["mode"]
    capability = json.loads(
        Path("docs/protocol/fixtures/archive-capability-v2.json").read_text()
    )["response"]
    assert set(capability["supported_metrics"]) == APP_SELECTABLE
    assert set(capability["supported_sample_types"]) == set(TYPE_METRICS)


def test_v210_new_metrics_have_exact_direct_source_and_canonical_unit():
    expected = {
        "running_power": ("RunningPower", "W"),
        "running_stride_length": ("RunningStrideLength", "m"),
        "running_ground_contact_time": ("RunningGroundContactTime", "ms"),
        "running_vertical_oscillation": ("RunningVerticalOscillation", "cm"),
        "cycling_power": ("CyclingPower", "W"),
        "cycling_cadence": ("CyclingCadence", "rpm"),
        "cycling_speed": ("CyclingSpeed", "m/s"),
        "running_speed": ("RunningSpeed", "m/s"),
        "cycling_functional_threshold_power": ("CyclingFunctionalThresholdPower", "W"),
        "swimming_stroke_count": ("SwimmingStrokeCount", "count"),
        "underwater_depth": ("UnderwaterDepth", "m"),
        "water_temperature": ("WaterTemperature", "degC"),
        "workout_effort_score": ("WorkoutEffortScore", "appleEffortScore"),
        "estimated_workout_effort_score": (
            "EstimatedWorkoutEffortScore",
            "appleEffortScore",
        ),
        "distance_rowing": ("DistanceRowing", "m"),
        "distance_paddle_sports": ("DistancePaddleSports", "m"),
        "distance_cross_country_skiing": ("DistanceCrossCountrySkiing", "m"),
        "distance_downhill_snow_sports": ("DistanceDownhillSnowSports", "m"),
        "distance_skating_sports": ("DistanceSkatingSports", "m"),
    }
    for metric, (suffix, unit) in expected.items():
        assert metric in RULES, f"Missing readable v2.1.0 metric: {metric}"
        assert RULES[metric].sample_type == "HKQuantityTypeIdentifier" + suffix
        assert RULES[metric].canonical_unit == unit


def test_all_numeric_metadata_units_are_supported_by_home_assistant():
    for rule in RULES.values():
        if rule.mode != "timeline" and rule.unit_class is not None:
            converter = UNIT_CLASS_TO_UNIT_CONVERTER[rule.unit_class]
            assert rule.unit in converter.VALID_UNITS, rule.metric
