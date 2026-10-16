from __future__ import annotations

from claude_fleet.disk_policy import GIB, assess_disk, disk_thresholds


def test_thresholds_scale_with_disk_size_and_keep_absolute_floor():
    small = disk_thresholds(100 * GIB)
    large = disk_thresholds(1000 * GIB)

    assert small["criticalBytes"] == 16 * GIB
    assert small["warningBytes"] == 32 * GIB
    assert small["recoveryBytes"] == 48 * GIB
    assert large["criticalBytes"] == 64 * GIB
    assert large["warningBytes"] == 96 * GIB
    assert large["recoveryBytes"] == 128 * GIB


def test_warning_hysteresis_clears_after_small_recovery_margin():
    result = assess_disk(300 * GIB, 65 * GIB, previous_level="warning")

    assert result["level"] == "healthy"
    assert result["warningBytes"] == 54 * GIB
    assert result["recoveryBytes"] == 75 * GIB

    still_close = assess_disk(300 * GIB, 56 * GIB, previous_level="warning")
    assert still_close["level"] == "warning"


def test_critical_hysteresis_keeps_full_recovery_buffer():
    result = assess_disk(300 * GIB, 65 * GIB, previous_level="critical")

    assert result["level"] == "warning"


def test_fast_consumption_predicts_pressure_before_static_threshold():
    samples = [
        {"at": 0, "freeBytes": 84 * GIB},
        {"at": 60, "freeBytes": 78 * GIB},
        {"at": 120, "freeBytes": 72 * GIB},
    ]

    result = assess_disk(300 * GIB, 72 * GIB, samples=samples)

    assert result["level"] == "healthy"
    assert result["effectiveLevel"] == "warning"
    assert result["mode"] == "predictive"
    assert 0 < result["estimatedMinutesToCritical"] <= 120


def test_one_off_space_drop_followed_by_flat_samples_is_not_predictive():
    samples = [
        {"at": 0, "freeBytes": 80 * GIB},
        {"at": 60, "freeBytes": 65 * GIB},
        {"at": 120, "freeBytes": 65 * GIB},
        {"at": 180, "freeBytes": 65 * GIB},
    ]

    result = assess_disk(300 * GIB, 65 * GIB, samples=samples)

    assert result["level"] == "healthy"
    assert result["predictive"] is False
    assert result["effectiveLevel"] == "healthy"


def test_recovery_trend_does_not_create_false_consumption_alarm():
    samples = [
        {"at": 0, "freeBytes": 60 * GIB},
        {"at": 60, "freeBytes": 70 * GIB},
        {"at": 120, "freeBytes": 80 * GIB},
    ]

    result = assess_disk(300 * GIB, 80 * GIB, samples=samples)

    assert result["consumptionBytesPerHour"] == 0
    assert result["effectiveLevel"] == "healthy"
