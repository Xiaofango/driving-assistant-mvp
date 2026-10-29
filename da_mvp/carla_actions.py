"""Write-operation mixin: preferences, routes, weather, recorder, scenarios,
driving commands (speed / lane / direct control), destination, emergency stop,
synchronous mode and operator events.

Every method returns an explicit ack dict; the DA service only reports success
when the ack confirms the target. All writes share the freshness/critical gates
except emergency_stop, which is allowed to bypass them by design.
"""

import math
import threading
import time
from pathlib import Path
from typing import Any

from .carla_config import FOLLOW_DISTANCE_METERS, weather_preset_parameters
from .telemetry import (SCENARIO_KINDS, SCENARIO_LABELS, WEATHER_LABELS,
                        WEATHER_PRESETS)


class ActionsMixin:
    """Gateway write operations; state lives on the gateway instance."""

    def set_preference(self, name: str, value: int) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("set_preference", name, value))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        if name != "follow_distance" or value not in FOLLOW_DISTANCE_METERS:
            return {"accepted": False, "reason": "参数不受支持"}
        try:
            self.tm.distance_to_leading_vehicle(self.ego, FOLLOW_DISTANCE_METERS[value])
        except Exception as exc:
            return {"accepted": False, "reason": str(exc)}
        with self.lock:
            self.follow_level = value
        return {"accepted": True, "name": name, "value": value,
                "distance_m": FOLLOW_DISTANCE_METERS[value]}

    def change_route(self, route_id: str, explicit: bool = False) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("change_route", route_id, explicit))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        if route_id not in ("route_a", "route_b") or route_id == self.current_route_id:
            return {"accepted": False, "reason": "目标路线不可切换（相同或不存在）"}
        path = [self.via, self.destination] if route_id == "route_a" else [self.destination]
        try:
            self.tm.set_path(self.ego, path)
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.current_route_id = route_id
        label = "原始路线（经途经点）" if route_id == "route_a" else "备选路线（直达）"
        self._append_event("operator", "route_selected", "info",
                           f"DA 已向 TM 提交{label}路径；行驶轨迹由 TM 决定。")
        eta = next(r["eta_min"] for r in self.snapshot()["navigation"]["routes"] if r["id"] == route_id)
        return {"accepted": True, "route_id": route_id, "eta_min": eta,
                "note": "CARLA Traffic Manager 已接收路径；ETA 为几何演示估计"}

    def set_weather(self, preset: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("set_weather", preset))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        if preset not in WEATHER_PRESETS:
            return {"accepted": False, "reason": "不支持的天气预设"}
        parameters = weather_preset_parameters(self.carla, preset)
        if parameters is None:
            return {"accepted": False, "reason": f"CARLA 缺少天气预设 {preset}"}
        try:
            self.world.set_weather(parameters)
        except Exception as exc:
            return {"accepted": False, "reason": str(exc)}
        with self.lock:
            previous = self._weather_preset
            self._weather_preset = preset
            self._weather_refreshed = 0.0
        self._append_event("operator", "weather_changed", "info",
                           f"DA 已把 CARLA 天气从 {WEATHER_LABELS.get(previous, previous)}"
                           f"切换为 {WEATHER_LABELS[preset]}。")
        return {"accepted": True, "preset": preset, "label": WEATHER_LABELS[preset],
                "previous": previous}

    def _read_weather(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._weather_cache is not None and now - self._weather_refreshed < 2.0:
            return dict(self._weather_cache)
        try:
            weather = self.world.get_weather()
            self._weather_cache = {"preset": self._weather_preset,
                                   "label": WEATHER_LABELS.get(self._weather_preset, self._weather_preset),
                                   "cloudiness_pct": round(weather.cloudiness, 0),
                                   "precipitation_pct": round(weather.precipitation, 0),
                                   "wetness_pct": round(weather.wetness, 0),
                                   "fog_density_pct": round(weather.fog_density, 0),
                                   "fog_distance_m": round(weather.fog_distance, 0),
                                   "wind_intensity_pct": round(weather.wind_intensity, 0),
                                   "sun_altitude_deg": round(weather.sun_altitude_angle, 0),
                                   "source": "carla_weather_parameters"}
            self._weather_refreshed = now
        except Exception:
            if self._weather_cache is None:
                self._weather_cache = {"preset": self._weather_preset,
                                       "label": WEATHER_LABELS.get(self._weather_preset, self._weather_preset),
                                       "source": "carla_weather_parameters_stale"}
        return dict(self._weather_cache)

    def control_recorder(self, action: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("control_recorder", action))
        if action not in ("start", "stop"):
            return {"accepted": False, "reason": "录制器只支持 start 或 stop"}
        if action == "start" and self._recorder_active:
            return {"accepted": True, "action": "start", "path": self._recorder_path,
                    "note": "录制已在进行中，本请求未重复开启"}
        if action == "stop" and not self._recorder_active:
            return {"accepted": True, "action": "stop", "note": "当前没有正在进行的录制"}
        try:
            if action == "start":
                directory = Path(__file__).resolve().parents[1] / "tmp" / "recordings"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / time.strftime("da-demo-%Y%m%dT%H%M%S.log")
                # additional_data=False: record actor transforms only, keep the file small.
                self.client.start_recorder(str(path), False)
                with self.lock:
                    self._recorder_active = True
                    self._recorder_path = str(path)
                self._append_event("operator", "recorder_started", "info",
                                   f"已开始 CARLA 录制（不含传感器附加数据）：{path.name}")
            else:
                self.client.stop_recorder()
                with self.lock:
                    self._recorder_active = False
                self._append_event("operator", "recorder_stopped", "info",
                                   "CARLA 录制已停止；文件可用 CARLA 回放工具重放（回放会重载仿真世界）。")
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        return {"accepted": True, "action": action, "path": self._recorder_path}

    def inject_scenario(self, kind: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("inject_scenario", kind))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        if kind == "clear":
            return self._clear_scenario()
        if kind not in SCENARIO_KINDS:
            return {"accepted": False, "reason": "不支持的注入场景"}
        if self._scenario is not None:
            return {"accepted": False,
                    "reason": f"已有注入场景（{SCENARIO_LABELS.get(self._scenario.get('kind'), '?')}），请先清除"}
        try:
            if kind == "red_light_ahead":
                actors = self._freeze_next_red_light()
            elif kind == "stalled_vehicle_ahead":
                actors = self._spawn_stalled_vehicles()
            else:
                actors = self._spawn_crossing_pedestrians()
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not actors:
            return {"accepted": False, "reason": "场景注入没有产生任何仿真对象"}
        with self.lock:
            self._scenario = {"kind": kind, "actors": actors}
        self._append_event("operator", "scenario_injected", "warning",
                           f"已注入演示场景：{SCENARIO_LABELS[kind]}。仿真交通会对此作出反应。")
        return {"accepted": True, "kind": kind, "label": SCENARIO_LABELS[kind],
                "note": "场景由 DA 通过 CARLA 接口注入；说“清除场景”可恢复"}

    def _freeze_next_red_light(self):
        light = self.ego.get_traffic_light()
        if light is None:
            forward = self.ego.get_transform().get_forward_vector()
            location = self.ego.get_location()
            best = None
            for actor in self.world.get_actors().filter("traffic.traffic_light*"):
                try:
                    trigger_center = actor.get_transform().transform(
                        actor.get_trigger_volume().location)
                except Exception:
                    continue
                delta = trigger_center - location
                longitudinal = delta.x * forward.x + delta.y * forward.y
                if 2.0 < longitudinal < 90.0 and (best is None or longitudinal < best[0]):
                    best = (longitudinal, actor)
            if best is None:
                raise RuntimeError("前方 90 米内没有可注入的信号灯")
            light = best[1]
        original = {"state": light.get_state(), "red_time": light.get_red_time()}
        light.set_state(self.carla.TrafficLightState.Red)
        light.set_red_time(60.0)
        try:
            light.freeze(True)
        except Exception:
            pass
        return [{"type": "traffic_light", "id": light.id, "original": {
            "state": str(original["state"]).split(".")[-1], "red_time": original["red_time"]}}]

    def _spawn_stalled_vehicles(self):
        waypoint = self.map.get_waypoint(self.ego.get_location(), project_to_road=True,
                                         lane_type=self.carla.LaneType.Driving)
        if waypoint is None:
            raise RuntimeError("当前没有可定位的车道")
        spawned = []
        for distance in (30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 95.0, 110.0):
            ahead = waypoint.next(distance)
            if not ahead or ahead[0].is_junction:
                continue
            blueprint = self._vehicle_blueprint()
            if blueprint.has_attribute("role_name"):
                blueprint.set_attribute("role_name", "stalled")
            # Waypoint transforms sit at road level; lift the spawn so the
            # bounding box clears the pavement, otherwise CARLA reports collision.
            base = ahead[0].transform
            pose = self.carla.Transform(self.carla.Location(x=base.location.x,
                                                            y=base.location.y,
                                                            z=base.location.z + 0.5),
                                        base.rotation)
            actor = self.world.try_spawn_actor(blueprint, pose)
            if actor is None:
                continue
            actor.apply_control(self.carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))
            self.owned_actors.append(actor)
            spawned.append({"type": "vehicle", "id": actor.id})
        if not spawned:
            raise RuntimeError("前方车道没有可用的停车注入位置")
        return spawned

    def _spawn_crossing_pedestrians(self):
        waypoint = self.map.get_waypoint(self.ego.get_location(), project_to_road=True,
                                         lane_type=self.carla.LaneType.Driving)
        if waypoint is None:
            raise RuntimeError("当前没有可定位的车道")
        ahead = waypoint.next(28.0)
        if not ahead:
            raise RuntimeError("前方没有可用的行人注入位置")
        origin = ahead[0].transform.location
        right = ahead[0].transform.get_right_vector()
        offset = waypoint.lane_width + 1.0
        transform = self.carla.Transform(self.carla.Location(
            x=origin.x + right.x * offset, y=origin.y + right.y * offset, z=origin.z + 0.5))
        library = self.world.get_blueprint_library()
        walker_blueprints = library.filter("walker.pedestrian.*")
        if not walker_blueprints:
            raise RuntimeError("地图没有行人蓝图")
        blueprint = walker_blueprints[0]
        if blueprint.has_attribute("is_invincible"):
            blueprint.set_attribute("is_invincible", "false")
        walker = self.world.try_spawn_actor(blueprint, transform)
        if walker is None:
            raise RuntimeError("行人注入位置被占用")
        self.owned_actors.append(walker)
        controller = self.world.try_spawn_actor(library.find("controller.ai.walker"),
                                                self.carla.Transform(), attach_to=walker)
        if controller is None:
            raise RuntimeError("无法生成行人控制器")
        self.owned_actors.append(controller)
        self.walker_controllers.append(controller)
        controller.start()
        controller.go_to_location(self.carla.Location(
            x=origin.x - right.x * offset, y=origin.y - right.y * offset, z=origin.z))
        controller.set_max_speed(1.8)
        return [{"type": "walker", "id": walker.id, "controller_id": controller.id}]

    def _clear_scenario(self) -> dict[str, Any]:
        if self._scenario is None:
            return {"accepted": True, "kind": "clear", "note": "当前没有注入场景"}
        kind = self._scenario.get("kind")
        for record in self._scenario.get("actors", []):
            try:
                if record["type"] == "traffic_light":
                    light = next((a for a in self.world.get_actors().filter("traffic.traffic_light*")
                                  if a.id == record["id"]), None)
                    if light is not None:
                        try:
                            light.freeze(False)
                        except Exception:
                            pass
                        light.set_red_time(record["original"]["red_time"])
                        light.set_state(getattr(
                            self.carla.TrafficLightState, record["original"]["state"],
                            self.carla.TrafficLightState.Green))
                else:
                    if "controller_id" in record:
                        controller = next((a for a in self.world.get_actors()
                                           if a.id == record["controller_id"]), None)
                        if controller is not None and controller.is_alive:
                            controller.stop()
                            controller.destroy()
                            self._forget_actor(controller)
                    actor = next((a for a in self.world.get_actors() if a.id == record["id"]), None)
                    if actor is not None and actor.is_alive:
                        actor.destroy()
                        self._forget_actor(actor)
            except Exception as exc:
                self._sensor_errors.append(f"scenario_clear:{record.get('id')}: {type(exc).__name__}: {exc}")
        with self.lock:
            self._scenario = None
        self._append_event("operator", "scenario_cleared", "info",
                           f"已清除注入场景（{SCENARIO_LABELS.get(kind, kind)}），交通恢复自主运行。")
        return {"accepted": True, "kind": "clear", "cleared": kind}

    def _forget_actor(self, actor) -> None:
        if actor in self.owned_actors:
            self.owned_actors.remove(actor)
        if actor in self.sensors:
            self.sensors.remove(actor)
        if actor in self.walker_controllers:
            self.walker_controllers.remove(actor)

    def set_speed(self, target_kmh: int | None, direction: str | None = None) -> dict[str, Any]:
        """Layer-1 driving command: TM keeps driving, only the target changes."""
        with self.lock:
            self.calls.append(("set_speed", target_kmh, direction))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        limit = self.ego.get_speed_limit()
        if not isinstance(limit, (int, float)) or limit <= 0:
            return {"accepted": False, "reason": "当前没有可用的限速基准"}
        if target_kmh is None:
            base = self._speed_target_kmh if self._speed_target_kmh else round(limit)
            target_kmh = base + (10 if direction == "fast" else -10)
        try:
            target_kmh = int(round(target_kmh))
        except (TypeError, ValueError):
            return {"accepted": False, "reason": "目标车速必须是数字"}
        if target_kmh > limit:
            return {"accepted": False, "reason": f"目标车速超过当前限速 {limit:.0f} km/h，不发送超速请求"}
        if target_kmh < 5:
            return {"accepted": False, "reason": "目标车速过低；如需停车请说“暂停自动驾驶”"}
        difference = (limit - target_kmh) / limit * 100.0
        try:
            self.tm.vehicle_percentage_speed_difference(self.ego, difference)
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            previous = self._speed_target_kmh
            self._speed_target_kmh = target_kmh
            self._speed_difference_pct = round(difference, 1)
        self._append_event("operator", "speed_target_set", "info",
                           f"DA 已把 TM 目标车速设为 {target_kmh} km/h（限速 {limit:.0f}，"
                           f"偏移 {difference:+.1f}%）；实际车速受跟车与路况影响。")
        return {"accepted": True, "target_kmh": target_kmh, "previous_kmh": previous,
                "speed_limit_kmh": round(limit), "difference_pct": round(difference, 1)}

    def change_lane(self, direction: str) -> dict[str, Any]:
        """Submit a lane-change path to TM; TM performs the manoeuvre itself."""
        with self.lock:
            self.calls.append(("change_lane", direction))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        if direction not in ("left", "right"):
            return {"accepted": False, "reason": "请说明向左还是向右变道"}
        now = time.monotonic()
        if self._lane_change_at is not None and now - self._lane_change_at < 6.0:
            return {"accepted": False, "reason": "刚提交过变道路径，请等当前变道完成后再试"}
        waypoint = self.map.get_waypoint(self.ego.get_location(), project_to_road=True,
                                         lane_type=self.carla.LaneType.Driving)
        if waypoint is None:
            return {"accepted": False, "reason": "当前没有可定位的车道"}
        if waypoint.is_junction:
            return {"accepted": False, "reason": "当前在路口范围内，拒绝变道请求"}
        target_wp = waypoint.get_left_lane() if direction == "left" else waypoint.get_right_lane()
        if (target_wp is None or target_wp.lane_type != self.carla.LaneType.Driving
                or (target_wp.lane_id > 0) != (waypoint.lane_id > 0)):
            side = "左" if direction == "left" else "右"
            return {"accepted": False, "reason": f"{side}侧没有可行驶的同向车道"}
        route = [self.via, self.destination] if self.current_route_id == "route_a" else [self.destination]
        try:
            self.tm.set_path(self.ego, [target_wp.transform.location, *route])
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self._lane_change_at = now
        self._append_event("operator", "lane_change_requested", "info",
                           f"DA 已向 TM 提交向{'左' if direction == 'left' else '右'}变道路径"
                           f"（车道 {waypoint.lane_id} → {target_wp.lane_id}）；执行由 TM 完成。")
        return {"accepted": True, "direction": direction,
                "from_lane": waypoint.lane_id, "to_lane": target_wp.lane_id,
                "note": "路径已提交；实际变道由 TM 执行并受交通状况影响"}

    def direct_control(self, action: str) -> dict[str, Any]:
        """Layer-2 stepping control, only while TM autopilot is paused."""
        with self.lock:
            self.calls.append(("direct_control", action))
        if self.snapshot()["telemetry"]["status"] != "live":
            return {"accepted": False, "reason": "CARLA 状态未就绪，不能执行直接控制"}
        if self.noa_status != "off":
            return {"accepted": False,
                    "reason": "TM 驾驶中不接受直接车辆控制；请先说“暂停自动驾驶”，或用“开到 40”/“向左变道”下达意图"}
        steps = {"throttle_up": (0.25, 0.75), "brake": None, "throttle_off": None,
                 "steer_left": (-0.3, -0.6), "steer_right": (0.3, 0.6), "steer_center": None}
        if action not in steps:
            return {"accepted": False, "reason": "不支持的控制动作"}
        control = dict(self._direct_control)
        if action == "throttle_up":
            control["throttle"] = min(steps[action][1], control["throttle"] + steps[action][0])
            control["brake"] = 0.0
        elif action == "throttle_off":
            control["throttle"] = 0.0
        elif action == "brake":
            control["throttle"] = 0.0
            control["brake"] = 1.0
        elif action == "steer_left":
            control["steer"] = max(steps[action][1], control["steer"] + steps[action][0])
        elif action == "steer_right":
            control["steer"] = min(steps[action][1], control["steer"] + steps[action][0])
        elif action == "steer_center":
            control["steer"] = 0.0
        try:
            self.ego.apply_control(self.carla.VehicleControl(throttle=control["throttle"],
                                                             brake=control["brake"],
                                                             steer=control["steer"]))
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self._direct_control = control
        self._append_event("operator", "direct_control", "info",
                           f"直接控制步进 {action}：油门 {control['throttle']:.2f}、"
                           f"刹车 {control['brake']:.2f}、转向 {control['steer']:.2f}。")
        return {"accepted": True, "action": action, "control": control,
                "note": "步进控制会保持到下一条指令；恢复自动驾驶后清除"}

    def set_destination(self, heading: str | None = None) -> dict[str, Any]:
        """Pick a new far spawn point (optionally in a compass bucket) and re-path TM."""
        with self.lock:
            self.calls.append(("set_destination", heading))
        issue = self._write_issue()
        if issue:
            return {"accepted": False, "reason": issue}
        start = self.ego.get_location()
        candidates = [p for p in self.map.get_spawn_points()
                      if 200.0 < p.location.distance(start) < 800.0]
        if not candidates:
            candidates = sorted((p for p in self.map.get_spawn_points()
                                 if p.location.distance(start) > 100.0),
                                key=lambda p: -p.location.distance(start))[:5]
        if not candidates:
            return {"accepted": False, "reason": "地图上没有足够远的目的地候选"}

        def bucket(point):
            # Map frame: +x east, +y south; 8-way compass bucket of the target.
            angle = (math.degrees(math.atan2(-(point.location.y - start.y),
                                             point.location.x - start.x)) + 360.0) % 360.0
            return ["东", "东北", "北", "西北", "西", "西南", "南", "东南"][int((angle + 22.5) // 45) % 8]

        if heading in ("东", "南", "西", "北", "东北", "东南", "西北", "西南"):
            directed = [p for p in candidates if bucket(p) == heading]
            pool = directed or candidates
        else:
            pool = [p for p in candidates if p.location.distance(self.destination) > 100.0] or candidates
        chosen = min(pool, key=lambda p: abs(p.location.distance(start) - 450.0))
        chosen_bucket = bucket(chosen)
        self.destination = chosen.location
        direct = start.distance(self.destination)
        vias = [p for p in self.map.get_spawn_points()
                if p.location.distance(start) > 80 and p.location.distance(self.destination) > 80]
        if vias:
            self.via = min(vias, key=lambda v: abs(
                (start.distance(v.location) + v.location.distance(self.destination)) / direct - 1.55)).location
        try:
            self.tm.set_path(self.ego, [self.via, self.destination])
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.current_route_id = "route_a"
            self._lane_change_at = None
        self._append_event("operator", "destination_changed", "info",
                           f"DA 已把目的地改为{chosen_bucket}方向约 "
                           f"{chosen.location.distance(start):.0f} 米处，并重新提交 TM 路径。")
        return {"accepted": True, "heading": chosen_bucket,
                "requested_heading": heading or "未指定",
                "distance_m": round(chosen.location.distance(start)),
                "eta_min": self._eta(start, [self.via, self.destination]),
                "note": "路径已提交；实际导航由 TM 完成"}

    def emergency_stop(self) -> dict[str, Any]:
        """Immediate stop: no freshness gate, allowed during critical alerts."""
        with self.lock:
            self.calls.append(("emergency_stop",))
        try:
            self.ego.set_autopilot(False, self.tm_port)
            self.ego.apply_control(self.carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))
        except Exception as exc:
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self.noa_status = "off"
            self._direct_control = {"throttle": 0.0, "brake": 1.0, "steer": 0.0}
        self._append_event("operator", "emergency_stop", "warning",
                           "执行了紧急停车：TM 已断开，满刹车并拉起手刹。")
        return {"accepted": True, "handbrake": True,
                "note": "紧急停车不依赖数据新鲜度；恢复说“恢复自动驾驶”"}

    def set_sync_mode(self, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            return {"accepted": False, "reason": "同步模式开关必须是布尔值"}
        with self.lock:
            self.calls.append(("set_sync_mode", enabled))
        if enabled == self._sync_enabled:
            return {"accepted": True, "enabled": enabled, "note": "仿真模式未变化"}
        self._sync_stop.set()
        collector = self._collector
        if collector is not None and collector is not threading.current_thread():
            collector.join(timeout=3.0)
        try:
            settings = self.world.get_settings()
            previous = (settings.synchronous_mode, settings.fixed_delta_seconds)
            if enabled:
                self._async_fixed_delta = settings.fixed_delta_seconds or 0.0
                settings.synchronous_mode = True
                settings.fixed_delta_seconds = 0.05
            else:
                settings.synchronous_mode = False
                settings.fixed_delta_seconds = getattr(self, "_async_fixed_delta", 0.0) or 0.0
            self.world.apply_settings(settings)
            try:
                # CARLA names it set_synchronous_mode; older bindings set_sync_mode.
                if hasattr(self.tm, "set_synchronous_mode"):
                    self.tm.set_synchronous_mode(enabled)
                elif hasattr(self.tm, "set_sync_mode"):
                    self.tm.set_sync_mode(enabled)
            except Exception as exc:
                # A frozen synchronous world would starve the async collector:
                # undo the world settings before surfacing the failure.
                rollback = self.world.get_settings()
                rollback.synchronous_mode, rollback.fixed_delta_seconds = previous
                self.world.apply_settings(rollback)
                raise RuntimeError(f"TrafficManager 同步设置失败：{type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            self._restart_collector()
            return {"accepted": False, "reason": f"{type(exc).__name__}: {exc}"}
        with self.lock:
            self._sync_enabled = enabled
            self._sync_stop = threading.Event()
            self._cached = None
            self._sample_monotonic = None
        self._restart_collector()
        return {"accepted": True, "enabled": enabled,
                "fixed_delta_s": 0.05 if enabled else None}

    def _critical(self) -> bool:
        with self.lock:
            return bool(self.latest_event and self.latest_event.get("severity") == "critical")

    def push_event(self, code: str, severity: str, message: str) -> None:
        with self.lock:
            frame = self._cached.get("telemetry", {}).get("frame") if self._cached else None
            sim_time = self._cached.get("telemetry", {}).get("simulation_time_s") if self._cached else None
        self._append_event("operator", code, severity, message, frame, sim_time)

    def clear_event(self) -> None:
        with self.lock:
            self.latest_event = None

    def set_noa_status(self, status: str) -> None:
        if status not in ("active", "off"):
            raise ValueError("NOA 状态只支持 active 或 off")
        if status == self.noa_status:
            return
        if status == "active" and self.snapshot()["telemetry"]["status"] != "live":
            raise RuntimeError("CARLA 状态未就绪，不能恢复 TM")
        self.ego.set_autopilot(status == "active", self.tm_port)
        if status == "off":
            self.ego.apply_control(self.carla.VehicleControl(throttle=0.0, brake=1.0))
            with self.lock:
                self._direct_control = {"throttle": 0.0, "brake": 1.0, "steer": 0.0}
        else:
            self.tm.distance_to_leading_vehicle(self.ego, FOLLOW_DISTANCE_METERS[self.follow_level])
            path = [self.via, self.destination] if self.current_route_id == "route_a" else [self.destination]
            self.tm.set_path(self.ego, path)
            self.tm.vehicle_percentage_speed_difference(self.ego, self._speed_difference_pct)
        with self.lock:
            self.noa_status = status
