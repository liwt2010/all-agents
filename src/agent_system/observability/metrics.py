"""
Prometheus-compatible metrics endpoint — PLATFORM §5.5, §6.5

Exposes metrics in Prometheus text format so a real Prometheus server
can scrape /metrics. In-memory counter / gauge / histogram types.
"""

import logging
import math
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class Counter:
    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels):
        if not self._check_labels(labels):
            return
        key = tuple(sorted(labels.items()))
        self._values[key] += amount

    def _check_labels(self, labels: dict[str, str]) -> bool:
        for ln in self.label_names:
            if ln not in labels:
                logger.warning(f"Counter {self.name} missing label {ln}")
                return False
        return True

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        for labels, value in self._values.items():
            label_str = ",".join(f'{k}="{v}"' for k, v in labels) if labels else ""
            if label_str:
                lines.append(f"{self.name}{{{label_str}}} {value}")
            else:
                lines.append(f"{self.name} {value}")
        return lines


class Gauge:
    def __init__(self, name: str, help_text: str, label_names: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self._values: dict[tuple[tuple[str, str], ...], float] = {}

    def set(self, value: float, **labels):
        key = tuple(sorted(labels.items()))
        self._values[key] = value

    def inc(self, amount: float = 1.0, **labels):
        key = tuple(sorted(labels.items()))
        self._values[key] = self._values.get(key, 0) + amount

    def dec(self, amount: float = 1.0, **labels):
        key = tuple(sorted(labels.items()))
        self._values[key] = self._values.get(key, 0) - amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for labels, value in self._values.items():
            label_str = ",".join(f'{k}="{v}"' for k, v in labels) if labels else ""
            if label_str:
                lines.append(f"{self.name}{{{label_str}}} {value}")
            else:
                lines.append(f"{self.name} {value}")
        return lines


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: list[float] | None = None,
                 label_names: list[str] | None = None):
        self.name = name
        self.help = help_text
        self.label_names = label_names or []
        self.buckets = sorted(buckets or [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10])
        # Per-label-set: list of bucket counts + sum + count
        self._data: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}

    def observe(self, value: float, **labels):
        key = tuple(sorted(labels.items()))
        if key not in self._data:
            self._data[key] = {
                "bucket_counts": [0] * len(self.buckets),
                "sum": 0.0,
                "count": 0,
            }
        d = self._data[key]
        for i, b in enumerate(self.buckets):
            if value <= b:
                d["bucket_counts"][i] += 1
        d["sum"] += value
        d["count"] += 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for labels, d in self._data.items():
            label_str = ",".join(f'{k}="{v}"' for k, v in labels) if labels else ""
            cum = 0
            for i, b in enumerate(self.buckets):
                cum = d["bucket_counts"][i]
                b_label = f'le="{b}"'
                if label_str:
                    lines.append(f'{self.name}_bucket{{{label_str},{b_label}}} {cum}')
                else:
                    lines.append(f'{self.name}_bucket{{{b_label}}} {cum}')
            # +Inf bucket
            if label_str:
                lines.append(f'{self.name}_bucket{{{label_str},le="+Inf"}} {d["count"]}')
            else:
                lines.append(f'{self.name}_bucket{{le="+Inf"}} {d["count"]}')
            # sum
            if label_str:
                lines.append(f'{self.name}_sum{{{label_str}}} {d["sum"]}')
                lines.append(f'{self.name}_count{{{label_str}}} {d["count"]}')
            else:
                lines.append(f'{self.name}_sum {d["sum"]}')
                lines.append(f'{self.name}_count {d["count"]}')
        return lines


class MetricsRegistry:
    """In-memory metrics store, renderable to Prometheus text format."""

    def __init__(self):
        self._metrics: dict[str, object] = {}

    def counter(self, name: str, help_text: str, label_names: list[str] | None = None) -> Counter:
        m = self._metrics.get(name)
        if not isinstance(m, Counter):
            m = Counter(name, help_text, label_names)
            self._metrics[name] = m
        return m

    def gauge(self, name: str, help_text: str, label_names: list[str] | None = None) -> Gauge:
        m = self._metrics.get(name)
        if not isinstance(m, Gauge):
            m = Gauge(name, help_text, label_names)
            self._metrics[name] = m
        return m

    def histogram(self, name: str, help_text: str, buckets: list[float] | None = None,
                  label_names: list[str] | None = None) -> Histogram:
        m = self._metrics.get(name)
        if not isinstance(m, Histogram):
            m = Histogram(name, help_text, buckets, label_names)
            self._metrics[name] = m
        return m

    def render(self) -> str:
        lines = []
        for m in self._metrics.values():
            lines.extend(m.render())
            lines.append("")
        return "\n".join(lines)


# Default global
_default_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = MetricsRegistry()
    return _default_registry


def reset_metrics_registry():
    global _default_registry
    _default_registry = None
