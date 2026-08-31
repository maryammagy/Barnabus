"""Observed wall-time and process-tree peak-memory telemetry."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator

import psutil


@dataclass
class StepMetric:
    step: str
    wall_seconds: float
    peak_rss_bytes: int
    reused: bool = False


class PeakMemorySampler:
    def __init__(self, interval_seconds: float = 0.05) -> None:
        self._interval = interval_seconds
        self._stop = threading.Event()
        self.peak = 0
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        process = psutil.Process()
        while not self._stop.is_set():
            try:
                rss = process.memory_info().rss
                rss += sum(child.memory_info().rss for child in process.children(recursive=True))
                self.peak = max(self.peak, rss)
            except (psutil.Error, OSError):
                pass
            self._stop.wait(self._interval)

    def __enter__(self) -> "PeakMemorySampler":
        self._sample_once()
        self._thread.start()
        return self

    def _sample_once(self) -> None:
        try:
            process = psutil.Process()
            rss = process.memory_info().rss
            rss += sum(child.memory_info().rss for child in process.children(recursive=True))
            self.peak = max(self.peak, rss)
        except (psutil.Error, OSError):
            pass

    def __exit__(self, *_: object) -> None:
        self._sample_once()
        self._stop.set()
        self._thread.join(timeout=1)


class MetricsRecorder:
    def __init__(self) -> None:
        self.steps: list[StepMetric] = []

    @contextmanager
    def step(self, name: str, *, reused: bool = False) -> Iterator[None]:
        start = time.perf_counter()
        with PeakMemorySampler() as sampler:
            yield
        self.steps.append(StepMetric(name, time.perf_counter() - start, sampler.peak, reused))

    def as_dicts(self) -> list[dict[str, object]]:
        return [asdict(step) for step in self.steps]
