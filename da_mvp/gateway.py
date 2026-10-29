"""Gateway boundary. The supplied implementation is an offline simulation."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol, Any

from .telemetry import WEATHER_LABELS, WEATHER_PRESETS


class Gateway(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def set_preference(self, name: str, value: int) -> dict[str, Any]: ...
    def change_route(self, route_id: str, explicit: bool = False) -> dict[str, Any]: ...
    def set_weather(self, preset: str) -> dict[str, Any]: ...
    def control_recorder(self, action: str) -> dict[str, Any]: ...
    def inject_scenario(self, kind: str) -> dict[str, Any]: ...
    def set_speed(self, target_kmh: int | None, direction: str | None = None) -> dict[str, Any]: ...
    def change_lane(self, direction: str) -> dict[str, Any]: ...
    def direct_control(self, action: str) -> dict[str, Any]: ...
    def set_destination(self, heading: str | None = None) -> dict[str, Any]: ...
    def emergency_stop(self) -> dict[str, Any]: ...


class InMemoryGateway:
    def __init__(self, state: dict[str, Any] | None = None):
        now = int(datetime.now(timezone.utc).timestamp())
        self.state = deepcopy(state) if state is not None else {
            "timestamp": now,
            "adas": {"noa_status": "active", "follow_distance_level": 2,
                     "current_speed_kmh": 88.5, "speed_limit_kmh": 120},
            "navigation": {"current_route_id": "route_a", "routes": [
                {"id": "route_a", "eta_min": 18}, {"id": "route_b", "eta_min": 15}]},
            "environment": {"weather": {"preset": "clear_noon", "label": WEATHER_LABELS["clear_noon"]}},
            "latest_event": None,
        }
        self.calls: list[tuple[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def refresh(self) -> None:
        """Advance the timestamp of the live local simulation."""
        self.state["timestamp"] = int(datetime.now(timezone.utc).timestamp())

    def set_noa_status(self, status: str) -> None:
        if status not in ("active", "off"):
            raise ValueError("NOA 状态只支持 active 或 off")
        self.state["adas"]["noa_status"] = status

    def clear_event(self) -> None:
        self.state["latest_event"] = None

    def frame(self) -> bytes | None:
        return None

    def set_camera_mode(self, mode: str) -> None:
        raise ValueError("内存模式没有 CARLA 摄像头")

    def close(self) -> None:
        pass

    def set_preference(self, name: str, value: int) -> dict[str, Any]:
        self.calls.append(("set_preference", name, value))
        event = self.state.get("latest_event")
        if isinstance(event, dict) and event.get("severity") == "critical":
            return {"accepted": False, "reason": "紧急告警优先"}
        if name != "follow_distance" or not 1 <= value <= 5:
            return {"accepted": False, "reason": "参数不受支持"}
        if self.state["adas"]["noa_status"] != "active":
            return {"accepted": False, "reason": "NOA 当前不可用"}
        self.state["adas"]["follow_distance_level"] = value
        return {"accepted": True, "name": name, "value": value}

    def change_route(self, route_id: str, explicit: bool = False) -> dict[str, Any]:
        self.calls.append(("change_route", route_id, explicit))
        event = self.state.get("latest_event")
        if isinstance(event, dict) and event.get("severity") == "critical":
            return {"accepted": False, "reason": "紧急告警优先"}
        routes = self.state["navigation"]["routes"]
        route = next((r for r in routes if r["id"] == route_id), None)
        if route is None:
            return {"accepted": False, "reason": "候选路线不存在"}
        current_id = self.state["navigation"]["current_route_id"]
        current = next((r for r in routes if r["id"] == current_id), None)
        if current_id == route_id:
            return {"accepted": False, "reason": "目标路线与当前路线相同"}
        if not explicit and (current is None or route["eta_min"] >= current["eta_min"]):
            return {"accepted": False, "reason": "目标路线并不更快"}
        self.state["navigation"]["current_route_id"] = route_id
        return {"accepted": True, "route_id": route_id, "eta_min": route["eta_min"]}

    def push_event(self, code: str, severity: str, message: str) -> None:
        self.state["latest_event"] = {"code": code, "severity": severity, "message": message}

    def set_weather(self, preset: str) -> dict[str, Any]:
        self.calls.append(("set_weather", preset))
        if preset not in WEATHER_PRESETS:
            return {"accepted": False, "reason": "不支持的天气预设"}
        weather = self.state.setdefault("environment", {}).setdefault(
            "weather", {"preset": "clear_noon", "label": WEATHER_LABELS["clear_noon"]})
        previous = weather.get("preset", "clear_noon")
        weather.update(preset=preset, label=WEATHER_LABELS[preset])
        return {"accepted": True, "preset": preset, "label": WEATHER_LABELS[preset],
                "previous": previous,
                "note": "内存演示仅记录天气标签，不产生真实天气效果"}

    def control_recorder(self, action: str) -> dict[str, Any]:
        self.calls.append(("control_recorder", action))
        return {"accepted": False, "reason": "内存模式没有 CARLA 录制器"}

    def inject_scenario(self, kind: str) -> dict[str, Any]:
        self.calls.append(("inject_scenario", kind))
        return {"accepted": False, "reason": "内存模式没有 CARLA 场景注入"}

    def set_speed(self, target_kmh: int | None, direction: str | None = None) -> dict[str, Any]:
        self.calls.append(("set_speed", target_kmh, direction))
        return {"accepted": False, "reason": "内存模式没有 CARLA 车辆速度控制"}

    def change_lane(self, direction: str) -> dict[str, Any]:
        self.calls.append(("change_lane", direction))
        return {"accepted": False, "reason": "内存模式没有 CARLA 变道控制"}

    def direct_control(self, action: str) -> dict[str, Any]:
        self.calls.append(("direct_control", action))
        return {"accepted": False, "reason": "内存模式没有 CARLA 直接车辆控制"}

    def set_destination(self, heading: str | None = None) -> dict[str, Any]:
        self.calls.append(("set_destination", heading))
        return {"accepted": True, "heading": heading or "未指定",
                "note": "内存模式仅记录目的地标签，不产生真实导航"}

    def emergency_stop(self) -> dict[str, Any]:
        self.calls.append(("emergency_stop",))
        self.state["adas"]["noa_status"] = "off"
        return {"accepted": True, "handbrake": True}

