"""Finite-safe, bounded-memory statistics used by diagnosis rules."""

import collections
import math

MIB = 1024.0 * 1024.0


def finite_number(value):
    """Return whether ``value`` is a finite, non-boolean number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _finite_float(value):
    if not finite_number(value):
        return None
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percentile(values, percent):
    """Return an interpolated percentile over finite values, or ``None``."""
    if not finite_number(percent) or not 0 <= percent <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(
        value for value in (_finite_float(item) for item in values)
        if value is not None)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(percent) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    left, right = ordered[lower], ordered[upper]
    scale = max(abs(left), abs(right))
    if scale == 0:
        return 0.0
    normalized = (
        (left / scale) * (1.0 - fraction)
        + (right / scale) * fraction)
    normalized = max(-1.0, min(1.0, normalized))
    result = normalized * scale
    return result if math.isfinite(result) else None


def robust_band(values, absolute_floor=0.0, relative_floor=0.0):
    """Build finite-safe robust high/low deviation thresholds.

    Floors deliberately widen degenerate bands. They suppress tiny cold-start
    noise without making a stable zero baseline blind to a material excursion.
    """
    absolute_floor = _finite_float(absolute_floor)
    relative_floor = _finite_float(relative_floor)
    if (absolute_floor is None or relative_floor is None
            or absolute_floor < 0 or relative_floor < 0):
        raise ValueError("band floors must be finite and nonnegative")
    sample = [
        value for value in (_finite_float(item) for item in values)
        if value is not None]
    center = percentile(sample, 50)
    if center is None:
        return None
    deviations = []
    for value in sample:
        try:
            deviation = abs(value - center)
        except OverflowError:
            continue
        if math.isfinite(deviation):
            deviations.append(deviation)
    mad = percentile(deviations, 50) or 0.0
    p10 = percentile(sample, 10)
    p90 = percentile(sample, 90)
    p99 = percentile(sample, 99)
    try:
        sigma = mad * 1.4826
        relative = abs(center) * relative_floor
    except OverflowError:
        sigma = 0.0
        relative = 0.0
    if not math.isfinite(sigma):
        sigma = 0.0
    if not math.isfinite(relative):
        relative = 0.0
    floor = max(absolute_floor, relative)
    use_quantiles = len(sample) >= 20
    high_observed = max(
        0.0, (p90 - center)
        if use_quantiles and p90 is not None and p90 >= center else 0.0)
    low_observed = max(
        0.0, (center - p10)
        if use_quantiles and p10 is not None and p10 <= center else 0.0)
    warn_high_width = max(floor, sigma * 3.0, high_observed)
    warn_low_width = max(floor, sigma * 3.0, low_observed)
    critical_floor = floor * 2.0
    critical_high_width = max(
        critical_floor, sigma * 6.0,
        (p99 - center)
        if use_quantiles and p99 is not None and p99 >= center else 0.0)
    critical_low_width = max(critical_floor, sigma * 6.0,
                             warn_low_width * 2.0)

    def shifted(delta):
        try:
            result = center + delta
        except OverflowError:
            return None
        return result if math.isfinite(result) else None

    return {
        "count": len(sample),
        "center": center,
        "mad": mad,
        "p10": p10,
        "p90": p90,
        "p99": p99,
        "warn_high": shifted(warn_high_width),
        "critical_high": shifted(critical_high_width),
        "warn_low": shifted(-warn_low_width),
        "critical_low": shifted(-critical_low_width),
        "absolute_floor": absolute_floor,
        "relative_floor": relative_floor,
        "quantiles_used": use_quantiles,
    }


def classify_deviation(value, band, direction="high"):
    """Classify one finite value against a robust band.

    Returns ``None`` when no claim can be made, otherwise an evidence dict.
    """
    value = _finite_float(value)
    if value is None or not band or direction not in ("high", "low"):
        return None
    warn = _finite_float(band.get("warn_" + direction))
    critical = _finite_float(band.get("critical_" + direction))
    center = _finite_float(band.get("center"))
    if warn is None or critical is None or center is None:
        return None
    crossed_warn = value >= warn if direction == "high" else value <= warn
    if not crossed_warn:
        return None
    crossed_critical = (
        value >= critical if direction == "high" else value <= critical)
    severity = "critical" if crossed_critical else "warn"
    threshold = critical if crossed_critical else warn
    span = abs(threshold - center)
    distance = abs(value - center)
    ratio = distance / span if span > 0 and math.isfinite(distance) else 1.0
    if not math.isfinite(ratio):
        ratio = 10.0
    if ratio >= 2.9:
        bonus = 19
    else:
        bonus = max(0, int((ratio - 1.0) * 10))
    return {
        "severity": severity,
        "score": min(100, (80 if crossed_critical else 40) + bonus),
        "direction": direction,
        "value": value,
        "threshold": threshold,
        "band": dict(band),
    }


class OnlineTrend:
    """Online least-squares trend with constant totals and bounded recent data."""

    def __init__(self, recent_size=10):
        if recent_size < 2:
            raise ValueError("recent_size must be >= 2")
        self.recent = collections.deque(maxlen=recent_size)
        self.count = 0
        self.first_time = None
        self.last_time = None
        self.first_value = None
        self.last_value = None
        self.mean_time = 0.0
        self.mean_value = 0.0
        self.sxx = 0.0
        self.sxy = 0.0
        self.rises = 0
        self.drops = 0
        self.flats = 0
        self.invalid_count = 0
        self.overflowed = False

    def add(self, timestamp, value):
        timestamp = _finite_float(timestamp)
        value = _finite_float(value)
        if timestamp is None or value is None or (
                self.last_time is not None and timestamp <= self.last_time):
            self.invalid_count += 1
            return False
        if self.last_value is not None:
            if value > self.last_value:
                self.rises += 1
            elif value < self.last_value:
                self.drops += 1
            else:
                self.flats += 1
        if self.count == 0:
            self.first_time = timestamp
            self.first_value = value
            x = 0.0
        else:
            try:
                x = timestamp - self.first_time
            except OverflowError:
                self.invalid_count += 1
                return False
            if not math.isfinite(x):
                self.invalid_count += 1
                return False
        self.count += 1
        delta_x = x - self.mean_time
        delta_y = value - self.mean_value
        self.mean_time += delta_x / self.count
        self.mean_value += delta_y / self.count
        try:
            self.sxx += delta_x * (x - self.mean_time)
            self.sxy += delta_x * (value - self.mean_value)
        except OverflowError:
            self.overflowed = True
        if not math.isfinite(self.sxx) or not math.isfinite(self.sxy):
            self.overflowed = True
        self.last_time = timestamp
        self.last_value = value
        self.recent.append((timestamp, value))
        return True

    @property
    def span_seconds(self):
        if self.count < 2:
            return 0.0
        span = self.last_time - self.first_time
        return span if math.isfinite(span) and span >= 0 else 0.0

    @property
    def slope_per_second(self):
        if self.count < 2 or self.overflowed or self.sxx <= 0:
            return None
        slope = self.sxy / self.sxx
        return slope if math.isfinite(slope) else None

    def recent_slope_per_second(self, minimum=3, window=None):
        points = list(self.recent)
        if window is not None:
            points = points[-window:]
        if len(points) < minimum:
            return None
        recent = OnlineTrend(max(len(points), 2))
        for timestamp, value in points:
            recent.add(timestamp, value)
        return recent.slope_per_second

    def summary(self):
        return {
            "count": self.count,
            "span_seconds": self.span_seconds,
            "slope_per_second": self.slope_per_second,
            "first_value": self.first_value,
            "last_value": self.last_value,
            "rises": self.rises,
            "drops": self.drops,
            "flats": self.flats,
            "invalid_count": self.invalid_count,
            "overflowed": self.overflowed,
        }


def trend(samples, recent_size=10):
    """Build an :class:`OnlineTrend` from ``(timestamp, value)`` pairs."""
    result = OnlineTrend(recent_size)
    for timestamp, value in samples:
        result.add(timestamp, value)
    return result


def leak_evidence(samples, min_count=5, min_span_seconds=30 * 60,
                  min_slope_mib_per_min=1.0, plateau_slope_mib_per_min=0.1,
                  recent_size=5):
    """Return sustained footprint-growth evidence, or ``None``."""
    accumulator = samples if isinstance(samples, OnlineTrend) else trend(
        samples, max(recent_size, 5))
    slope = accumulator.slope_per_second
    if (accumulator.count < min_count
            or accumulator.span_seconds < min_span_seconds
            or slope is None):
        return None
    slope_mib = slope * 60.0 / MIB
    transitions = max(1, accumulator.count - 1)
    mostly_rising = (
        accumulator.rises >= math.ceil(transitions * 0.6)
        and accumulator.drops <= max(1, transitions // 4)
        and accumulator.last_value > accumulator.first_value)
    recent_slope = accumulator.recent_slope_per_second(
        min(recent_size, len(accumulator.recent)), window=recent_size)
    recent_slope_mib = (
        recent_slope * 60.0 / MIB if recent_slope is not None else None)
    plateaued = (
        recent_slope_mib is not None
        and recent_slope_mib < plateau_slope_mib_per_min)
    if (not mostly_rising or plateaued
            or slope_mib < min_slope_mib_per_min):
        return None
    current = accumulator.last_value
    critical = slope_mib >= 10.0 and current >= 512 * MIB
    return {
        "severity": "critical" if critical else "warn",
        "score": min(100, (80 if critical else 40)
                     + min(19, int(slope_mib))),
        "sample_count": accumulator.count,
        "span_seconds": accumulator.span_seconds,
        "slope_mib_per_min": slope_mib,
        "recent_slope_mib_per_min": recent_slope_mib,
        "current_footprint_bytes": current,
        "rises": accumulator.rises,
        "drops": accumulator.drops,
        "plateaued": plateaued,
    }


_RUNAWAY_POLICIES = {
    "cpu_pct": (20.0, 0.5, 75.0, 95.0),
    "pkg_idle_wakeups_per_s": (50.0, 1.0, 500.0, 1000.0),
    "interrupt_wakeups_per_s": (250.0, 1.0, 1000.0, 2500.0),
}


def runaway_evidence(metric, current, baseline_values=(), min_baseline_count=5):
    """Classify one process CPU or wakeup metric against history or thresholds."""
    if metric not in _RUNAWAY_POLICIES:
        raise ValueError("unsupported runaway metric: %s" % metric)
    current = _finite_float(current)
    if current is None:
        return None
    absolute_floor, relative_floor, static_warn, static_critical = (
        _RUNAWAY_POLICIES[metric])
    values = [
        value for value in (_finite_float(item) for item in baseline_values)
        if value is not None]
    mature = len(values) >= min_baseline_count
    historical = None
    if mature:
        band = robust_band(values, absolute_floor, relative_floor)
        historical = classify_deviation(current, band, "high")
    static = None
    if current >= static_warn:
        critical = current >= static_critical
        static = {
            "severity": "critical" if critical else "warn",
            "score": min(100, (80 if critical else 40)
                         + min(19, int(current / max(static_warn, 1)))),
            "value": current,
            "threshold": static_critical if critical else static_warn,
        }
    if historical is None and static is None:
        return None
    severity_rank = {"warn": 1, "critical": 2}
    if (historical is not None and (
            static is None
            or (severity_rank[historical["severity"]], historical["score"])
            >= (severity_rank[static["severity"]], static["score"]))):
        result = historical
    else:
        result = static
    result.update({
        "metric": metric,
        "band": robust_band(values, absolute_floor, relative_floor)
        if mature else None,
        "baseline_source": (
            "history_and_static_threshold"
            if mature and static is not None else
            "history" if mature else "static_threshold"),
        "baseline_count": len(values),
        "static_warn": static_warn,
        "static_critical": static_critical,
        "confidence": (
            "high" if len(values) >= 100 else
            "moderate" if len(values) >= 20 else
            "low"),
    })
    return result
