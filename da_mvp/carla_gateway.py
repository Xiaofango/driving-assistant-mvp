"""CARLA 0.9.16 live input adapter. Traffic Manager owns actuation.

WorldSnapshot supplies coherent kinematics; camera and events keep their own
frame/time. Actor/map truth is a simulator test input, not camera perception.
Route ETA remains a geometry demo estimate.

This module is the composition point and runtime core: connection, lifecycle,
the collection loop, world sampling and the snapshot. Feature groups live in
mixins:

- :class:`da_mvp.carla_scene.SceneMixin` — hero vehicle, routes, NPC traffic,
  pedestrians and riders;
- :class:`da_mvp.carla_sensors.SensorMixin` — camera, event sensors and the
  extended perception suite with all callbacks;
- :class:`da_mvp.carla_actions.ActionsMixin` — preferences, routes, weather,
  recorder, scenario injection, driving commands, destination, emergency stop,
  synchronous mode and operator events.
"""

import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .carla_actions import ActionsMixin
from .carla_config import FOLLOW_DISTANCE_METERS
from .carla_scene import SceneMixin
from .carla_sensors import SensorMixin
from .telemetry import SampleBuffer, magnitude, motion_state, sample_health


class CarlaGateway(SceneMixin, SensorMixin, ActionsMixin):
    def __init__(self, host: str = "127.0.0.1", port: int = 2000,
                 town: str | None = None, traffic_port: int = 8000,
                 osm_map: str | None = None):
        try:
            import carla
        except ImportError as exc:
            raise RuntimeError("缺少 CARLA Python 客户端：请用 .venv\\Scripts\\python.exe 启动") from exc
        self.carla = carla
        self.client = carla.Client(host, port)
        self.client.set_timeout(8.0)
        try:
            if osm_map:
                self._load_osm_world(osm_map)
            self.world = self.client.get_world()
            if town and town not in self.world.get_map().name:
                self.world = self.client.load_world(town)
        except Exception as exc:
            raise RuntimeError(f"无法连接 CARLA {host}:{port}：{exc}") from exc
        if self.world.get_settings().synchronous_mode:
            raise RuntimeError("CARLA 服务器处于同步模式；请由仿真服务器持续 tick，或切换异步模式")
        self.lock = threading.RLock()
        self.latest_frame: bytes | None = None
        self.latest_event: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.owned_actors = []
        self.sensors = []
        self.ego = None
        self.camera = None
        self.camera_mode = "front"
        self.follow_level = 2
        self.noa_status = "active"
        self.current_route_id = "route_a"
        self._cached: dict[str, Any] | None = None
        self._sample_buffer = SampleBuffer(capacity=300)
        self._sample_monotonic: float | None = None
        self._camera_monotonic: float | None = None
        self._camera_frame: int | None = None
        self._camera_time: float | None = None
        self._camera_generation = 0
        self._camera_error: str | None = None
        self._last_error: str | None = None
        self._sensor_errors: list[str] = []
        self._last_frame: int | None = None
        self._minimum_frame = 0
        self._stationary_since: float | None = None
        self._vehicle_actors = []
        self._actors_refreshed = 0.0
        self._event_seq = 0
        self._collision_seen: dict[int, float] = {}
        self._sensor_data: dict[str, dict[str, Any]] = {}
        self._walker_actors = []
        self.walker_controllers = []
        self._walker_refreshed = 0.0
        self._rider_ids: set[int] = set()
        self._weather_preset = "clear_noon"
        self._weather_cache: dict[str, Any] | None = None
        self._weather_refreshed = 0.0
        self._recorder_active = False
        self._recorder_path: str | None = None
        self._scenario: dict[str, Any] | None = None
        self._speed_target_kmh: int | None = None
        self._speed_difference_pct: float = 10.0
        self._lane_change_at: float | None = None
        self._direct_control = {"throttle": 0.0, "brake": 1.0, "steer": 0.0}
        self._sync_enabled = False
        self._sync_stop = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._collector: threading.Thread | None = None
        self.tm = self.client.get_trafficmanager(traffic_port)
        self.tm_port = self.tm.get_port()
        self.map = self.world.get_map()
        try:
            self._spawn_scene()
            self._spawn_event_sensors()
            self._spawn_extended_sensors()
            self._spawn_walkers()
            # Keep runtime collection/cleanup bounded when the server disappears.
            self.client.set_timeout(2.0)
            self._collector = threading.Thread(target=self._collect_loop,
                                                name="carla-telemetry", daemon=True)
            self._collector.start()
            self._ready.wait(3.0)
        except Exception:
            self.close()
            raise

    def _load_osm_world(self, osm_path: str) -> None:
        """Import a local OpenStreetMap file as the driving world."""
        source = Path(osm_path)
        if not source.is_file():
            raise RuntimeError(f"OSM 文件不存在：{source}")
        converter = getattr(self.carla, "Osm2Odrl", None)
        if converter is None:
            raise RuntimeError("当前 CARLA 客户端不支持 Osm2Odrl 导入")
        odrl = converter(source.read_text(encoding="utf-8"))
        self.world = self.client.generate_opendrive_world(odrl)
        print(f"[carla] 已从 OSM 文件生成自定义路网：{source.name}", flush=True)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _append_event(self, source, code, severity, message, frame=None,
                      simulation_time_s=None, **extra):
        if self._stop.is_set():
            return
        with self.lock:
            if source == "carla_sensor" and frame is not None and frame < self._minimum_frame:
                return
            self._event_seq += 1
            event = {"id": self._event_seq, "source": source, "code": code,
                     "severity": severity, "message": message, "frame": frame,
                     "simulation_time_s": simulation_time_s,
                     "timestamp": int(time.time()), **extra}
            self.events.append(event)
            self.events = self.events[-20:]
            if not self.latest_event or self.latest_event.get("severity") != "critical" or severity == "critical":
                self.latest_event = event

    # ------------------------------------------------------------------
    # Continuous collection
    # ------------------------------------------------------------------

    def _restart_collector(self) -> None:
        self._collector = threading.Thread(target=self._collect_loop,
                                           name="carla-telemetry", daemon=True)
        self._collector.start()

    def _collect_loop(self):
        while not self._stop.is_set() and not self._sync_stop.is_set():
            started = time.monotonic()
            try:
                if self._sync_enabled:
                    # Synchronous mode: this loop owns the 20 Hz world tick.
                    # World.tick() returns the new frame id, not a snapshot.
                    self.world.tick()
                    world_snapshot = self.world.get_snapshot()
                else:
                    world_snapshot = self.world.wait_for_tick(1.0)
                received_mono, received_wall = time.monotonic(), time.time()
                if world_snapshot is None:
                    raise RuntimeError("等待 CARLA 世界新帧超时")
                if world_snapshot.frame == self._last_frame:
                    continue
                sample = self._read_world_sample(world_snapshot)
                with self.lock:
                    if (world_snapshot.frame < self._minimum_frame or
                            (self._last_frame is not None and world_snapshot.frame <= self._last_frame)):
                        continue
                    self._cached = sample
                    self._cached["timestamp"] = int(received_wall)
                    self._sample_monotonic = received_mono
                    self._last_frame = world_snapshot.frame
                    self._last_error = None
                    # Capture the full input as seen at collection time, including
                    # current TM settings and event metadata; never store JPEG bytes.
                    self._sample_buffer.append(self.snapshot(), received_mono)
                self._ready.set()
            except Exception as exc:
                with self.lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
            if self._sync_enabled:
                self._stop.wait(max(0.0, 0.05 - (time.monotonic() - started)))
            else:
                self._stop.wait(0.2)

    # ------------------------------------------------------------------
    # World sampling (per-frame snapshot readers)
    # ------------------------------------------------------------------

    def _read_world_sample(self, world_snapshot):
        ego = self.ego
        actor_snapshot = world_snapshot.find(ego.id) if ego else None
        if actor_snapshot is None:
            raise RuntimeError("CARLA 世界快照中找不到演示车辆")
        pose = actor_snapshot.get_transform()
        velocity = actor_snapshot.get_velocity()
        acceleration = actor_snapshot.get_acceleration()
        location = pose.location
        speed = magnitude(velocity) * 3.6
        sim_time = world_snapshot.timestamp.elapsed_seconds
        waypoint = self.map.get_waypoint(location, project_to_road=False,
                                        lane_type=self.carla.LaneType.Driving)
        control = ego.get_control()
        affecting = ego.is_at_traffic_light()
        light = {"affecting": affecting,
                 "state": str(ego.get_traffic_light_state()).split(".")[-1] if affecting else None}
        if affecting:
            light.update(self._light_phase(ego.get_traffic_light()))
        front, nearby_count = self._read_surroundings(world_snapshot, pose, waypoint)
        pedestrians = self._read_pedestrians(world_snapshot, pose)
        weather = self._read_weather()
        if speed < 0.5:
            if self._stationary_since is None:
                self._stationary_since = sim_time
            stationary_s = max(0.0, sim_time - self._stationary_since)
        else:
            self._stationary_since = None
            stationary_s = 0.0
        with self.lock:
            autopilot = self.noa_status
        speed_limit = ego.get_speed_limit()
        return {
            "source": "carla", "map": self.map.name,
            "telemetry": {"frame": world_snapshot.frame,
                          "simulation_time_s": round(sim_time, 3),
                          "source": "carla_world_snapshot", "sample_period_s": 0.2,
                          "sensor_errors": list(self._sensor_errors)},
            "vehicle": {"id": ego.id,
                        "location": {axis: round(getattr(location, axis), 2) for axis in ("x", "y", "z")},
                        "heading_deg": round(pose.rotation.yaw, 1),
                        "acceleration_mps2": round(magnitude(acceleration), 2),
                        "road_id": waypoint.road_id if waypoint else None,
                        "lane_id": waypoint.lane_id if waypoint else None,
                        "is_junction": waypoint.is_junction if waypoint else None,
                        "control": {"throttle": round(control.throttle, 3),
                                    "brake": round(control.brake, 3),
                                    "steer": round(control.steer, 3)},
                        "control_source": "carla_latest_tick"},
            "adas": {"current_speed_kmh": round(speed, 1),
                     "speed_limit_kmh": round(speed_limit, 1) if speed_limit and speed_limit > 0 else None},
            "environment": {"traffic_light": light, "front_vehicle": front,
                            "nearby_vehicle_count": nearby_count, "nearby_radius_m": 60,
                            "weather": weather, "pedestrians": pedestrians,
                            "source": "carla_actor_map_ground_truth",
                            "front_vehicle_method": "同一道路车道、同向前方车辆的车身几何间距；非视觉识别",
                            "motion": {"state": motion_state(speed, stationary_s, autopilot, light, front, control.throttle),
                                       "stationary_s": round(stationary_s, 1)}},
            "navigation": {"eta_source": "geometric_demo_estimate",
                           "destination_distance_m": round(location.distance(self.destination)),
                           "routes": [{"id": "route_a", "eta_min": self._eta(location, [self.via, self.destination])},
                                      {"id": "route_b", "eta_min": self._eta(location, [self.destination])}]}
        }

    def _read_surroundings(self, world_snapshot, pose, waypoint):
        now = time.monotonic()
        if now - self._actors_refreshed >= 1.0:
            self._vehicle_actors = list(self.world.get_actors().filter("vehicle.*"))
            self._actors_refreshed = now
        nearest, count = None, 0
        forward = pose.get_forward_vector()
        for other in self._vehicle_actors:
            if other.id == self.ego.id:
                continue
            other_sample = world_snapshot.find(other.id)
            if other_sample is None:
                continue
            other_pose = other_sample.get_transform()
            delta = other_pose.location - pose.location
            if magnitude(delta) > 60:
                continue
            count += 1
            if waypoint is None:
                continue
            longitudinal = delta.x * forward.x + delta.y * forward.y
            other_forward = other_pose.get_forward_vector()
            aligned = forward.x * other_forward.x + forward.y * other_forward.y
            if longitudinal <= 0 or aligned < 0.7:
                continue
            other_waypoint = self.map.get_waypoint(other_pose.location, project_to_road=False,
                                                   lane_type=self.carla.LaneType.Driving)
            if other_waypoint is None or (other_waypoint.road_id, other_waypoint.lane_id) != (waypoint.road_id, waypoint.lane_id):
                continue
            lateral = abs(-delta.x * forward.y + delta.y * forward.x)
            if lateral > max(1.5, waypoint.lane_width * 0.65):
                continue
            gap = max(0.0, longitudinal - self.ego.bounding_box.extent.x - other.bounding_box.extent.x)
            if nearest is None or gap < nearest["gap_m"]:
                nearest = {"id": other.id, "gap_m": round(gap, 1),
                           "speed_kmh": round(magnitude(other_sample.get_velocity()) * 3.6, 1),
                           "source": "carla_actor_map_ground_truth"}
        return nearest, count

    def _light_phase(self, light_actor) -> dict[str, Any]:
        try:
            state = str(light_actor.get_state()).split(".")[-1]
            duration = {"Red": light_actor.get_red_time(),
                        "Yellow": light_actor.get_yellow_time(),
                        "Green": light_actor.get_green_time()}.get(state)
            elapsed = light_actor.get_elapsed_time()
            remaining = max(0.0, duration - elapsed) if isinstance(duration, (int, float)) else None
            frozen = bool(self._scenario and self._scenario.get("kind") == "red_light_ahead")
            return {"light_id": light_actor.id,
                    "state_elapsed_s": round(elapsed, 1) if isinstance(elapsed, (int, float)) else None,
                    "state_time_s": round(duration, 1) if isinstance(duration, (int, float)) else None,
                    "state_remaining_s": round(remaining, 1) if remaining is not None else None,
                    "frozen_by_scenario": frozen,
                    "phase_source": "carla_traffic_light_timer_estimate"}
        except Exception as exc:
            return {"phase_source": f"unavailable: {type(exc).__name__}: {exc}"}

    def _read_pedestrians(self, world_snapshot, pose) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._walker_refreshed >= 1.0:
            try:
                self._walker_actors = list(self.world.get_actors().filter("walker.pedestrian.*"))
                self._walker_refreshed = now
            except Exception:
                pass
        nearby = 0
        nearest = None
        for walker in self._walker_actors:
            walker_sample = world_snapshot.find(walker.id)
            if walker_sample is None:
                continue
            delta = walker_sample.get_transform().location - pose.location
            distance = magnitude(delta)
            if distance > 30:
                continue
            nearby += 1
            surface = max(0.0, distance - 0.5)
            if nearest is None or surface < nearest:
                nearest = surface
        riders = sum(1 for actor in self._vehicle_actors
                     if actor.id in self._rider_ids
                     and world_snapshot.find(actor.id) is not None
                     and (magnitude(world_snapshot.find(actor.id).get_transform().location
                                    - pose.location)) <= 60)
        return {"nearby_count_30m": nearby,
                "nearest_m": round(nearest, 1) if nearest is not None else None,
                "riders_nearby_60m": riders,
                "source": "carla_actor_map_ground_truth"}

    def refresh(self) -> None:
        # Data is continuously sampled. Commands cannot refresh an old timestamp.
        pass

    # ------------------------------------------------------------------
    # Snapshot and stream APIs
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            data = deepcopy(self._cached) if self._cached else {
                "timestamp": None, "source": "carla", "map": self.map.name,
                "adas": {"current_speed_kmh": None, "speed_limit_kmh": None},
                "vehicle": {}, "environment": {},
                "navigation": {"eta_source": "geometric_demo_estimate", "routes": []},
                "telemetry": {"frame": None, "simulation_time_s": None,
                              "source": "carla_world_snapshot", "sensor_errors": list(self._sensor_errors)}
            }
            telemetry = data.get("telemetry", {})
            if isinstance(telemetry, dict) and telemetry.get("sensor_errors") is None:
                telemetry["sensor_errors"] = list(self._sensor_errors)
            status, age = sample_health(self._sample_monotonic, now, self._last_error)
            data["telemetry"].update(status=status, age_ms=age, error=self._last_error)
            data["sensors"] = self._sensor_summary(now)
            data["recorder"] = {"active": self._recorder_active, "path": self._recorder_path}
            data["scenario"] = {"active_kind": self._scenario["kind"]} if self._scenario else {"active_kind": None}
            data["simulation_mode"] = "sync" if self._sync_enabled else "async"
            camera_status, camera_age = sample_health(self._camera_monotonic, now, self._camera_error)
            data["camera"] = {"status": "waiting" if self._camera_monotonic is None else (
                                    "live" if camera_status == "live" else "stale"),
                              "frame": self._camera_frame, "simulation_time_s": self._camera_time,
                              "age_ms": camera_age, "error": self._camera_error}
            data["camera_available"] = data["camera"]["status"] == "live"
            data["camera_mode"] = self.camera_mode
            data["adas"].update(noa_status=self.noa_status,
                                  follow_distance_level=self.follow_level,
                                  follow_distance_m=FOLLOW_DISTANCE_METERS[self.follow_level],
                                  speed_target_kmh=self._speed_target_kmh,
                                  tm_speed_difference_pct=self._speed_difference_pct,
                                  direct_control=(dict(self._direct_control)
                                                  if self.noa_status == "off" else None),
                                  setting_source="local_tm_command_state")
            data["navigation"]["current_route_id"] = self.current_route_id
            data["latest_event"] = deepcopy(self.latest_event)
            data["events"] = deepcopy(self.events)
            return data

    def _write_issue(self):
        state = self.snapshot()
        if state["telemetry"]["status"] != "live":
            return "CARLA 状态未就绪、延迟或连接中断"
        if self._critical() or self.noa_status != "active":
            return "TM 未启用或存在紧急告警"
        return None

    def telemetry_stream(self, limit: int = 30) -> dict[str, Any]:
        """Expose continuous scene-local input history without creating a sample.

        sample_rate_hz is the average of up to 30 latest receive intervals.
        snapshot holds current health; historical samples keep capture-time health.
        """
        with self.lock:
            return {"source": "carla", **self._sample_buffer.view(limit),
                    "snapshot": self.snapshot()}

    def export_samples(self) -> list[dict]:
        """Export the newest 300 inputs; callers may serialize them as JSONL."""
        with self.lock:
            return self._sample_buffer.export()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset_demo(self) -> None:
        """Reset the demonstration without recreating the native client/TM."""
        if self.snapshot()["telemetry"]["status"] != "live" or not self.ego.is_alive:
            raise RuntimeError("CARLA 尚未实时连接，请恢复仿真服务后再重置。")
        locations = [actor.get_location() for actor in self.world.get_actors().filter("vehicle.*")
                     if actor.id != self.ego.id]
        pose = next((p for p in self.map.get_spawn_points()
                     if all(p.location.distance(other) > 8 for other in locations)), None)
        if pose is None:
            raise RuntimeError("没有空闲出生点，当前场景保持不变。")
        self.ego.set_autopilot(False, self.tm_port)
        self.ego.set_target_velocity(self.carla.Vector3D())
        self.ego.set_target_angular_velocity(self.carla.Vector3D())
        self.ego.apply_control(self.carla.VehicleControl(throttle=0, brake=1))
        self.ego.set_transform(pose)
        self.ego.apply_control(self.carla.VehicleControl(throttle=0, brake=0))
        self.ego.set_autopilot(True, self.tm_port)
        self.tm.distance_to_leading_vehicle(self.ego, FOLLOW_DISTANCE_METERS[2])
        self.tm.set_path(self.ego, [self.via, self.destination])
        self.tm.vehicle_percentage_speed_difference(self.ego, 10.0)
        # Return the world to its unmodified baseline: no injected scenario,
        # no recorder session, default weather, default driving commands.
        if self._scenario is not None:
            self._clear_scenario()
        if self._recorder_active:
            self.control_recorder("stop")
        if self._weather_preset != "clear_noon":
            self.set_weather("clear_noon")
        minimum_frame = self.world.get_snapshot().frame + 1
        with self.lock:
            self._minimum_frame = minimum_frame
            self._follow_level = 2
            self.current_route_id = "route_a"
            self.noa_status = "active"
            self.latest_event = None
            self.events = []
            self._collision_seen.clear()
            self._stationary_since = None
            self._sensor_data.clear()
            self._speed_target_kmh = None
            self._speed_difference_pct = 10.0
            self._lane_change_at = None
            self._direct_control = {"throttle": 0.0, "brake": 1.0, "steer": 0.0}
            # Wait for an actual post-reset frame rather than relabeling old data.
            self._cached = None
            self._sample_monotonic = None
            self._sample_buffer.clear()
            self.latest_frame = None
            self._camera_monotonic = None
            self._camera_frame = None
            self._camera_time = None

    def close(self) -> None:
        self._stop.set()
        self._sync_stop.set()
        self.client.set_timeout(1.0)
        if self._sync_enabled:
            try:
                settings = self.world.get_settings()
                settings.synchronous_mode = False
                settings.fixed_delta_seconds = getattr(self, "_async_fixed_delta", 0.0) or 0.0
                self.world.apply_settings(settings)
            except Exception:
                pass
        collector = self._collector
        if collector and collector is not threading.current_thread():
            collector.join(timeout=3.0)
        if self._recorder_active:
            try:
                self.client.stop_recorder()
            except Exception:
                pass
            self._recorder_active = False
        for controller in list(self.walker_controllers):
            try:
                controller.stop()
            except Exception:
                pass
        for sensor in list(self.sensors):
            try:
                sensor.stop()
            except Exception:
                pass
        for actor in reversed(self.owned_actors):
            try:
                if actor.is_alive:
                    actor.destroy()
            except Exception:
                pass
        self.owned_actors = []
        self.sensors = []
        self.ego = None
