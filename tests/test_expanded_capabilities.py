"""Tests for the expanded CARLA capabilities: sensors, weather, scenarios, recorder."""

import unittest
from datetime import datetime, timezone

from da_mvp import DrivingAssistant, InMemoryGateway
from da_mvp.intent import resolve
from da_mvp.telemetry import WEATHER_LABELS, WEATHER_PRESETS


def request(text):
    return {"session_id": "s1", "user_id": "u1", "utterance": text,
            "context": {}, "timestamp": int(datetime.now(timezone.utc).timestamp())}


def carla_snapshot(**overrides):
    now = int(datetime.now(timezone.utc).timestamp())
    base = {
        "timestamp": now, "source": "carla", "map": "Carla/Maps/Town10HD_Opt",
        "telemetry": {"frame": 1234, "simulation_time_s": 260.0, "status": "live",
                      "age_ms": 120, "error": None, "sensor_errors": []},
        "adas": {"noa_status": "active", "follow_distance_level": 2,
                 "current_speed_kmh": 31.5, "speed_limit_kmh": 50.0},
        "vehicle": {"road_id": 6, "lane_id": 1, "is_junction": False,
                    "location": {"x": -107.8, "y": -35.0, "z": 0.0}},
        "environment": {
            "traffic_light": {"affecting": True, "state": "Red", "light_id": 9,
                              "state_elapsed_s": 3.2, "state_time_s": 10.0,
                              "state_remaining_s": 6.8, "frozen_by_scenario": False,
                              "phase_source": "carla_traffic_light_timer_estimate"},
            "front_vehicle": None, "nearby_vehicle_count": 2,
            "weather": {"preset": "rain_noon", "label": WEATHER_LABELS["rain_noon"],
                        "precipitation_pct": 80.0, "fog_density_pct": 5.0,
                        "wetness_pct": 60.0, "sun_altitude_deg": 45.0},
            "pedestrians": {"nearby_count_30m": 2, "nearest_m": 12.5,
                            "riders_nearby_60m": 1}},
        "sensors": {
            "gnss": {"status": "live", "latitude": 0.00042, "longitude": 0.00009,
                     "altitude_m": 100.2},
            "imu": {"status": "live", "accelerometer": [0.1, 0.0, 9.8],
                    "gyroscope": [0.0, 0.0, 0.0], "compass_deg": 107.4},
            "lidar": {"status": "live", "points": 4820, "nearest_obstacle_m": 18.2},
            "radar": {"status": "live", "nearest_m": 17.5, "target_count": 3, "targets": []},
            "depth": {"status": "live", "nearest_in_view_m": 19.0},
            "obstacle": {"status": "live", "distance_m": 16.9,
                         "actor_type": "vehicle.ford.mustang"},
            "segmentation": {"status": "live", "people_in_view": False,
                             "pixels_by_class": {"pedestrian": 0, "rider": 0,
                                                 "vehicle": 900, "traffic_light": 30}}},
        "navigation": {"current_route_id": "route_a", "eta_source": "geometric_demo_estimate",
                       "routes": [{"id": "route_a", "eta_min": 5}, {"id": "route_b", "eta_min": 7}]},
        "latest_event": None, "events": [],
        "recorder": {"active": False, "path": None},
        "scenario": {"active_kind": None}, "simulation_mode": "async",
    }
    base.update(overrides)
    return base


class IntentParsingTests(unittest.TestCase):
    def test_weather_phrases_map_to_query_or_write(self):
        self.assertEqual(resolve("现在天气怎么样").kind, "weather_query")
        self.assertEqual(resolve("下雨了吗").kind, "weather_query")
        self.assertEqual(resolve("下雨").option, "rain_noon")
        self.assertEqual(resolve("下大雨").option, "hard_rain_noon")
        self.assertEqual(resolve("起雾").option, "fog_noon")
        self.assertEqual(resolve("天黑").option, "clear_night")
        self.assertEqual(resolve("晴天").option, "clear_noon")

    def test_recorder_and_scenario_phrases(self):
        self.assertEqual((resolve("开始录制").kind, resolve("开始录制").option),
                         ("recorder_control", "start"))
        self.assertEqual(resolve("停止录制").option, "stop")
        self.assertEqual(resolve("模拟红灯场景").option, "red_light_ahead")
        self.assertEqual(resolve("前面放一辆事故车").option, "stalled_vehicle_ahead")
        self.assertEqual(resolve("模拟行人横穿马路").option, "pedestrian_crossing")
        self.assertEqual(resolve("清除场景").option, "clear")

    def test_new_read_queries(self):
        self.assertEqual(resolve("附近有行人吗").kind, "pedestrian_query")
        self.assertEqual(resolve("最近障碍物多远").kind, "obstacle_query")
        self.assertEqual(resolve("红灯还有多久变绿").kind, "light_phase_query")
        self.assertEqual(resolve("我们现在在哪").kind, "gps_query")

    def test_existing_phrases_still_resolve_unchanged(self):
        self.assertEqual(resolve("跟车太近了").kind, "follow_distance")
        self.assertEqual(resolve("帮我变道").kind, "lane_change")
        self.assertEqual(resolve("到目的地还有多久").kind, "eta_query")
        self.assertEqual(resolve("前面堵车，换条不堵车的路").kind, "faster_route")
        self.assertEqual(resolve("前车离我多远").kind, "front_distance")


class MemoryGatewayCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.gateway = InMemoryGateway()
        self.assistant = DrivingAssistant(self.gateway)

    def test_weather_write_records_label_in_memory(self):
        response = self.assistant.handle(request("下雨"))
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(self.gateway.state["environment"]["weather"]["preset"], "rain_noon")
        self.assertIn("内存演示仅记录标签", response["reply_text"])

    def test_weather_query_reads_recorded_label(self):
        self.gateway.set_weather("fog_noon")
        response = self.assistant.handle(request("现在天气怎么样"))
        self.assertEqual(response["action_result"], "success")
        self.assertIn("浓雾", response["reply_text"])

    def test_recorder_and_scenario_are_rejected_in_memory(self):
        recorder = self.assistant.handle(request("开始录制"))
        self.assertEqual(recorder["action_result"], "unavailable")
        self.assertIn("内存模式没有 CARLA 录制器", recorder["reply_text"])
        scenario = self.assistant.handle(request("模拟红灯场景"))
        self.assertEqual(scenario["action_result"], "unavailable")
        self.assertIn("内存模式没有 CARLA 场景注入", scenario["reply_text"])

    def test_perception_queries_need_carla_source(self):
        for utterance, marker in (("附近有行人吗", "行人观测"),
                                  ("最近障碍物多远", "激光雷达"),
                                  ("我们现在在哪", "GNSS")):
            response = self.assistant.handle(request(utterance))
            self.assertEqual(response["action_result"], "unavailable", utterance)
            self.assertIn(marker, response["reply_text"], utterance)

    def test_unknown_preset_is_refused(self):
        ack = self.gateway.set_weather("snow_noon")
        self.assertFalse(ack["accepted"])
        self.assertNotIn("snow_noon", WEATHER_PRESETS)


class CarlaSnapshotCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.gateway = InMemoryGateway()
        self.assistant = DrivingAssistant(self.gateway)
        self.snapshot = carla_snapshot()

    def handle(self, text, snapshot=None):
        return self.assistant.handle(request(text), snapshot=snapshot or self.snapshot)

    def test_weather_query_reports_carla_parameters(self):
        response = self.handle("现在天气怎么样")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("中雨", response["reply_text"])
        self.assertIn("雨量 80", response["reply_text"])
        self.assertEqual(response["action_detail"]["weather"]["preset"], "rain_noon")

    def test_weather_write_requires_gateway_ack(self):
        response = self.handle("天黑")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("夜晚", response["reply_text"])
        ack_calls = [call for call in self.gateway.calls if call[0] == "set_weather"]
        self.assertEqual(ack_calls, [("set_weather", "clear_night")])

    def test_pedestrian_query_reports_truth_and_view(self):
        response = self.handle("附近有行人吗")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("2 名行人", response["reply_text"])
        self.assertIn("12.5 米", response["reply_text"])
        self.assertIn("未检出行人", response["reply_text"])

    def test_obstacle_query_merges_sensor_observations(self):
        response = self.handle("最近障碍物多远")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("激光雷达最近回波约 18.2 米", response["reply_text"])
        self.assertIn("毫米波雷达最近目标约 17.5 米", response["reply_text"])
        self.assertIn("不是目标识别", response["reply_text"])

    def test_obstacle_query_flags_stale_sensors_instead_of_using_them(self):
        stale = carla_snapshot()
        stale["sensors"]["lidar"]["status"] = "stale"
        stale["sensors"]["radar"]["status"] = "stale"
        response = self.handle("最近障碍物多远", snapshot=stale)
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["reply_text"].count("数据stale，未计入当前结论"), 2)
        self.assertIn("前视深度画面最近距离约 19 米；", response["reply_text"])

    def test_light_phase_reports_remaining_estimate(self):
        response = self.handle("红灯还有多久变绿")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("红灯", response["reply_text"])
        self.assertIn("6.8 秒", response["reply_text"])

    def test_light_phase_without_affecting_light_is_no_change(self):
        free = carla_snapshot()
        free["environment"]["traffic_light"] = {"affecting": False, "state": None}
        response = self.handle("红灯还有多久变绿", snapshot=free)
        self.assertEqual(response["action_result"], "no_change")
        self.assertIn("不受任何信号灯约束", response["reply_text"])

    def test_gps_query_reports_coordinates_and_road(self):
        response = self.handle("我们现在在哪")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("0.00042", response["reply_text"])
        self.assertIn("道路 6", response["reply_text"])

    def test_gps_query_refuses_non_live_gnss(self):
        stale = carla_snapshot()
        stale["sensors"]["gnss"] = {"status": "stale"}
        response = self.handle("我们现在在哪", snapshot=stale)
        self.assertEqual(response["action_result"], "unavailable")
        self.assertIn("未就绪", response["reply_text"])

    def test_scenario_write_requires_gateway_ack(self):
        response = self.handle("模拟红灯场景")
        self.assertEqual(response["action_result"], "unavailable")
        self.assertIn("内存模式", response["reply_text"])

    def test_stale_telemetry_blocks_new_write_intents(self):
        stale = carla_snapshot()
        stale["telemetry"]["status"] = "stale"
        response = self.handle("下雨", snapshot=stale)
        self.assertEqual(response["action_result"], "unavailable")
        self.assertIn("CARLA 状态未就绪、已延迟或连接中断", response["reply_text"])


if __name__ == "__main__":
    unittest.main()
