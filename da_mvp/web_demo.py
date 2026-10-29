"""Interactive, local-only DA simulation. Run: python -m da_mvp.web_demo"""

import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .gateway import InMemoryGateway
from .intent import resolve
from .service import DrivingAssistant


STATIC = Path(__file__).with_name("web")


class DemoState:
    def __init__(self, gateway_factory=InMemoryGateway):
        self.lock = threading.RLock()
        self.gateway_factory = gateway_factory
        self.gateway = None
        self.reset()

    def reset(self):
        if self.gateway is not None and callable(getattr(self.gateway, "reset_demo", None)):
            # Reuse the CARLA client/TM and sensors. Native client teardown and
            # reconstruction inside a request can invalidate callback threads.
            self.gateway.reset_demo()
            self.history = []
            self.turn = 0
            return
        if self.gateway is not None:
            self.gateway.close()
        self.gateway = None
        self.assistant = None
        self.gateway = self.gateway_factory()
        self.assistant = DrivingAssistant(self.gateway)
        self.history = []
        self.turn = 0

    def view(self):
        if self.gateway is None:
            raise RuntimeError("场景未就绪，请检查 CARLA 后重新生成场景。")
        return {"snapshot": self.gateway.snapshot(), "history": self.history[-30:],
                "gateway_calls": self.gateway.calls[-30:], "turn": self.turn,
                "mode": "carla" if self.gateway.__class__.__name__ == "CarlaGateway" else "memory"}

    def command(self, utterance):
        if not isinstance(utterance, str) or not utterance.strip():
            raise ValueError("请输入驾驶相关话术")
        if len(utterance) > 300:
            raise ValueError("话术不能超过 300 个字符")
        self.turn += 1
        self.gateway.refresh()
        before = self.gateway.snapshot()
        call_index = len(self.gateway.calls)
        request = {"session_id": "web_demo", "user_id": "demo_user",
                   "utterance": utterance.strip(),
                   "context": {"turn_count": self.turn,
                               "last_intent": self.history[-1]["intent"] if self.history else None},
                   "timestamp": int(time.time())}
        response = self.assistant.handle(request, snapshot=before)
        calls = self.gateway.calls[call_index:]
        intent = resolve(utterance).kind
        if intent == "clarify" and response.get("action_detail", {}).get("type") == "preference_changed":
            intent = "follow_distance"
        after = self.gateway.snapshot()
        changes = []
        before_level = before.get("adas", {}).get("follow_distance_level")
        after_level = after.get("adas", {}).get("follow_distance_level")
        if before_level != after_level:
            changes.append(f"跟车设置：{before_level} → {after_level} 级；设置后的物理变化待观察")
        before_route = before.get("navigation", {}).get("current_route_id")
        after_route = after.get("navigation", {}).get("current_route_id")
        if before_route != after_route:
            changes.append(f"页面路线记录：{before_route} → {after_route}；行驶轨迹未验证")
        before_target = before.get("adas", {}).get("speed_target_kmh")
        after_target = after.get("adas", {}).get("speed_target_kmh")
        if before_target != after_target and after_target is not None:
            changes.append(f"目标车速：{before_target if before_target is not None else '按限速'} → {after_target} km/h；实际车速待观察")
        before_noa = before.get("adas", {}).get("noa_status")
        after_noa = after.get("adas", {}).get("noa_status")
        if before_noa != after_noa:
            changes.append(f"TM 自动驾驶：{before_noa} → {after_noa}")
        entry = {"turn": self.turn, "utterance": utterance.strip(),
                 "intent": intent, "response": response,
                 "agent_input": before,
                 "gateway_calls": calls, "before": before, "after": after,
                 "observed_change": changes or ["跟车设置和路线记录未改变"],
                 "time": datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")}
        self.history.append(entry)
        return entry


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if args and (str(args[0]).startswith("GET /api/state ")
                         or str(args[0]).startswith("GET /api/frame")
                         or str(args[0]).startswith("GET /api/telemetry-stream")):
                return
            print("[web] " + fmt % args)

        def send(self, code, data, content_type="application/json; charset=utf-8", filename=None):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            try:
                if route == "/api/state":
                    with state.lock:
                        return self.send(200, state.view())
                if route == "/api/agent-input":
                    with state.lock:
                        if state.gateway is None:
                            raise RuntimeError("CARLA 场景尚未就绪。")
                        return self.send(200, {"schema_version": "1.1", "snapshot": state.gateway.snapshot()})
                if route in ("/api/telemetry-stream", "/api/telemetry-export.jsonl"):
                    with state.lock:
                        gateway = state.gateway
                        if gateway is None or gateway.snapshot().get("source") != "carla":
                            raise RuntimeError("当前没有 CARLA 实时采样；请使用 CARLA 模式。")
                        if route == "/api/telemetry-stream":
                            reader = getattr(gateway, "telemetry_stream", None)
                            if not callable(reader):
                                raise RuntimeError("当前网关未提供持续采样记录。")
                            return self.send(200, reader(limit=30))
                        exporter = getattr(gateway, "export_samples", None)
                        if not callable(exporter):
                            raise RuntimeError("当前网关未提供 CARLA 采样导出。")
                        samples = exporter()
                    if not isinstance(samples, list) or not samples:
                        raise RuntimeError("暂无 CARLA 采样可导出，请等待真实世界帧到达。")
                    if not all(isinstance(s, dict) and s.get("source") == "carla"
                               and isinstance(s.get("telemetry"), dict)
                               and isinstance(s.get("telemetry", {}).get("frame"), int)
                               and not isinstance(s["telemetry"]["frame"], bool) for s in samples):
                        raise RuntimeError("缓存含有无效或非 CARLA 采样，已停止导出。")
                    body = ("\n".join(json.dumps(sample, ensure_ascii=False) for sample in samples) + "\n").encode("utf-8")
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    return self.send(200, body, "application/x-ndjson; charset=utf-8",
                                     filename=f"carla-inputs-{stamp}.jsonl")
                if route == "/api/frame":
                    with state.lock:
                        if state.gateway is None:
                            raise RuntimeError("CARLA 场景尚未就绪。")
                        frame = state.gateway.frame()
                    return self.send(200, frame, "image/jpeg") if frame else self.send(503, {"error": "摄像头尚未就绪或画面已经过期"})
            except RuntimeError as exc:
                return self.send(503, {"error": str(exc)})
            if route not in files:
                return self.send(404, {"error": "not found"})
            filename, mime = files[route]
            return self.send(200, (STATIC / filename).read_bytes(), mime)

        def do_POST(self):
            routes = {"/api/command", "/api/reset", "/api/event", "/api/clear-event", "/api/noa",
                      "/api/camera-mode", "/api/weather", "/api/recorder", "/api/scenario",
                      "/api/sync-mode"}
            if self.path not in routes:
                return self.send(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > 4096 or size < 0:
                    return self.send(413, {"error": "请求过大"})
                data = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("请求必须是 JSON 对象")
                with state.lock:
                    if self.path == "/api/command":
                        entry = state.command(data.get("utterance"))
                        return self.send(200, {"entry": entry, **state.view()})
                    if self.path == "/api/reset":
                        state.reset()
                    elif self.path == "/api/event":
                        state.gateway.push_event("takeover_required", "critical", "请立即接管驾驶")
                        entry = state.command("请解释接管告警")
                        return self.send(200, {"entry": entry, **state.view()})
                    elif self.path == "/api/clear-event":
                        state.gateway.clear_event()
                    elif self.path == "/api/noa":
                        status = data.get("status")
                        if status not in ("active", "off"):
                            raise ValueError("NOA 状态只支持 active 或 off")
                        state.gateway.set_noa_status(status)
                    elif self.path == "/api/camera-mode":
                        state.gateway.set_camera_mode(data.get("mode"))
                    elif self.path == "/api/weather":
                        ack = state.gateway.set_weather(data.get("preset") or "")
                        if not isinstance(ack, dict) or not ack.get("accepted"):
                            raise RuntimeError(ack.get("reason", "天气设置未确认") if isinstance(ack, dict) else "天气设置未确认")
                    elif self.path == "/api/recorder":
                        action = data.get("action")
                        if action not in ("start", "stop"):
                            raise ValueError("录制器只支持 start 或 stop")
                        ack = state.gateway.control_recorder(action)
                        if not isinstance(ack, dict) or not ack.get("accepted"):
                            raise RuntimeError(ack.get("reason", "录制操作未确认") if isinstance(ack, dict) else "录制操作未确认")
                    elif self.path == "/api/scenario":
                        ack = state.gateway.inject_scenario(data.get("kind") or "")
                        if not isinstance(ack, dict) or not ack.get("accepted"):
                            raise RuntimeError(ack.get("reason", "场景操作未确认") if isinstance(ack, dict) else "场景操作未确认")
                    elif self.path == "/api/sync-mode":
                        enabled = data.get("enabled")
                        if not isinstance(enabled, bool):
                            raise ValueError("同步模式开关必须是布尔值")
                        setter = getattr(state.gateway, "set_sync_mode", None)
                        if not callable(setter):
                            raise RuntimeError("内存模式没有 CARLA 仿真模式切换")
                        ack = setter(enabled)
                        if not isinstance(ack, dict) or not ack.get("accepted"):
                            raise RuntimeError(ack.get("reason", "仿真模式切换未确认") if isinstance(ack, dict) else "仿真模式切换未确认")
                    return self.send(200, state.view())
            except (ValueError, json.JSONDecodeError) as exc:
                return self.send(400, {"error": str(exc)})
            except RuntimeError as exc:
                return self.send(503, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="DA 本地交互演示")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--mode", choices=("memory", "carla"), default="carla")
    parser.add_argument("--carla-host", default="127.0.0.1")
    parser.add_argument("--carla-port", type=int, default=2000)
    parser.add_argument("--town", default=None, help="可选 CARLA 地图；不指定则使用服务器当前地图")
    parser.add_argument("--osm-map", default=None,
                        help="可选本地 .osm 文件路径；提供时用 OpenStreetMap 数据生成自定义路网")
    args = parser.parse_args()
    if args.mode == "carla":
        from .carla_gateway import CarlaGateway
        factory = lambda: CarlaGateway(args.carla_host, args.carla_port, town=args.town,
                                       osm_map=args.osm_map)
    else:
        factory = InMemoryGateway
    try:
        state = DemoState(factory)
    except RuntimeError as exc:
        parser.exit(2, f"CARLA 演示启动失败：{exc}\n")

    class DemoServer(ThreadingHTTPServer):
        # http.server defaults to SO_REUSEADDR, which on Windows lets a second
        # instance silently bind the same port and split incoming requests.
        allow_reuse_address = False

    try:
        server = DemoServer(("127.0.0.1", args.port), make_handler(state))
    except OSError as exc:
        parser.exit(2, f"端口 {args.port} 已被占用（可能已有演示在运行）：{exc}\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"DA 交互演示已启动：{url}", flush=True)
    print("仅限本机仿真。按 Ctrl+C 停止。", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        state.gateway.close()


if __name__ == "__main__":
    main()
