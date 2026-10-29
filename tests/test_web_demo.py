import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from da_mvp.gateway import InMemoryGateway
from da_mvp.web_demo import DemoState, make_handler


class CarlaGateway(InMemoryGateway):
    """Test double for the web transport; it is not a CARLA runtime test."""

    camera_mode = "front"

    def snapshot(self):
        data = super().snapshot()
        data.update(source="carla", map="Town03", camera_available=True,
                    camera_mode=self.camera_mode)
        data["adas"]["follow_distance_m"] = 5.0
        data["navigation"]["eta_source"] = "geometric_demo_estimate"
        return data

    def frame(self):
        return b"\xff\xd8test\xff\xd9"

    def set_camera_mode(self, mode):
        self.camera_mode = mode

    def set_preference(self, name, value):
        ack = super().set_preference(name, value)
        if ack["accepted"]:
            ack["distance_m"] = {1: 3.0, 2: 5.0, 3: 7.0, 4: 9.0, 5: 12.0}[value]
        return ack


class WebDemoTests(unittest.TestCase):
    def setUp(self):
        self.state = DemoState(CarlaGateway)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, path, value):
        request = Request(self.base + path, json.dumps(value).encode(),
                          {"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            return json.load(response)

    def test_camera_and_telemetry_transport(self):
        with urlopen(self.base + "/api/state") as response:
            state = json.load(response)
        self.assertEqual(state["mode"], "carla")
        self.assertEqual(state["snapshot"]["map"], "Town03")
        with urlopen(self.base + "/api/agent-input") as response:
            agent_input = json.load(response)
        self.assertEqual(agent_input["schema_version"], "1.1")
        self.assertEqual(agent_input["snapshot"]["source"], "carla")
        with urlopen(self.base + "/api/frame?t=1") as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertTrue(response.read().startswith(b"\xff\xd8"))
        changed = self.post("/api/camera-mode", {"mode": "chase"})
        self.assertEqual(changed["snapshot"]["camera_mode"], "chase")

    def test_chat_changes_follow_distance_and_event_preempts(self):
        change = self.post("/api/command", {"utterance": "跟车距离调远一点"})
        self.assertEqual(change["snapshot"]["adas"]["follow_distance_level"], 3)
        self.assertIn("CARLA", change["entry"]["response"]["reply_text"])
        observed = change["entry"]["response"]["action_detail"]["observation"]
        self.assertEqual(observed["timestamp"], change["entry"]["agent_input"]["timestamp"])
        self.assertEqual(change["entry"]["agent_input"]["adas"]["follow_distance_level"], 2)
        self.assertEqual(change["snapshot"]["adas"]["follow_distance_level"], 3)
        self.assertIn("跟车设置：2 → 3 级", change["entry"]["observed_change"][0])
        self.assertEqual(change["entry"]["response"]["action_detail"]["decision"]["gateway"],
                         "设置值已确认")
        alert = self.post("/api/event", {})
        self.assertEqual(alert["entry"]["response"]["action_result"], "no_change")
        blocked = self.post("/api/command", {"utterance": "换一条更快的路"})
        self.assertEqual(blocked["snapshot"]["navigation"]["current_route_id"], "route_a")
        self.assertFalse(blocked["entry"]["gateway_calls"])


class ResetLifecycleTests(unittest.TestCase):
    def test_live_reset_reuses_gateway_and_preserves_failed_reset_state(self):
        class ReusableGateway(InMemoryGateway):
            fail_reset = False

            def close(self):
                raise AssertionError("A reusable gateway must not be destroyed by reset")

            def reset_demo(self):
                if self.fail_reset:
                    raise RuntimeError("simulator unavailable")
                self.state["adas"]["follow_distance_level"] = 2

        state = DemoState(ReusableGateway)
        gateway = state.gateway
        state.command("跟车距离调远一点")
        self.assertEqual(gateway.state["adas"]["follow_distance_level"], 3)
        gateway.fail_reset = True
        with self.assertRaises(RuntimeError):
            state.reset()
        self.assertIs(state.gateway, gateway)
        self.assertEqual(state.turn, 1)
        self.assertEqual(len(state.history), 1)
        gateway.fail_reset = False
        state.reset()
        self.assertIs(state.gateway, gateway)
        self.assertEqual(state.turn, 0)
        self.assertEqual(state.history, [])
        self.assertEqual(gateway.state["adas"]["follow_distance_level"], 2)


if __name__ == "__main__":
    unittest.main()
