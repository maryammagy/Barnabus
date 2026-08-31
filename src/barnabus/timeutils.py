"""Reference timestamp normalization used by contracts and focused tests.

The production SQL performs the same arithmetic out of core. A configured IANA
zone determines an unambiguous instant; a supplied numeric offset selects the
fold when a wall clock is ambiguous. A nonexistent wall time has no valid
instant and is rejected from workflow ordering. The source contract declares
device timestamps UTC when their offset field is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from barnabus.config import SourceTimeConfig
from barnabus.errors import ContractViolation


@dataclass(frozen=True)
class NormalizedTimestamp:
    utc_naive: datetime
    offset_hours: float
    resolution: str
    zone_audit: str


def _parse_naive(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise ContractViolation("event timestamp must be source-local and timezone-naive")
    return parsed


def zone_valid_offsets(local: datetime, zone_name: str) -> set[float]:
    zone = ZoneInfo(zone_name)
    valid: set[float] = set()
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == local:
            offset = aware.utcoffset()
            if offset is not None:
                valid.add(offset.total_seconds() / 3600)
    return valid


def normalize_source_timestamp(
    value: str | datetime,
    supplied_offset_hours: float | None,
    source: str,
    source_config: SourceTimeConfig,
) -> NormalizedTimestamp:
    local = _parse_naive(value)
    if supplied_offset_hours is None:
        if source_config.supplied_offset_required:
            raise ContractViolation(f"{source} requires a numeric UTC offset")
        if source_config.missing_offset_assumption_hours is None:
            raise ContractViolation(f"{source} has no declared missing-offset convention")
        supplied_or_default = float(source_config.missing_offset_assumption_hours)
        resolution = "declared_source_default"
    else:
        supplied_or_default = float(supplied_offset_hours)
        if supplied_or_default not in source_config.allowed_offsets_hours:
            raise ContractViolation(
                f"{source} offset {supplied_or_default} is outside its contract"
            )
        resolution = "iana_zone"

    zone_name = source_config.inferred_named_zone_for_audit
    valid_offsets = zone_valid_offsets(local, zone_name)
    if not valid_offsets:
        raise ContractViolation("nonexistent_local_time")
    if len(valid_offsets) > 1:
        if supplied_or_default not in valid_offsets:
            raise ContractViolation("ambiguous_local_time_without_matching_offset")
        offset = supplied_or_default
        zone_audit = "ambiguous_fold_disambiguated"
    else:
        offset = next(iter(valid_offsets))
        if supplied_offset_hours is not None and supplied_or_default != offset:
            zone_audit = "offset_zone_corrected"
        elif supplied_offset_hours is None:
            zone_audit = "declared_source_default"
        else:
            zone_audit = "zone_consistent"
    utc_naive = local - timedelta(hours=offset)
    return NormalizedTimestamp(utc_naive, offset, resolution, zone_audit)


def elapsed_seconds(start: NormalizedTimestamp, end: NormalizedTimestamp) -> int:
    """Return an exact UTC duration; never subtract source-local wall clocks."""

    return int((end.utc_naive - start.utc_naive).total_seconds())
