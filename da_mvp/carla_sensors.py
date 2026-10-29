"""Sensor suite mixin: RGB camera, event sensors, extended perception sensors.

Callbacks run on CARLA worker threads; every writer takes the gateway lock.
Parsing uses plain struct/bytes (no numpy). Values enter the snapshot as
aggregated numbers only — no image streams beyond the JPEG camera frame.
"""

import io
import math
import struct
import time
from typing import Any

from .carla_config import SEMANTIC_TAGS, SENSOR_NAMES
from .telemetry import FRESHNESS_SECONDS, magnitude, sample_health


class SensorMixin:
    """All sensor spawning, callbacks and aggregated sensor state."""

    def _spawn_camera(self):
        camera_bp = self.world.get_blueprint_library().find("sensor.camera.rgb")
        for name, value in (("image_size_x", "800"), ("image_size_y", "450"),
                            ("fov", "100"), ("sensor_tick", "0.15")):
            camera_bp.set_attribute(name, value)
        pose = (self.carla.Transform(self.carla.Location(x=1.6, z=1.75),
                                     self.carla.Rotation(pitch=-3))
                if self.camera_mode == "front" else
                self.carla.Transform(self.carla.Location(x=-6.0, z=3.0),
                                     self.carla.Rotation(pitch=-12)))
        with self.lock:
            self._camera_generation += 1
            generation = self._camera_generation
        self.camera = self.world.spawn_actor(camera_bp, pose, attach_to=self.ego)
        self.owned_actors.append(self.camera)
        self.sensors.append(self.camera)
        self.camera.listen(lambda image: self._on_image(image, generation))

    def _spawn_event_sensors(self):
        for blueprint, callback in (("sensor.other.collision", self._on_collision),
                                    ("sensor.other.lane_invasion", self._on_lane_invasion)):
            try:
                sensor = self.world.spawn_actor(self.world.get_blueprint_library().find(blueprint),
                                                self.carla.Transform(), attach_to=self.ego)
                self.owned_actors.append(sensor)
                self.sensors.append(sensor)
                sensor.listen(callback)
            except Exception as exc:
                self._sensor_errors.append(f"{blueprint}: {type(exc).__name__}: {exc}")

    def _spawn_extended_sensors(self):
        """Attach the extra sensing suite. Failures degrade, never abort the demo."""
        library = self.world.get_blueprint_library()
        plans = []
        try:
            gnss_bp = library.find("sensor.other.gnss")
            gnss_bp.set_attribute("sensor_tick", "0.5")
            plans.append((gnss_bp, self._on_gnss))
        except Exception as exc:
            self._sensor_errors.append(f"gnss: {type(exc).__name__}: {exc}")
        try:
            imu_bp = library.find("sensor.other.imu")
            imu_bp.set_attribute("sensor_tick", "0.1")
            plans.append((imu_bp, self._on_imu))
        except Exception as exc:
            self._sensor_errors.append(f"imu: {type(exc).__name__}: {exc}")
        try:
            lidar_bp = library.find("sensor.lidar.ray_cast")
            for name, value in (("channels", "16"), ("range", "30"), ("points_per_second", "24000"),
                                ("rotation_frequency", "10"), ("upper_fov", "10"), ("lower_fov", "-30"),
                                ("sensor_tick", "0.2"), ("dropoff_general_rate", "0"),
                                ("dropoff_intensity_limit", "0"), ("dropoff_zero_intensity", "0")):
                lidar_bp.set_attribute(name, value)
            plans.append((lidar_bp, self._on_lidar))
        except Exception as exc:
            self._sensor_errors.append(f"lidar: {type(exc).__name__}: {exc}")
        try:
            radar_bp = library.find("sensor.other.radar")
            for name, value in (("horizontal_fov", "25.0"), ("vertical_fov", "10.0"),
                                ("range", "40.0"), ("points_per_second", "1500"),
                                ("sensor_tick", "0.2")):
                radar_bp.set_attribute(name, value)
            plans.append((radar_bp, self._on_radar))
        except Exception as exc:
            self._sensor_errors.append(f"radar: {type(exc).__name__}: {exc}")
        camera_pose = self.carla.Transform(self.carla.Location(x=1.6, z=1.75),
                                           self.carla.Rotation(pitch=-3))
        try:
            seg_bp = library.find("sensor.camera.semantic_segmentation")
            for name, value in (("image_size_x", "300"), ("image_size_y", "168"),
                                ("fov", "90"), ("sensor_tick", "0.5")):
                seg_bp.set_attribute(name, value)
            plans.append((seg_bp, self._on_segmentation))
        except Exception as exc:
            self._sensor_errors.append(f"segmentation: {type(exc).__name__}: {exc}")
        try:
            depth_bp = library.find("sensor.camera.depth")
            for name, value in (("image_size_x", "300"), ("image_size_y", "168"),
                                ("fov", "90"), ("sensor_tick", "0.5")):
                depth_bp.set_attribute(name, value)
            plans.append((depth_bp, self._on_depth))
        except Exception as exc:
            self._sensor_errors.append(f"depth: {type(exc).__name__}: {exc}")
        try:
            obstacle_bp = library.find("sensor.other.obstacle")
            for name, value in (("distance", "18.0"), ("hit_radius", "0.5"),
                                ("only_dynamics", "False"), ("sensor_tick", "0.2")):
                obstacle_bp.set_attribute(name, value)
            plans.append((obstacle_bp, self._on_obstacle))
        except Exception as exc:
            self._sensor_errors.append(f"obstacle: {type(exc).__name__}: {exc}")

        mounts = {"sensor.other.gnss": self.carla.Transform(self.carla.Location(z=1.8)),
                  "sensor.other.imu": self.carla.Transform(self.carla.Location(z=1.6)),
                  "sensor.lidar.ray_cast": self.carla.Transform(self.carla.Location(z=2.0)),
                  "sensor.other.radar": self.carla.Transform(self.carla.Location(x=2.45, z=0.9)),
                  "sensor.camera.semantic_segmentation": camera_pose,
                  "sensor.camera.depth": camera_pose,
                  "sensor.other.obstacle": self.carla.Transform(self.carla.Location(x=2.65, z=1.0))}
        for blueprint, callback in plans:
            try:
                sensor = self.world.spawn_actor(blueprint, mounts[blueprint.id], attach_to=self.ego)
                self.owned_actors.append(sensor)
                self.sensors.append(sensor)
                sensor.listen(callback)
            except Exception as exc:
                self._sensor_errors.append(f"{blueprint.id}: {type(exc).__name__}: {exc}")

    def _store_sensor(self, name: str, frame: int | None, value: dict[str, Any],
                      monotonic: float | None = None) -> None:
        if self._stop.is_set() or (frame is not None and frame < self._minimum_frame):
            return
        with self.lock:
            self._sensor_data[name] = {**value, "frame": frame,
                                       "monotonic": monotonic if monotonic is not None else time.monotonic()}

    def _on_gnss(self, event):
        self._store_sensor("gnss", event.frame,
                           {"latitude": round(event.latitude, 6),
                            "longitude": round(event.longitude, 6),
                            "altitude_m": round(event.altitude, 2)})

    def _on_imu(self, event):
        accel, gyro = event.accelerometer, event.gyroscope
        self._store_sensor("imu", event.frame,
                           {"accelerometer": [round(accel.x, 3), round(accel.y, 3), round(accel.z, 3)],
                            "gyroscope": [round(gyro.x, 4), round(gyro.y, 4), round(gyro.z, 4)],
                            "compass_deg": round(math.degrees(event.compass), 1)})

    def _on_lidar(self, image):
        nearest = None
        total = 0
        # float32 quadruples: x forward, y left, z up, relative to the roof mount.
        # x>3.0 skips the ego body (extent ~2.5 m); z>-0.7 skips ground and hood.
        for x, y, z, _intensity in struct.iter_unpack("<4f", image.raw_data):
            total += 1
            if x <= 3.0 or abs(y) > 3.0 or not -0.7 < z < 1.6:
                continue
            distance = math.sqrt(x * x + y * y)
            if nearest is None or distance < nearest:
                nearest = distance
        self._store_sensor("lidar", image.frame,
                           {"points": total, "nearest_obstacle_m": round(nearest, 1) if nearest else None,
                            "range_m": 30.0,
                            "method": "点云中前向锥形区域内最近回波；非目标识别"})

    def _on_radar(self, measurement):
        targets = sorted(({"distance_m": round(d.depth, 1),
                           "azimuth_deg": round(math.degrees(d.azimuth), 1),
                           "velocity_mps": round(d.velocity, 1)}
                          for d in measurement if d.depth >= 1.0),
                         key=lambda t: t["distance_m"])[:8]
        self._store_sensor("radar", measurement.frame,
                           {"targets": targets, "target_count": len(targets),
                            "nearest_m": targets[0]["distance_m"] if targets else None,
                            "range_m": 40.0})

    def _on_segmentation(self, image):
        raw = bytes(image.raw_data)
        counts = {label: 0 for label in SEMANTIC_TAGS.values()}
        # BGRA pixels; the semantic tag occupies the red byte.
        for index in range(2, len(raw), 8):
            label = SEMANTIC_TAGS.get(raw[index])
            if label is not None:
                counts[label] += 1
        self._store_sensor("segmentation", image.frame,
                           {"pixels_by_class": counts,
                            "people_in_view": counts["pedestrian"] + counts["rider"] > 40,
                            "resolution": f"{image.width}x{image.height}",
                            "method": "语义分割像素统计；非目标级检测"})

    def _on_depth(self, image):
        raw = bytes(image.raw_data)
        nearest = None
        denominator = 256 ** 3 - 1
        stride = image.width * 4
        hood_rows = int(image.height * 0.55) * stride
        # Sample every 3rd pixel in the upper 55% of the frame (the ego hood fills
        # the bottom): depth = (R + G*256 + B*256^2) / (256^3-1) * 1000 m.
        for index in range(0, min(len(raw), hood_rows), 12):
            blue, green, red = raw[index], raw[index + 1], raw[index + 2]
            distance = (red + green * 256 + blue * 65536) / denominator * 1000.0
            if distance < 2.5 or distance > 80.0:
                continue
            if nearest is None or distance < nearest:
                nearest = distance
        self._store_sensor("depth", image.frame,
                           {"nearest_in_view_m": round(nearest, 1) if nearest else None,
                            "method": "画面上半部采样像素的最小深度；非障碍物识别"})

    def _on_obstacle(self, event):
        if event.distance < 0.3:
            # Sub-30 cm readings are the bumper-adjacent static geometry, not objects.
            return
        other = event.other_actor
        self._store_sensor("obstacle", event.frame,
                           {"distance_m": round(event.distance, 2),
                            "actor_id": other.id if other is not None else None,
                            "actor_type": other.type_id if other is not None else None})

    def _on_image(self, image, generation=None):
        if self._stop.is_set():
            return
        received = time.monotonic()
        try:
            from PIL import Image
            rgb = Image.frombytes("RGBA", (image.width, image.height),
                                  bytes(image.raw_data), "raw", "BGRA").convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=75)
            with self.lock:
                if generation is not None and generation != self._camera_generation:
                    return
                if image.frame < self._minimum_frame:
                    return
                self.latest_frame = output.getvalue()
                self._camera_monotonic = received
                self._camera_frame = image.frame
                self._camera_time = image.timestamp
                self._camera_error = None
        except Exception as exc:
            with self.lock:
                self._camera_error = f"摄像头帧处理失败：{type(exc).__name__}"

    def _on_collision(self, event):
        other = event.other_actor
        with self.lock:
            previous = self._collision_seen.get(other.id, -math.inf)
            if event.timestamp - previous < 1.0:
                return
            self._collision_seen[other.id] = event.timestamp
        self._append_event("carla_sensor", "collision", "warning",
                           f"CARLA 碰撞传感器记录到与 {other.type_id} 的接触。",
                           event.frame, event.timestamp, other_actor_id=other.id,
                           other_actor_type=other.type_id,
                           impulse_ns=round(magnitude(event.normal_impulse), 2))

    def _on_lane_invasion(self, event):
        markings = sorted({str(mark.type).split(".")[-1] for mark in event.crossed_lane_markings})
        self._append_event("carla_sensor", "lane_invasion", "info",
                           f"CARLA 压线传感器记录到跨越标线：{', '.join(markings)}。",
                           event.frame, event.timestamp, markings=markings)

    def frame(self) -> bytes | None:
        with self.lock:
            if self._camera_monotonic is None or time.monotonic() - self._camera_monotonic > FRESHNESS_SECONDS:
                return None
            return self.latest_frame

    def set_camera_mode(self, mode: str) -> None:
        if mode not in ("front", "chase"):
            raise ValueError("视角只支持 front 或 chase")
        if mode == self.camera_mode:
            return
        old = self.camera
        with self.lock:
            self._camera_generation += 1
            self.latest_frame = None
            self._camera_monotonic = None
            self._camera_frame = None
            self._camera_time = None
        if old is not None:
            old.stop()
            old.destroy()
            self.owned_actors.remove(old)
            self.sensors.remove(old)
            self.camera = None
        self.camera_mode = mode
        self._spawn_camera()

    def _sensor_summary(self, now: float) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for name in SENSOR_NAMES:
            entry = self._sensor_data.get(name)
            if entry is None:
                summary[name] = {"status": "waiting"}
                continue
            value = {key: entry_value for key, entry_value in entry.items() if key != "monotonic"}
            status, age_ms = sample_health(entry.get("monotonic"), now, None)
            summary[name] = {**value, "status": status, "age_ms": age_ms}
        return summary
