"""Historical source-volume checks that distinguish a real zero from a likely outage."""

from __future__ import annotations

from dataclasses import dataclass
import os
from statistics import mean


@dataclass(frozen=True)
class SourceHealthAssessment:
    status: str
    health_status: str
    baseline_average: float
    baseline_samples: int
    reason: str = ""


def assess_source_health(
    status: str, collected_count: int, previous_counts: list[int],
    observed_count: int | None = None, previous_observed_counts: list[int] | None = None,
) -> SourceHealthAssessment:
    """Flag a successful zero only when the source has a meaningful recent baseline."""
    sample_limit = int(os.getenv("RADAR_SOURCE_HEALTH_WINDOW", "7"))
    minimum_samples = int(os.getenv("RADAR_SOURCE_HEALTH_MIN_SAMPLES", "3"))
    average_threshold = float(os.getenv("RADAR_SOURCE_ZERO_AVG_THRESHOLD", "3"))
    observed_samples = [max(0, int(value)) for value in (previous_observed_counts or [])[:sample_limit]]
    use_observed = observed_count is not None and any(observed_samples)
    current = observed_count if use_observed else collected_count
    raw_samples = observed_samples if use_observed else previous_counts
    samples = [max(0, int(value)) for value in raw_samples[:sample_limit]]
    average = round(mean(samples), 2) if samples else 0.0
    if status == "failed":
        return SourceHealthAssessment(status, "FAILED", average, len(samples))
    if status == "partial":
        return SourceHealthAssessment(status, "DEGRADED", average, len(samples))
    if status != "success" or current > 0:
        return SourceHealthAssessment(status, "HEALTHY", average, len(samples))
    if len(samples) >= minimum_samples and average >= average_threshold:
        basis = "原始列表" if use_observed else "最终采集"
        reason = f"来源疑似假0条：近{len(samples)}次{basis}平均{average:g}条，本次为0条"
        return SourceHealthAssessment("warning", "SUSPECT", average, len(samples), reason)
    return SourceHealthAssessment(status, "HEALTHY", average, len(samples))
