"""Recorded-sample HTTP contracts using a deterministic CARLA test double."""

from copy import deepcopy
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

from da_mvp.gateway import InMemoryGateway
from da_mvp.telemetry import SampleBuffer
from da_mvp.web_demo import DemoState, make_handler


def captured_sample(frame, speed, x):
    return {
        "source": "carla", "timestamp": 1793170000 + frame,
        "map": "Carla/Maps/Town10HD_Opt",
        "telemetry": {"status": "live", "frame": frame,
                      "simulation_time_s": frame / 5, "age_ms": 0, "error": None},
        "camera": {"status": "live", "frame": frame,
                   "simulation_time_s": frame / 5, "age_ms": 0},
        "camera_available": True, "camera_mode": "front",
        "adas": {"noa_status": "active", "current_speed_kmh": speed,
                 "speed_limit_kmh": 30.0, "follow_distance_level": 2,
                 "follow_distance_m": 5.0},
        "vehicle": {"id": 42, "location": {"x": x, "y": 2.0, "z": 0.1},
                    "heading_deg": 90.0, "acceleration_mps2": 0.2,
                    "road_id": 12, "lane_id": -2, "is_junction": False,
                    "control": {"throttle": 0.2, "brake": 0.0, "steer": 0.0}},
        "environment": {
            "traffic_light": {"affecting": False, "state": None},
            "front_vehicle": {"id": 43, "gap_m": 12.4, "speed_kmh": speed},
            "nearby_vehicle_count": 1,
            "motion": {"state": "moving", "stationary_s": 0},
        },
        "navigation": {"current_route_id": "route_a",
                       "eta_source": "geometric_demo_estimate",
                       "routes": [{"id": "route_a", "eta_min": 0.5},
                                  {"id": "route_b", "eta_min": 0.4}]},
        "events": [{"id": 1, "source": "carla_sensor", "code": "lane_invasion",
                    "severity": "warning", "message": "检测到压线",
                    "frame": frame, "simulation_time_s": frame / 5,
                    "timestamp": 1793170000 + frame}],
        "latest_event": None,
    }


class CarlaGateway(InMemoryGateway):
    """Exposes two captured frames; it neither imports nor controls CARLA."""

    def __init__(self):
        super().__init__()
        self.samples = [captured_sample(100, 12.5, 1.0),
                        captured_sample(101, 13.2, 1.8)]
        self.requested_limits = []

    def snapshot(self):
        if self.samples:
            return deepcopy(self.samples[-1])
        data = captured_sample(0, None, None)
        data["telemetry"].update(status="connecting", frame=None,
                                 simulation_time_s=None, age_ms=None)
        data["camera"].update(status="waiting", frame=None,
                              simulation_time_s=None, age_ms=None)
        return data

    def telemetry_stream(self, limit=30):
        self.requested_limits.append(limit)
        return {
            "source": "carla", "samples_total": len(self.samples),
            "buffer_size": len(self.samples), "buffer_capacity": 300,
            "dropped_samples": 0,
            "sample_rate_hz": 5.0 if self.samples else None,
            "recent_samples": deepcopy(self.samples[-limit:]),
            "snapshot": self.snapshot(),
        }

    def export_samples(self):
        return deepcopy(self.samples)


class TelemetryStreamTests(unittest.TestCase):
    def setUp(self):
        self.state = DemoState(CarlaGateway)
        self.gateway = self.state.gateway
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, path):
        return self.opener.open(self.base + path, timeout=3)

    def assert_unavailable(self, path):
        with self.assertRaises(HTTPError) as caught:
            self.get(path)
        error = caught.exception
        self.assertEqual(error.code, 503)
        self.assertIn("error", json.loads(error.read()))
        self.assertIsNone(error.headers.get("Content-Disposition"))

    def test_stream_returns_capture_history_and_current_snapshot_without_writing(self):
        before = deepcopy(self.gateway.samples)
        with self.get("/api/telemetry-stream") as response:
            self.assertEqual(response.status, 200)
            payload = json.load(response)
        self.assertEqual(payload["source"], "carla")
        self.assertEqual(payload["samples_total"], 2)
        self.assertEqual(payload["buffer_size"], 2)
        self.assertEqual(payload["buffer_capacity"], 300)
        self.assertEqual(payload["dropped_samples"], 0)
        self.assertEqual(payload["sample_rate_hz"], 5.0)
        self.assertEqual(payload["recent_samples"], before)
        self.assertEqual(payload["snapshot"], before[-1])
        self.assertEqual(self.gateway.requested_limits, [30])
        self.assertEqual(self.gateway.samples, before)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.state.turn, 0)
        self.assertEqual(self.state.history, [])

    def test_export_is_a_jsonl_attachment_with_original_frames_and_values(self):
        before = deepcopy(self.gateway.samples)
        with self.get("/api/telemetry-export.jsonl") as response:
            self.assertEqual(response.status, 200)
            disposition = response.headers.get("Content-Disposition", "")
            self.assertIn("attachment", disposition)
            self.assertIn(".jsonl", disposition)
            self.assertIn("json", response.headers.get("Content-Type", ""))
            body = response.read().decode("utf-8")
        lines = body.splitlines()
        self.assertEqual(len(lines), 2)
        exported = [json.loads(line) for line in lines]
        self.assertEqual(exported, before)
        self.assertEqual([item["telemetry"]["frame"] for item in exported], [100, 101])
        self.assertEqual(exported[1]["adas"]["current_speed_kmh"], 13.2)
        self.assertEqual(exported[1]["vehicle"]["location"]["x"], 1.8)
        self.assertEqual(exported[0]["events"][0]["message"], "检测到压线")
        self.assertEqual(self.gateway.samples, before)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(self.state.turn, 0)
        self.assertEqual(self.state.history, [])

    def test_empty_stream_exposes_connecting_state_but_export_is_unavailable(self):
        self.gateway.samples.clear()
        with self.get("/api/telemetry-stream") as response:
            payload = json.load(response)
        self.assertEqual(payload["samples_total"], 0)
        self.assertEqual(payload["buffer_size"], 0)
        self.assertEqual(payload["recent_samples"], [])
        self.assertIsNone(payload["sample_rate_hz"])
        self.assertEqual(payload["snapshot"]["telemetry"]["status"], "connecting")
        self.assert_unavailable("/api/telemetry-export.jsonl")

    def test_memory_source_cannot_be_presented_as_a_carla_stream_or_export(self):
        self.state.gateway = InMemoryGateway()
        for path in ("/api/telemetry-stream", "/api/telemetry-export.jsonl"):
            with self.subTest(path=path):
                self.assert_unavailable(path)
        self.assertEqual(self.state.gateway.calls, [])

    def test_export_rejects_non_carla_rows_even_if_gateway_has_an_export_method(self):
        self.gateway.samples[0]["source"] = "memory"
        self.assert_unavailable("/api/telemetry-export.jsonl")
        self.assertEqual(self.gateway.calls, [])


class SampleBufferTests(unittest.TestCase):
    def test_default_capacity_matches_the_stream_contract(self):
        self.assertEqual(SampleBuffer().view()["buffer_capacity"], 300)

    def test_capacity_evicts_oldest_frames_and_tracks_total_and_dropped_samples(self):
        buffer = SampleBuffer(capacity=3)
        for index, frame in enumerate(range(100, 105)):
            self.assertTrue(buffer.append(captured_sample(frame, 12.5, index),
                                          received_monotonic=10 + index * 0.5))
        stats = buffer.view()
        self.assertEqual(stats["samples_total"], 5)
        self.assertEqual(stats["buffer_size"], 3)
        self.assertEqual(stats["buffer_capacity"], 3)
        self.assertEqual(stats["dropped_samples"], 2)
        self.assertEqual([item["telemetry"]["frame"] for item in buffer.export()],
                         [102, 103, 104])

    def test_duplicate_and_out_of_order_frames_do_not_inflate_sampling_counts(self):
        buffer = SampleBuffer(capacity=3)
        self.assertTrue(buffer.append(captured_sample(100, 12.5, 1), 10.0))
        self.assertFalse(buffer.append(captured_sample(100, 99, 99), 10.1))
        self.assertFalse(buffer.append(captured_sample(99, 99, 99), 10.2))
        self.assertTrue(buffer.append(captured_sample(101, 13.2, 2), 10.5))
        stats = buffer.view()
        self.assertEqual(stats["samples_total"], 2)
        self.assertEqual(stats["dropped_samples"], 0)
        self.assertEqual(stats["sample_rate_hz"], 2.0)
        self.assertEqual([item["adas"]["current_speed_kmh"] for item in buffer.export()],
                         [12.5, 13.2])

    def test_rate_uses_real_receive_time_and_is_unchanged_by_reads(self):
        buffer = SampleBuffer(capacity=3)
        for index, frame in enumerate((100, 105, 110)):
            sample = captured_sample(frame, 12.5, 1)
            sample["telemetry"]["simulation_time_s"] = index * 1000
            buffer.append(sample, received_monotonic=50 + index * 0.5)
        stats = buffer.view()
        self.assertEqual(stats["sample_rate_hz"], 2.0)
        for _ in range(3):
            self.assertEqual(buffer.view(), stats)
        self.assertEqual(buffer.view(limit=1)["recent_samples"], buffer.export()[-1:])
        self.assertEqual(len(buffer.export()), 3)

    def test_buffer_owns_immutable_copies_of_nested_capture_data(self):
        buffer = SampleBuffer()
        sample = captured_sample(100, 12.5, 1)
        expected = deepcopy(sample)
        buffer.append(sample, 10.0)
        sample["vehicle"]["location"]["x"] = 999
        sample["events"][0]["message"] = "mutated caller object"
        self.assertEqual(buffer.export(), [expected])
        exported = buffer.export()
        exported[0]["adas"]["current_speed_kmh"] = 999
        viewed = buffer.view()
        viewed["recent_samples"][0]["telemetry"]["frame"] = 999
        self.assertEqual(buffer.export(), [expected])
        self.assertEqual(buffer.view()["recent_samples"], [expected])

    def test_clear_resets_scene_counters_and_accepts_a_new_lower_frame(self):
        buffer = SampleBuffer(capacity=3)
        buffer.append(captured_sample(100, 12.5, 1), 10.0)
        buffer.append(captured_sample(101, 13.2, 2), 10.5)
        buffer.clear()
        stats = buffer.view()
        self.assertEqual(stats["samples_total"], 0)
        self.assertEqual(stats["buffer_size"], 0)
        self.assertEqual(stats["dropped_samples"], 0)
        self.assertEqual(stats["recent_samples"], [])
        self.assertIsNone(stats["sample_rate_hz"])
        self.assertEqual(buffer.export(), [])
        self.assertTrue(buffer.append(captured_sample(1, 5.0, 3), 1.0))
        self.assertEqual(buffer.view()["samples_total"], 1)
        self.assertEqual(buffer.export()[0]["telemetry"]["frame"], 1)

    def test_invalid_receive_time_does_not_partially_append_a_capture(self):
        buffer = SampleBuffer(capacity=3)
        buffer.append(captured_sample(100, 12.5, 1), 10.0)
        before = buffer.view()
        for invalid_time in (9.0, float("inf"), float("nan")):
            with self.subTest(received_monotonic=invalid_time):
                with self.assertRaises(ValueError):
                    buffer.append(captured_sample(101, 13.2, 2), invalid_time)
                self.assertEqual(buffer.view(), before)


if __name__ == "__main__":
    unittest.main()
