"""Compatibility contracts inherited from the immutable Health Bridge v2.1.0 tag."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.health_bridge import _prepare_backfill_batch
from custom_components.health_bridge.const import METRIC_ATTRIBUTES_MAP


WEBHOOK = "/api/webhook/health_bridge"
HAL_TOKEN = "health-assistant-compatibility-token-00001"
PAL_TOKEN = "phone-assistant-compatibility-token-000001"
USER_ID = "compat"


@pytest.mark.asyncio
async def test_both_app_config_entries_keep_distinct_ownership(bridge_entries, hass):
    hal_entry, pal_entry = bridge_entries

    assert hal_entry.unique_id == "health_bridge:health_assistant_link"
    assert pal_entry.unique_id == "health_bridge:phone_assistant_link"
    assert hass.data["health_bridge"]["entry_id"] == hal_entry.entry_id
    assert hass.data["health_bridge"]["entry_ids"] == {
        "health_assistant_link": hal_entry.entry_id,
        "phone_assistant_link": pal_entry.entry_id,
    }


@pytest.mark.asyncio
async def test_each_app_token_only_authenticates_its_own_requests(bridge_client):
    async def send(token, payload):
        response = await bridge_client.post(
            WEBHOOK, json={"token": token, "user_id": USER_ID, **payload}
        )
        return (
            response.status,
            await response.json()
            if response.content_type == "application/json"
            else None,
        )

    hal_request = {"data": {"test_connection": [{"value": True}]}}
    pal_request = {
        "request_type": "phone_assistant_link",
        "client": "phone_assistant_link",
        "action": "ping",
    }

    assert (await send(HAL_TOKEN, hal_request))[0] == 200
    assert (await send(PAL_TOKEN, pal_request))[1]["protocol_version"] == 3
    assert (await send(PAL_TOKEN, hal_request))[0] == 401
    assert (await send(HAL_TOKEN, pal_request))[0] == 401


def test_upstream_metric_registry_keeps_all_111_keys():
    expected = set(
        """
        active_calories asleep_time basal_energy_burned blood_glucose
        blood_pressure_diastolic blood_pressure_systolic body_fat_percentage
        body_mass body_temperature cardio_recovery cycling_cadence cycling_distance
        cycling_functional_threshold_power cycling_power cycling_speed dietary_biotin
        dietary_caffeine dietary_calcium dietary_carbohydrates dietary_chloride
        dietary_cholesterol dietary_chromium dietary_copper dietary_energy_consumed
        dietary_fat dietary_fiber dietary_folate dietary_iodine dietary_iron
        dietary_magnesium dietary_manganese dietary_molybdenum dietary_niacin
        dietary_pantothenic_acid dietary_phosphorus dietary_potassium dietary_protein
        dietary_riboflavin dietary_selenium dietary_sodium dietary_sugar dietary_thiamin
        dietary_vitamin_a dietary_vitamin_b12 dietary_vitamin_b6 dietary_vitamin_c
        dietary_vitamin_d dietary_vitamin_e dietary_vitamin_k dietary_water dietary_zinc
        distance distance_cross_country_skiing distance_downhill_snow_sports
        distance_paddle_sports distance_rowing distance_skating_sports
        environmental_audio_exposure estimated_workout_effort_score exercise_time
        flights_climbed headphone_audio_exposure heart_rate heart_rate_variability
        height insulin_delivery last_apple_workout last_sync_time lean_body_mass
        mindful_minutes net_calories oxygen_saturation physical_effort respiratory_rate
        resting_heart_rate running_ground_contact_time running_power running_speed
        running_stride_length running_vertical_oscillation six_minute_walk_test_distance
        sleep_awake_hours sleep_core_hours sleep_deep_hours sleep_details sleep_duration
        sleep_rem_hours sleep_unspecified_hours stair_ascent_speed stair_descent_speed
        stand_time steps swimming_distance swimming_stroke_count test_connection
        time_in_daylight underwater_depth uv_exposure_sed uv_index vo2_max
        waist_circumference wake_time walking_asymmetry_percentage
        walking_double_support_percentage walking_heart_rate_average walking_speed
        walking_steadiness walking_step_length water_temperature workout_effort_score
        wrist_temperature
    """.split()
    )
    assert len(expected) == 111
    assert set(METRIC_ATTRIBUTES_MAP) == expected


@pytest.mark.asyncio
async def test_live_v1_acknowledges_applied_sensor(bridge_client, hass):
    response = await bridge_client.post(
        WEBHOOK,
        json={
            "token": HAL_TOKEN,
            "user_id": USER_ID,
            "request_type": "live",
            "protocol_version": 1,
            "request_id": "compat-live-1",
            "data": {
                "steps": [
                    {"timestamp": datetime.now(timezone.utc).isoformat(), "value": 42}
                ]
            },
        },
    )

    assert response.status == 200
    body = await response.json()
    expected = {
        "ok": True,
        "applied": True,
        "request_type": "live",
        "protocol_version": 1,
        "request_id": "compat-live-1",
    }
    assert {key: body.get(key) for key in expected} == expected
    await hass.async_block_till_done()
    assert hass.states.get("sensor.steps_compat").state == "42"


@pytest.mark.asyncio
async def test_numeric_and_text_backfill_v1_keep_original_shapes(bridge_client, hass):
    now = datetime.now(timezone.utc)
    workout = {
        "workout_type": "Running",
        "duration": 1800,
        "end_time": now.isoformat(),
        "timestamp": now.isoformat(),
    }
    live_response = await bridge_client.post(
        WEBHOOK,
        json={
            "token": HAL_TOKEN,
            "user_id": USER_ID,
            "request_type": "live",
            "protocol_version": 1,
            "request_id": "compat-live-backfill",
            "data": {
                "steps": [{"timestamp": now.isoformat(), "value": 10}],
                "last_apple_workout": [workout],
            },
        },
    )
    assert live_response.status == 200
    await hass.async_block_till_done()

    earlier = now - timedelta(hours=1)
    batch = _prepare_backfill_batch(
        hass,
        {
            "request_type": "backfill",
            "protocol_version": 1,
            "request_id": "compat-backfill-1",
            "data": {
                "steps": [
                    {"timestamp": earlier.isoformat(), "value": 7},
                    {"timestamp": now.isoformat(), "value": 10},
                ],
                "last_apple_workout": [
                    {
                        **workout,
                        "timestamp": earlier.isoformat(),
                        "end_time": earlier.isoformat(),
                    }
                ],
            },
        },
        USER_ID,
    )

    assert batch.request_id == "compat-backfill-1"
    assert batch.series_by_entity["sensor.steps_compat"] == [
        (earlier.timestamp(), 7.0),
        (now.timestamp(), 10.0),
    ]
    text_points = batch.text_series_by_entity["sensor.last_apple_workout_compat"]
    assert len(text_points) == 1
    assert "Running" in text_points[0].state
    assert text_points[0].attributes["workout_type"] == "Running"
