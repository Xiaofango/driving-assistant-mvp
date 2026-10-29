"""Deterministic safety checks before any gateway write request."""

from datetime import datetime, timezone
import math
from typing import Any


ALLOWED_WRITE_TOOLS = frozenset({"set_driving_preference", "modify_navigation",
                                 "set_weather", "control_recorder", "inject_scenario",
                                 "set_speed", "change_lane", "direct_control",
                                 "set_driving_state"})


def check_fresh(snapshot: dict[str, Any], now: int | None = None) -> str | None:
    telemetry = snapshot.get("telemetry")
    if snapshot.get("source") == "carla" and isinstance(telemetry, dict):
        age = telemetry.get("age_ms")
        if (telemetry.get("status") != "live" or isinstance(age, bool)
                or not isinstance(age, (int, float)) or not math.isfinite(age) or not 0 <= age <= 2000):
            return "CARLA 状态未就绪、已延迟或连接中断，请等待新数据后重试。"
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp())
    stamp = snapshot.get("timestamp")
    if not isinstance(stamp, int) or isinstance(stamp, bool) or abs(now - stamp) > 5:
        return "驾驶状态已过期，请稍后重试。"
    return None


def check_preference(snapshot: dict[str, Any], value: int) -> str | None:
    if snapshot.get("adas", {}).get("noa_status") != "active":
        return "NOA 当前不可用，无法调整跟车距离。"
    if not isinstance(value, int) or not 1 <= value <= 5:
        return "跟车距离已到可调边界，无法继续调整。"
    return None


def faster_route(snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    nav = snapshot.get("navigation", {})
    routes = nav.get("routes", [])
    current = next((r for r in routes if r.get("id") == nav.get("current_route_id")), None)
    if current is None or not isinstance(current.get("eta_min"), (int, float)):
        return None, 0
    eligible = [r for r in routes if r.get("id") != current["id"]
                and isinstance(r.get("eta_min"), (int, float)) and r["eta_min"] >= 0
                and r["eta_min"] < current["eta_min"]]
    if not eligible:
        return None, 0
    best = min(eligible, key=lambda r: r["eta_min"])
    return best, int(current["eta_min"] - best["eta_min"])

