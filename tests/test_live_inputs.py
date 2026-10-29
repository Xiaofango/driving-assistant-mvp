"""DA input regressions using labeled observations, without a CARLA server."""

from copy import deepcopy
import time
import unittest

from da_mvp import DrivingAssistant, InMemoryGateway
from da_mvp.intent import resolve
from da_mvp.safety import check_fresh
from da_mvp.telemetry import motion_state, sample_health


def request(text):
    return {
        "session_id": "live-input-test", "user_id": "test-user",
        "utterance": text, "context": {}, "timestamp": int(time.time()),
    }


def live_observation():
    return {
        "timestamp": int(time.time()), "source": "carla",
        "telemetry": {"status": "live", "frame": 123,
                      "simulation_time_s": 15.2, "age_ms": 100},
        "camera": {"status": "stale", "age_ms": 4000},
        "adas": {"noa_status": "active", "current_speed_kmh": 0.0,
                 "speed_limit_kmh": 30, "follow_distance_level": 2,
                 "follow_distance_m": 5.0},
        "navigation": {"current_route_id": "route_a", "routes": [
            {"id": "route_a", "eta_min": 0.5},
            {"id": "route_b", "eta_min": 0.4},
        ]},
        "vehicle": {"road_id": 12, "lane_id": -2, "is_junction": False,
                    "location": {"x": 1, "y": 2, "z": 0},
                    "control": {"throttle": 0.1, "brake": 0, "steer": 0}},
        "environment": {
            "traffic_light": {"affecting": True, "state": "Red"},
            "front_vehicle": {"id": 7, "gap_m": 12.4, "speed_kmh": 0},
            "motion": {"state": "waiting_red_light", "stationary_s": 5},
            "nearby_vehicle_count": 2,
        },
        "events": [{"source": "carla_sensor", "code": "collision",
                    "message": "检测到碰撞", "frame": 122}],
        "latest_event": None,
    }


class LiveInputTests(unittest.TestCase):
    def setUp(self):
        self.gateway = InMemoryGateway()
        self.assistant = DrivingAssistant(self.gateway)
        self.observation = live_observation()

    def ask(self, text):
        return self.assistant.handle(request(text), snapshot=self.observation)

    def test_new_queries_route_to_read_only_intents(self):
        for text, kind in (
            ("为什么停了", "stop_reason"),
            ("前车离我多远", "front_distance"),
            ("当前道路和车道", "road_context"),
            ("最近发生了什么", "recent_events"),
        ):
            with self.subTest(text=text):
                self.assertEqual(resolve(text).kind, kind)

    def test_reply_uses_supplied_snapshot_and_preserves_its_provenance(self):
        # A fresh gateway read would provide a different observation. The reply
        # must remain tied to the snapshot saved for this turn.
        self.gateway.snapshot = lambda: (_ for _ in ()).throw(
            AssertionError("the explicit observation should be used"))
        before = deepcopy(self.observation)
        response = self.ask("前车离我多远")
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["action_detail"]["observation"], {
            "source": "carla", "timestamp": before["timestamp"],
            "frame": 123, "simulation_time_s": 15.2,
            "telemetry_status": "live",
        })
        self.assertEqual(self.observation, before)
        self.assertEqual(self.gateway.calls, [])

    def test_front_gap_query_uses_actual_observed_gap_without_setting_distance(self):
        response = self.ask("前车离我多远")
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["action_detail"]["front_vehicle"],
                         self.observation["environment"]["front_vehicle"])
        self.assertIn("12.4", response["reply_text"])
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 2)
        self.assertEqual(self.gateway.calls, [])

    def test_absent_front_vehicle_is_not_reported_as_zero_distance(self):
        self.observation["environment"]["front_vehicle"] = None
        response = self.ask("前车离我多远")
        self.assertNotEqual(response["action_result"], "success")
        self.assertNotIn("12.4", response["reply_text"])
        self.assertNotIn("0 米", response["reply_text"])
        self.assertNotIn("front_vehicle", response["action_detail"])
        self.assertEqual(self.gateway.calls, [])

    def test_stop_explanation_uses_red_light_and_same_frame_evidence(self):
        response = self.ask("为什么停了")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("红灯", response["reply_text"])
        self.assertEqual(response["action_detail"]["motion"]["state"],
                         "waiting_red_light")
        evidence = " ".join(response["action_detail"]["decision"]["evidence"])
        self.assertIn("123", evidence)
        self.assertIn("Red", evidence)
        self.assertEqual(self.gateway.calls, [])

    def test_stationary_without_cause_does_not_claim_confirmed_obstruction(self):
        self.observation["environment"].update(
            traffic_light={"affecting": False, "state": None},
            front_vehicle=None,
            motion={"state": "stationary", "stationary_s": 5},
        )
        response = self.ask("为什么停了")
        self.assertTrue(any(word in response["reply_text"]
                            for word in ("未知", "不足", "无法确定")))
        self.assertNotIn("确认卡住", response["reply_text"])
        self.assertNotIn("已卡住", response["reply_text"])
        self.assertNotIn("红灯影响", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_road_query_reports_actual_map_identifiers(self):
        response = self.ask("当前道路和车道")
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["action_detail"]["road_id"], 12)
        self.assertEqual(response["action_detail"]["lane_id"], -2)
        self.assertIn("12", response["reply_text"])
        self.assertIn("-2", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_missing_road_information_is_not_substituted_with_default_values(self):
        self.observation["vehicle"].update(road_id=None, lane_id=None)
        response = self.ask("当前道路和车道")
        self.assertEqual(response["action_result"], "unavailable")
        self.assertNotIn("road_id", response["action_detail"])
        self.assertEqual(self.gateway.calls, [])

    def test_recent_events_preserve_sensor_and_operator_sources(self):
        self.observation["events"].append({
            "source": "operator", "code": "takeover_required",
            "message": "模拟接管告警", "frame": 123,
        })
        response = self.ask("最近发生了什么")
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["action_detail"]["events"], self.observation["events"])
        self.assertIn("传感器", response["reply_text"])
        self.assertIn("模拟", response["reply_text"])
        self.assertIn("检测到碰撞", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_camera_staleness_does_not_reject_fresh_telemetry_queries(self):
        self.assertIsNone(check_fresh(self.observation))
        response = self.ask("我现在开多快")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("0.0 km/h", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_non_live_telemetry_rejects_reads_and_writes_even_with_current_timestamp(self):
        for status in ("connecting", "stale", "disconnected"):
            for text in ("前车离我多远", "跟车距离调远一点"):
                with self.subTest(status=status, text=text):
                    self.observation["telemetry"]["status"] = status
                    response = self.ask(text)
                    self.assertEqual(response["action_result"], "unavailable")
                    self.assertEqual(self.gateway.calls, [])

    def test_excessive_capture_age_rejects_write_even_if_status_still_says_live(self):
        self.observation["telemetry"]["age_ms"] = 2001
        self.assertIsNotNone(check_fresh(self.observation))
        response = self.ask("跟车距离调远一点")
        self.assertEqual(response["action_result"], "unavailable")
        self.assertEqual(self.gateway.calls, [])

    def test_old_capture_timestamp_is_not_rescued_by_fresh_status_label(self):
        self.observation["timestamp"] -= 10
        response = self.ask("前车离我多远")
        self.assertEqual(response["action_result"], "unavailable")
        self.assertEqual(self.gateway.calls, [])

    def test_negated_follow_instruction_never_writes(self):
        text = "不要把跟车距离调远"
        self.assertEqual(resolve(text).kind, "no_action")
        response = self.ask(text)
        self.assertEqual(response["action_result"], "no_change")
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 2)


class TelemetryCalculationTests(unittest.TestCase):
    def test_no_sample_has_no_age_or_fabricated_live_state(self):
        self.assertEqual(sample_health(None, 100.0), ("connecting", None))
        self.assertEqual(sample_health(None, 100.0, "CARLA unavailable"),
                         ("disconnected", None))

    def test_reading_the_same_capture_ages_it_instead_of_refreshing_it(self):
        captured_at = 100.0
        self.assertEqual(sample_health(captured_at, 100.5), ("live", 500))
        self.assertEqual(sample_health(captured_at, 103.1), ("stale", 3100))
        self.assertEqual(sample_health(captured_at, 103.1, "tick timeout"),
                         ("disconnected", 3100))

    def test_freshness_boundary_matches_the_da_write_gate(self):
        self.assertEqual(sample_health(100.0, 102.0), ("live", 2000))
        self.assertEqual(sample_health(100.0, 102.001), ("stale", 2001))

    def test_a_moving_vehicle_is_not_labeled_stuck_from_old_stationary_duration(self):
        state = motion_state(4.0, 60, "active",
                             {"affecting": False, "state": None}, None, 0.8)
        self.assertEqual(state, "moving")

    def test_paused_tm_is_an_explicit_stop_reason(self):
        state = motion_state(0.0, 20, "off",
                             {"affecting": True, "state": "Red"}, None, 0.0)
        self.assertEqual(state, "paused")

    def test_only_an_affecting_red_light_explains_a_red_light_stop(self):
        self.assertEqual(motion_state(0.0, 3, "active",
                                      {"affecting": True, "state": "Red"},
                                      None, 0.0), "waiting_red_light")
        for light in ({"affecting": False, "state": "Red"},
                      {"affecting": True, "state": "Green"}):
            with self.subTest(light=light):
                self.assertEqual(motion_state(0.0, 3, "active", light, None, 0.0),
                                 "stationary")

    def test_near_slow_front_vehicle_provides_a_possible_blocking_reason(self):
        no_light = {"affecting": False, "state": None}
        self.assertEqual(motion_state(0.0, 3, "active", no_light,
                                      {"gap_m": 7, "speed_kmh": 0.2}, 0.0),
                         "blocked_by_vehicle")
        # A distant vehicle or a fast vehicle is not evidence of an immediate
        # stopped queue in front of this vehicle.
        for front in ({"gap_m": 35, "speed_kmh": 0.0},
                      {"gap_m": 7, "speed_kmh": 20.0}):
            with self.subTest(front=front):
                self.assertEqual(motion_state(0.0, 3, "active", no_light, front, 0.0),
                                 "stationary")

    def test_sustained_zero_speed_with_throttle_is_only_a_suspicion(self):
        no_light = {"affecting": False, "state": None}
        self.assertEqual(motion_state(0.0, 10, "active", no_light, None, 0.6),
                         "possibly_stuck")
        self.assertEqual(motion_state(0.0, 2, "active", no_light, None, 0.6),
                         "stationary")

    def test_stationary_time_alone_does_not_diagnose_an_obstruction(self):
        state = motion_state(0.0, 60, "active",
                             {"affecting": False, "state": None}, None, 0.0)
        self.assertEqual(state, "stationary")


if __name__ == "__main__":
    unittest.main()
