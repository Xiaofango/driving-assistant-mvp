"""CARLA-independent live data calculations."""

import math
from collections import deque
from copy import deepcopy


FRESHNESS_SECONDS = 2.0

WEATHER_PRESETS = ("clear_noon", "clear_sunset", "clear_night", "cloudy_noon",
                   "rain_noon", "hard_rain_noon", "fog_noon")
WEATHER_LABELS = {"clear_noon": "晴天", "clear_sunset": "晴天黄昏", "clear_night": "夜晚",
                  "cloudy_noon": "多云", "rain_noon": "中雨", "hard_rain_noon": "暴雨",
                  "fog_noon": "浓雾"}

SCENARIO_KINDS = ("red_light_ahead", "stalled_vehicle_ahead", "pedestrian_crossing")
SCENARIO_LABELS = {"red_light_ahead": "前方红灯冻结", "stalled_vehicle_ahead": "前方事故车",
                   "pedestrian_crossing": "行人横穿"}


class SampleBuffer:
    """Bounded scene-local sample history; the adapter owns synchronization.

    Samples are immutable copies. Rates use up to 30 monotonic receive times,
    independent of CARLA's simulation clock or the frequency of UI requests.
    """

    def __init__(self, capacity: int = 300):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("Sample buffer capacity must be a positive integer")
        self.capacity = capacity
        self._samples = deque(maxlen=capacity)
        self._received = deque(maxlen=30)
        self._total = 0
        self._last_frame = None

    def append(self, sample: dict, received_monotonic: float) -> bool:
        frame = sample.get("telemetry", {}).get("frame")
        if not isinstance(frame, int) or isinstance(frame, bool):
            return False
        if self._last_frame is not None and frame <= self._last_frame:
            return False
        if not isinstance(received_monotonic, (int, float)) or not math.isfinite(received_monotonic):
            raise ValueError("Receive time must be finite")
        if self._received and received_monotonic < self._received[-1]:
            raise ValueError("Receive time must be monotonic")
        self._samples.append(deepcopy(sample))
        self._received.append(received_monotonic)
        self._last_frame = frame
        self._total += 1
        return True

    def view(self, limit: int = 30) -> dict:
        limit = max(1, min(self.capacity, int(limit)))
        elapsed = self._received[-1] - self._received[0] if len(self._received) >= 2 else 0
        rate = round((len(self._received) - 1) / elapsed, 2) if elapsed > 0 else None
        return {"samples_total": self._total,
                "buffer_size": len(self._samples), "buffer_capacity": self.capacity,
                "dropped_samples": max(0, self._total - len(self._samples)),
                "sample_rate_hz": rate,
                "recent_samples": deepcopy(list(self._samples)[-limit:])}

    def export(self) -> list[dict]:
        return deepcopy(list(self._samples))

    def clear(self) -> None:
        self._samples.clear()
        self._received.clear()
        self._total = 0
        self._last_frame = None


def magnitude(vector) -> float:
    return math.sqrt(vector.x ** 2 + vector.y ** 2 + vector.z ** 2)


def sample_health(sample_monotonic: float | None, now: float,
                  error: str | None = None) -> tuple[str, int | None]:
    """Age the last received sample; reading does not refresh it."""
    if sample_monotonic is None:
        return ("disconnected" if error else "connecting"), None
    age_ms = max(0, round((now - sample_monotonic) * 1000))
    if age_ms > FRESHNESS_SECONDS * 1000:
        return ("disconnected" if error else "stale"), age_ms
    return "live", age_ms


def motion_state(speed_kmh: float, stationary_s: float, autopilot: str,
                 light: dict, front: dict | None, throttle: float) -> str:
    """Conservative explanation hints, not an ADAS diagnosis."""
    if speed_kmh >= 0.5:
        return "moving"
    if autopilot != "active":
        return "paused"
    if light.get("affecting") and light.get("state") == "Red":
        return "waiting_red_light"
    if front and front.get("gap_m", math.inf) <= 12 and front.get("speed_kmh", math.inf) < 2:
        return "blocked_by_vehicle"
    if stationary_s >= 8 and throttle > 0.2:
        return "possibly_stuck"
    return "stationary"
