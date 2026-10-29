"""Tests for layered driving commands: speed, lane change, pause/resume, direct control."""

import unittest
from datetime import datetime, timezone

from da_mvp import DrivingAssistant, InMemoryGateway
from da_mvp.intent import resolve
from tests.test_expanded_capabilities import carla_snapshot, request


class DrivingIntentParsingTests(unittest.TestCase):
    def test_speed_phrases(self):
        self.assertEqual(resolve("开到60").option, "target:60")
        self.assertEqual(resolve("把车速调到80").option, "target:80")
        self.assertEqual(resolve("恢复限速").option, "limit")
        self.assertEqual(resolve("开慢点").option, "slow")
        self.assertEqual(resolve("慢一点").option, "slow")
        self.assertEqual(resolve("加速").option, "fast")
        self.assertEqual(resolve("快点开").option, "fast")

    def test_lane_and_state_phrases(self):
        self.assertEqual(resolve("向左变道").option, "left")
        self.assertEqual(resolve("向右换道").option, "right")
        self.assertEqual(resolve("帮我变道").option, None)
        self.assertEqual(resolve("暂停自动驾驶").option, "pause")
        self.assertEqual(resolve("恢复自动驾驶").option, "resume")
        self.assertEqual(resolve("停车").option, "pause")
        self.assertEqual(resolve("开车").option, "resume")
        self.assertEqual(resolve("开车吧").option, "resume")

    def test_direct_control_phrases(self):
        self.assertEqual(resolve("踩油门").option, "throttle_up")
        self.assertEqual(resolve("刹车").option, "brake")
        self.assertEqual(resolve("左打方向").option, "steer_left")
        self.assertEqual(resolve("回正方向").option, "steer_center")

    def test_high_risk_actions_stay_forbidden(self):
        self.assertEqual(resolve("帮我超车").kind, "forbidden")
        self.assertEqual(resolve("闯红灯").kind, "forbidden")

    def test_explicit_route_selection_phrases(self):
        self.assertEqual(resolve("走备选路线").option, "route_b")
        self.assertEqual(resolve("切换到备选路线").option, "route_b")
        self.assertEqual(resolve("回到原始路线").option, "route_a")
        self.assertEqual(resolve("换一条更快的路").kind, "faster_route")

    def test_destination_and_emergency_phrases(self):
        self.assertEqual(resolve("去东区").option, "东")
        self.assertEqual(resolve("改去西南").option, "西南")
        self.assertEqual(resolve("换个目的地").option, "any")
        self.assertEqual(resolve("紧急停车").kind, "emergency_stop")
        self.assertEqual(resolve("立刻停车").kind, "emergency_stop")
        self.assertEqual(resolve("到目的地还有多久").kind, "eta_query")

    def test_queries_are_not_mistaken_for_speed_commands(self):
        self.assertEqual(resolve("我现在开多快").kind, "status")
        self.assertEqual(resolve("现在车速多少").kind, "status")
        self.assertEqual(resolve("把跟车距离设为四级").kind, "follow_distance")


class SpeedyGateway(InMemoryGateway):
    """CARLA-flavoured double: accepts the driving writes the tests exercise."""

    def snapshot(self):
        data = super().snapshot()
        data.update(source="carla")
        return data

    def set_speed(self, target_kmh, direction=None):
        self.calls.append(("set_speed", target_kmh, direction))
        if target_kmh is not None and target_kmh > 60:
            return {"accepted": False, "reason": "目标车速超过当前限速 60 km/h，不发送超速请求"}
        return {"accepted": True, "target_kmh": target_kmh or 50,
                "previous_kmh": None, "speed_limit_kmh": 60, "difference_pct": -10.0}

    def change_lane(self, direction):
        self.calls.append(("change_lane", direction))
        if direction == "left":
            return {"accepted": False, "reason": "左 侧没有可行驶的同向车道"}
        return {"accepted": True, "direction": direction, "from_lane": -1, "to_lane": -2,
                "note": "路径已提交"}

    def direct_control(self, action):
        self.calls.append(("direct_control", action))
        return {"accepted": False, "reason": "TM 驾驶中不接受直接车辆控制；请先说“暂停自动驾驶”"}


class DrivingCommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.gateway = SpeedyGateway()
        self.assistant = DrivingAssistant(self.gateway)

    def handle(self, text, snapshot=None):
        return self.assistant.handle(request(text), snapshot=snapshot or carla_snapshot())

    def test_speed_target_within_limit_is_applied_via_gateway(self):
        response = self.handle("开到40")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("40 km/h", response["reply_text"])
        self.assertIn("TM 偏移", response["reply_text"])
        self.assertIn(("set_speed", 40, None), self.gateway.calls)

    def test_speed_target_over_limit_is_rejected_before_gateway(self):
        response = self.handle("开到150")
        self.assertEqual(response["action_result"], "rejected")
        self.assertIn("超过当前限速", response["reply_text"])
        self.assertFalse(any(call[0] == "set_speed" for call in self.gateway.calls))

    def test_lane_change_with_direction_is_submitted(self):
        response = self.handle("向右变道")
        self.assertEqual(response["action_result"], "success")
        self.assertIn("车道 -1 → -2", response["reply_text"])
        self.assertIn(("change_lane", "right"), self.gateway.calls)

    def test_lane_change_blocked_by_gateway_keeps_current_lane(self):
        response = self.handle("向左变道")
        self.assertEqual(response["action_result"], "unavailable")
        self.assertIn("保持当前车道", response["reply_text"])

    def test_lane_change_without_direction_asks_for_clarification(self):
        response = self.handle("帮我变道")
        self.assertEqual(response["action_result"], "clarification")
        self.assertIn("向左变道", response["reply_text"])

    def test_direct_control_is_denied_while_tm_drives(self):
        response = self.handle("踩油门")
        self.assertEqual(response["action_result"], "rejected")
        self.assertIn("分层权限拒绝", response["reply_text"])
        # The layer check happens before the gateway: no control call is attempted.
        self.assertFalse(any(call[0] == "direct_control" for call in self.gateway.calls))

    def test_pause_and_resume_cycle(self):
        paused_snapshot = carla_snapshot()
        paused = self.handle("暂停自动驾驶", snapshot=paused_snapshot)
        self.assertEqual(paused["action_result"], "success")
        self.assertIn("直接控制", paused["reply_text"])
        self.assertEqual(self.gateway.state["adas"]["noa_status"], "off")
        paused_snapshot["adas"]["noa_status"] = "off"
        resumed = self.handle("恢复自动驾驶", snapshot=paused_snapshot)
        self.assertEqual(resumed["action_result"], "success")
        self.assertEqual(self.gateway.state["adas"]["noa_status"], "active")
        paused_snapshot["adas"]["noa_status"] = "active"
        again = self.handle("恢复自动驾驶", snapshot=paused_snapshot)
        self.assertEqual(again["action_result"], "no_change")

    def test_memory_mode_refuses_driving_commands(self):
        plain = InMemoryGateway()
        assistant = DrivingAssistant(plain)
        for utterance in ("开到40", "向左变道", "踩油门"):
            response = assistant.handle(request(utterance))
            self.assertEqual(response["action_result"], "unavailable", utterance)

    def test_status_reply_includes_speed_target(self):
        snapshot = carla_snapshot()
        snapshot["adas"]["speed_target_kmh"] = 40
        response = self.handle("现在自动驾驶开了吗", snapshot=snapshot)
        self.assertIn("目标车速 40 km/h", response["reply_text"])

    def test_emergency_stop_bypasses_critical_alert_preemption(self):
        self.gateway.push_event("takeover_required", "critical", "请接管")
        snapshot = carla_snapshot()
        snapshot["latest_event"] = {"code": "takeover_required", "severity": "critical", "message": "请接管"}
        self.gateway.emergency_stop = lambda: {"accepted": True, "handbrake": True}
        response = self.handle("紧急停车", snapshot=snapshot)
        self.assertEqual(response["action_result"], "success")
        self.assertIn("满刹车", response["reply_text"])
        self.assertIn("不会被告警抢占", response["reply_text"])

    def test_emergency_stop_works_on_stale_telemetry(self):
        snapshot = carla_snapshot()
        snapshot["telemetry"]["status"] = "stale"
        self.gateway.emergency_stop = lambda: {"accepted": True, "handbrake": True}
        response = self.handle("紧急停车", snapshot=snapshot)
        self.assertEqual(response["action_result"], "success")

    def test_memory_destination_request_is_recorded_not_claimed(self):
        plain = InMemoryGateway()
        assistant = DrivingAssistant(plain)
        response = assistant.handle(request("去东区"))
        self.assertEqual(response["action_result"], "success")
        self.assertIn("仅记录", response["reply_text"])


if __name__ == "__main__":
    unittest.main()
