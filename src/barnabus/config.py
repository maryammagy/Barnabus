"""Validated configuration and runtime paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceTimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplied_offset_required: bool
    allowed_offsets_hours: list[float]
    missing_offset_assumption_hours: float | None = None
    inferred_named_zone_for_audit: str


class EventTimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    convention: str
    sources: dict[str, SourceTimeConfig]

    @field_validator("convention")
    @classmethod
    def require_utc_naive(cls, value: str) -> str:
        if value != "utc_naive":
            raise ValueError("only the utc_naive storage convention is supported")
        return value


class StudyWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str


class SchemaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_event_order: list[str]
    operational_event_types: list[str]
    service_code_pattern: str
    case_id_pattern: str
    event_id_pattern: str
    clinician_id_pattern: str
    ward_id_pattern: str

    @model_validator(mode="after")
    def event_types_do_not_overlap(self) -> "SchemaConfig":
        overlap = set(self.workflow_event_order) & set(self.operational_event_types)
        if overlap:
            raise ValueError(f"event type appears in both classes: {sorted(overlap)}")
        if len(self.workflow_event_order) != len(set(self.workflow_event_order)):
            raise ValueError("workflow_event_order contains duplicates")
        return self


class ContractsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_sites: list[str]
    cost_cad_min: float
    cost_cad_max: float
    max_ingest_lag_days: int = Field(ge=0)
    contained_rejection_reasons: list[str]
    expected_raw_min_rows: int = Field(ge=1)


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    study_window: StudyWindow
    event_time: EventTimeConfig
    schema_: SchemaConfig = Field(alias="schema")
    contracts: ContractsConfig
    grains: dict[str, str | list[str]]

    @property
    def allowed_event_types(self) -> list[str]:
        return self.schema_.workflow_event_order + self.schema_.operational_event_types


def load_config(path: Path) -> tuple[PipelineConfig, bytes]:
    raw = path.read_bytes()
    parsed: Any = yaml.safe_load(raw)
    return PipelineConfig.model_validate(parsed), raw


class RuntimePaths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    data_root: Path
    work_root: Path
    output_root: Path
    config_path: Path

    @model_validator(mode="after")
    def resolve_and_validate(self) -> "RuntimePaths":
        self.data_root = self.data_root.resolve()
        self.work_root = self.work_root.resolve()
        self.output_root = self.output_root.resolve()
        self.config_path = self.config_path.resolve()
        if not self.data_root.is_dir():
            raise ValueError(f"data root does not exist or is not a directory: {self.data_root}")
        if not (self.data_root / "events").is_dir():
            raise ValueError(f"data root has no events directory: {self.data_root}")
        if not self.config_path.is_file():
            raise ValueError(f"pipeline config not found: {self.config_path}")
        if self.work_root.is_relative_to(self.data_root) or self.output_root.is_relative_to(self.data_root):
            raise ValueError("work/output roots must not be inside the read-only data root")
        if self.work_root == self.output_root:
            raise ValueError("work and output roots must be separate")
        return self


def environment_default(name: str, fallback: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else fallback
