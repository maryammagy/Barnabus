from __future__ import annotations

from datetime import datetime

import pytest

from barnabus.config import load_config
from barnabus.errors import ContractViolation
from barnabus.timeutils import elapsed_seconds, normalize_source_timestamp, zone_valid_offsets

from conftest import CONFIG_PATH


def test_fall_dst_fold_is_disambiguated_by_the_supplied_offset() -> None:
    config, _ = load_config(CONFIG_PATH)
    source = config.event_time.sources["hisA"]
    daylight = normalize_source_timestamp(
        "2025-11-02 01:30:00", -5.0, "hisA", source
    )
    standard = normalize_source_timestamp(
        "2025-11-02 01:30:00", -6.0, "hisA", source
    )
    assert daylight.utc_naive == datetime(2025, 11, 2, 6, 30)
    assert standard.utc_naive == datetime(2025, 11, 2, 7, 30)
    assert daylight.zone_audit == standard.zone_audit == "ambiguous_fold_disambiguated"
    assert elapsed_seconds(daylight, standard) == 3600


def test_spring_dst_nonexistent_wall_time_is_rejected() -> None:
    config, _ = load_config(CONFIG_PATH)
    source = config.event_time.sources["hisA"]
    assert zone_valid_offsets(datetime(2026, 3, 8, 2, 30), "America/Chicago") == set()
    with pytest.raises(ContractViolation, match="nonexistent_local_time"):
        normalize_source_timestamp("2026-03-08 02:30:00", -6.0, "hisA", source)


def test_required_source_offset_cannot_be_silently_inferred() -> None:
    config, _ = load_config(CONFIG_PATH)
    source = config.event_time.sources["hisA"]
    with pytest.raises(ContractViolation, match="requires a numeric UTC offset"):
        normalize_source_timestamp("2026-01-01 12:00:00", None, "hisA", source)
