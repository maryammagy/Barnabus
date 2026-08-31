from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from barnabus.config import RuntimePaths
from barnabus.pipeline import RunResult, run_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline.yaml"


EVENT_SCHEMA_BASE: list[tuple[str, pa.DataType]] = [
    ("case_id", pa.string()),
    ("event_type", pa.string()),
    ("site", pa.string()),
    ("clinician_id", pa.string()),
    ("ward_id", pa.string()),
    ("cost_cad", pa.float64()),
    ("ingest_ts", pa.timestamp("ns")),
    ("source_system", pa.string()),
    ("tz_offset_hours", pa.float64()),
    ("event_ts", pa.string()),
]


def event(
    event_id: str,
    case_id: str,
    event_type: str,
    event_ts: str,
    ingest_ts: str,
    *,
    source_system: str = "hisB",
    offset: float | None = 0.0,
    cost_cad: float | None = None,
    site: str = " a ",
    clinician_id: str = " md0001 ",
    ward_id: str = " a-w1 ",
    svc_code: str | None = " a-card ",
    service_code: str | None = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "event_type": event_type,
        "site": site,
        "clinician_id": clinician_id,
        "ward_id": ward_id,
        "cost_cad": cost_cad,
        "ingest_ts": datetime.fromisoformat(ingest_ts),
        "source_system": source_system,
        "tz_offset_hours": offset,
        "event_ts": event_ts,
        "svc_code": svc_code,
        "service_code": service_code,
        "event_id": event_id,
    }


def write_event_partition(
    data_root: Path,
    month: str,
    rows: list[dict[str, Any]],
    *,
    include_legacy: bool,
    include_current: bool,
) -> Path:
    fields = list(EVENT_SCHEMA_BASE)
    if include_legacy:
        fields.append(("svc_code", pa.string()))
    if include_current:
        fields.append(("service_code", pa.string()))
    fields.append(("event_id", pa.string()))
    schema = pa.schema(fields)
    projected = [{name: row.get(name) for name, _ in fields} for row in rows]
    path = data_root / "events" / f"ingest_month={month}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(projected, schema=schema), path)
    return path


def build_synthetic_data_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    january = [
        event(
            "0000000000000001",
            " 1 ",
            " Referral_Created ",
            "2026-01-01 09:00:00",
            "2026-01-01 12:00:00",
        ),
        event(
            "0000000000000002",
            "c000000001",
            "documents_received",
            "2026-01-01 10:00:00",
            "2026-01-01 11:00:00",
        ),
        # A semantic retry changes both its delivery timestamp and source metadata.
        event(
            "0000000000000003",
            "C000000001",
            "documents_received",
            "2026-01-01 10:00:00",
            "2026-01-01 11:30:00",
            source_system="device",
            offset=None,
        ),
        event(
            "0000000000000004",
            "2",
            "referral_created",
            "2026-01-01 08:00:00",
            "2026-01-01 08:05:00",
        ),
        event(
            "0000000000000005",
            "2",
            "case_closed",
            "2026-01-01 10:00:00",
            "2026-01-10 10:00:00",
            cost_cad=10.01,
        ),
    ]
    august = [
        event(
            "0000000000000006",
            "3",
            "referral_created",
            "2026-08-01 09:00:00",
            "2026-08-01 09:01:00",
            svc_code=None,
            service_code="A-CARD",
        )
    ]
    write_event_partition(
        root, "2026-01", january, include_legacy=True, include_current=True
    )
    write_event_partition(
        root, "2026-08", august, include_legacy=False, include_current=True
    )

    with (root / "clinicians.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "clinician_id",
                "name",
                "site",
                "home_service",
                "ward_id",
                "covers_other_services",
            ]
        )
        writer.writerow(["MD0001", "Synthetic Clinician", "A", "A-CARD", "A-W1", "false"])

    with (root / "snapshot_cases.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "site",
                "service_code",
                "referral_ts",
                "patient_age",
                "cancelled",
                "readiness_days",
                "case_ref",
            ]
        )
        writer.writerow(["A", "A-CARD", "2026-01-01", "40", "false", "", "1"])
        writer.writerow(["A", "A-CARD", "2026-01-01", "50", "true", "", "2"])
        writer.writerow(["A", "A-CARD", "2026-08-01", "60", "false", "", "3"])
    return root


def make_runtime_paths(base: Path, data_root: Path) -> RuntimePaths:
    return RuntimePaths(
        data_root=data_root,
        work_root=base / "work",
        output_root=base / "outputs",
        config_path=CONFIG_PATH,
    )


@pytest.fixture(scope="session")
def synthetic_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[RuntimePaths, RunResult]:
    base = tmp_path_factory.mktemp("synthetic-pipeline")
    data_root = build_synthetic_data_root(base / "data")
    paths = make_runtime_paths(base, data_root)
    result = run_pipeline(paths, mode="full", threads=2, memory_limit="512MB")
    return paths, result
