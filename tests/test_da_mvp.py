import unittest
from datetime import datetime, timezone

from da_mvp import DrivingAssistant, InMemoryGateway


def request(text):
    return {"session_id": "s1", "user_id": "u1", "utterance": text,
            "context": {}, "timestamp": int(datetime.now(timezone.utc).timestamp())}


class DrivingAssistantTests(unittest.TestCase):
    def setUp(self):
        self.gateway = InMemoryGateway()
        self.assistant = DrivingAssistant(self.gateway)

    def test_faster_route_uses_gateway_candidate(self):
        response = self.assistant.handle(request("换一条更快的路"))
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(response["action_detail"]["saved_time_min"], 3)
        self.assertEqual(self.gateway.state["navigation"]["current_route_id"], "route_b")

    def test_follow_distance_adjustment(self):
        response = self.assistant.handle(request("跟车距离调远一点"))
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 3)

    def test_explicit_follow_level_uses_requested_value(self):
        response = self.assistant.handle(request("把跟车距离设为四级"))
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 4)
        self.assertIn("2 级调到 4 级", response["reply_text"])
        self.assertIn("原等级：2", response["action_detail"]["decision"]["evidence"])

    def test_natural_follow_complaint_adjusts_one_level(self):
        response = self.assistant.handle(request("跟车太近了"))
        self.assertEqual(response["action_result"], "success")
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 3)

    def test_status_reply_uses_current_snapshot_and_does_not_write(self):
        self.gateway.state["adas"].update(current_speed_kmh=46.2, speed_limit_kmh=80,
                                           follow_distance_level=4)
        self.gateway.state["navigation"]["current_route_id"] = "route_b"
        response = self.assistant.handle(request("现在自动驾驶开了吗"))
        self.assertEqual(response["action_result"], "success")
        self.assertIn("46.2 km/h", response["reply_text"])
        self.assertIn("80 km/h", response["reply_text"])
        self.assertIn("4 级", response["reply_text"])
        self.assertIn("备选路线", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_eta_reply_marks_demo_estimate_and_does_not_write(self):
        self.gateway.state["navigation"]["eta_source"] = "geometric_demo_estimate"
        response = self.assistant.handle(request("到目的地还有多久"))
        self.assertEqual(response["action_result"], "success")
        self.assertIn("18 分钟", response["reply_text"])
        self.assertIn("几何演示估计", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_traffic_request_does_not_claim_to_avoid_congestion(self):
        self.gateway.state["source"] = "carla"
        response = self.assistant.handle(request("前面堵车，换条不堵车的路"))
        self.assertEqual(response["action_result"], "clarification")
        self.assertIn("没有实时拥堵数据", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_follow_up_uses_explicit_previous_intent(self):
        first = self.assistant.handle(request("跟车距离调远一点"))
        follow_up = request("再远一点")
        follow_up["context"]["last_intent"] = "follow_distance"
        second = self.assistant.handle(follow_up)
        self.assertEqual(first["action_result"], "success")
        self.assertEqual(second["action_result"], "success")
        self.assertEqual(self.gateway.state["adas"]["follow_distance_level"], 4)

    def test_critical_event_preempts_write(self):
        self.gateway.push_event("takeover_required", "critical", "请接管")
        response = self.assistant.handle(request("跟车距离调远一点"))
        self.assertIn("接管", response["reply_text"])
        self.assertIn("请求暂不执行", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_lane_change_without_direction_asks_instead_of_guessing(self):
        response = self.assistant.handle(request("帮我变道"))
        self.assertEqual(response["action_result"], "clarification")
        self.assertIn("向左变道", response["reply_text"])
        self.assertEqual(self.gateway.calls, [])

    def test_ambiguous_input_does_not_write(self):
        response = self.assistant.handle(request("调一下那个"))
        self.assertEqual(response["action_result"], "clarification")
        self.assertTrue(response["need_follow_up"])
        self.assertEqual(self.gateway.calls, [])

    def test_noa_off_rejects_adjustment(self):
        self.gateway.state["adas"]["noa_status"] = "off"
        response = self.assistant.handle(request("跟车距离调远一点"))
        self.assertEqual(response["action_result"], "rejected")
        self.assertEqual(self.gateway.calls, [])

    def test_stale_state_rejects_write(self):
        self.gateway.state["timestamp"] -= 60
        response = self.assistant.handle(request("换一条更快的路"))
        self.assertEqual(response["action_result"], "unavailable")
        self.assertEqual(self.gateway.calls, [])

    def test_gateway_rejection_is_not_success(self):
        self.gateway.change_route = lambda route_id: {"accepted": False}
        response = self.assistant.handle(request("换一条更快的路"))
        self.assertEqual(response["action_result"], "unavailable")

    def test_incomplete_gateway_ack_is_not_success(self):
        self.gateway.change_route = lambda route_id: {"accepted": True, "route_id": route_id}
        response = self.assistant.handle(request("换一条更快的路"))
        self.assertEqual(response["action_result"], "unavailable")

    def test_no_better_route_keeps_current(self):
        self.gateway.state["navigation"]["routes"][1]["eta_min"] = 20
        response = self.assistant.handle(request("换一条更快的路"))
        self.assertEqual(response["action_result"], "no_change")
        self.assertEqual(self.gateway.calls, [])

    def test_forbidden_control(self):
        response = self.assistant.handle(request("帮我超车"))
        self.assertEqual(response["action_result"], "rejected")
        self.assertEqual(self.gateway.calls, [])

    def test_follow_distance_limit(self):
        self.gateway.state["adas"]["follow_distance_level"] = 5
        response = self.assistant.handle(request("跟车距离调远一点"))
        self.assertEqual(response["action_result"], "rejected")
        self.assertEqual(self.gateway.calls, [])


if __name__ == "__main__":
    unittest.main()
