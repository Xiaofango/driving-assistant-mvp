"""PC-facing DA service. No direct vehicle actuation lives here."""

from datetime import datetime, timezone
from .gateway import Gateway
from .intent import Intent, resolve
from .models import Request, Result
from .safety import check_fresh, check_preference, faster_route
from .telemetry import SCENARIO_LABELS, WEATHER_LABELS


class DrivingAssistant:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway

    def handle(self, payload: dict, *, snapshot: dict | None = None) -> dict:
        req = Request.from_dict(payload)
        now = int(datetime.now(timezone.utc).timestamp())
        state = {}

        def result(reply: str, status: str, detail: dict | None = None,
                   follow_up: bool = False, reason: str = "",
                   evidence: list[str] | None = None, gateway: str = "无写操作") -> dict:
            action_detail = dict(detail or {})
            action_detail["decision"] = {"reason": reason, "evidence": evidence or [],
                                         "gateway": gateway}
            telemetry = state.get("telemetry", {})
            action_detail["observation"] = {
                "source": state.get("source", "memory"), "timestamp": state.get("timestamp"),
                "frame": telemetry.get("frame"), "simulation_time_s": telemetry.get("simulation_time_s"),
                "telemetry_status": telemetry.get("status", "memory")}
            return Result(req.session_id, reply, status, action_detail, follow_up, now).to_dict()

        try:
            state = snapshot if snapshot is not None else self.gateway.snapshot()
        except Exception:
            return result("驾驶状态暂时不可用，请稍后再试。", "unavailable",
                          reason="状态读取失败，不能基于未知状态执行驾驶请求。")

        intent = resolve(req.utterance)
        if intent.kind == "no_action":
            return result("已理解为不执行调整，本轮没有发送设置或路径请求。", "no_change",
                          {"type": "no_action"}, reason="话术包含否定或保持状态的要求。")
        if intent.kind not in ("forbidden", "clarify", "emergency_stop"):
            stale = check_fresh(state, now)
            if stale:
                return result(stale, "unavailable", reason="CARLA 实际采集状态不满足新鲜度要求。",
                              evidence=[f"数据状态：{state.get('telemetry', {}).get('status', '未知')}",
                                        f"采集时间：{state.get('timestamp')}"])
        event = state.get("latest_event")
        if isinstance(event, dict) and event.get("severity") == "critical" and intent.kind != "emergency_stop":
            alert = self._event_reply(event, state.get("source") == "carla")
            if intent.kind == "explain_event":
                reply = alert
            elif intent.kind == "forbidden":
                reply = f"{alert} 你的车辆控制请求也不在 DA 允许范围内，未发送给仿真车。"
            else:
                reply = f"{alert} 当前优先处理告警，刚才的请求暂不执行。"
            return result(reply, "no_change",
                          {"type": "critical_event", "code": event.get("code")},
                          reason="紧急告警优先于普通查询和设置。",
                          evidence=[f"事件码：{event.get('code', '未知')}",
                                    f"本轮意图：{intent.kind}", "车辆参数和路线保持不变"])

        if intent.kind == "clarify" and req.context.get("last_intent") == "follow_distance":
            follow_up = req.utterance.strip().replace(" ", "")
            if follow_up in ("再远一点", "再调远一点"):
                intent = Intent("follow_distance", 1)
            elif follow_up in ("再近一点", "再调近一点"):
                intent = Intent("follow_distance", -1)
        if intent.kind == "forbidden":
            destination = "没有向 CARLA 发送" if state.get("source") == "carla" else "没有向网关发送"
            return result(f"这类请求超出驾驶助手的安全范围，{destination}控制指令。"
                          "超车/闯红灯/逆行不提供；速度和变道请说“开到 40”“向左变道”这类意图指令。",
                          "rejected", {"type": "forbidden_control"},
                          reason="请求超出驾驶助手的受限工具范围。",
                          evidence=["识别为：高危车辆动作", "意图级指令（速度/变道）可用，危险动作不可用"])
        if intent.kind == "clarify":
            return result("我还不能确定你想改什么。请明确说“跟车距离调远一点”“跟车距离设为 4 级”或“查询驾驶状态”，"
                          "也可以说“下雨”“现在天气怎么样”“开始录制”“模拟红灯场景”“附近有行人吗”。",
                          "clarification", follow_up=True,
                          reason="当前话术没有足够信息定位受限工具和参数。",
                          evidence=["未选择写工具", "车辆状态保持不变"])
        if intent.kind in ("stop_reason", "front_distance", "road_context", "recent_events"):
            return self._live_query(intent.kind, state, result)
        if intent.kind == "status":
            adas = state.get("adas", {})
            speed = adas.get("current_speed_kmh", "未知")
            sim = state.get("source") == "carla"
            status_name = "CARLA 的 TM 自动驾驶" if sim else "NOA"
            status_text = "已启用" if adas.get("noa_status") == "active" else "未启用"
            distance = adas.get("follow_distance_level", "未知")
            meters = f"（设置值 {adas['follow_distance_m']:g} 米）" if sim and isinstance(adas.get("follow_distance_m"), (int, float)) else ""
            route_id = state.get("navigation", {}).get("current_route_id")
            route_name = {"route_a": "原始路线", "route_b": "备选路线"}.get(route_id, "未知路线")
            limit = adas.get("speed_limit_kmh")
            limit_text = f"{limit} km/h" if isinstance(limit, (int, float)) else "未知"
            target = adas.get("speed_target_kmh")
            target_text = f"，目标车速 {target:g} km/h" if sim and isinstance(target, (int, float)) else ""
            reply = (f"{status_name}{status_text}。当前车速 {speed} km/h，限速 "
                     f"{limit_text}{target_text}；跟车距离 {distance} 级{meters}，"
                     f"页面记录为{route_name}。")
            motion = state.get("environment", {}).get("motion", {}).get("state")
            if sim and isinstance(speed, (int, float)) and speed < 0.5:
                reply += " " + self._motion_text(motion)
            return result(reply, "success", {"type": "driving_status", "adas": adas},
                          reason="这是只读状态查询，没有调整车辆参数。",
                          evidence=[f"车速：{speed} km/h", f"TM/NOA：{status_text}",
                                    f"跟车等级：{distance}", f"页面路线：{route_name}"])
        if intent.kind == "eta_query":
            nav = state.get("navigation", {})
            route = next((r for r in nav.get("routes", [])
                          if r.get("id") == nav.get("current_route_id")), None)
            if not route or not isinstance(route.get("eta_min"), (int, float)):
                return result("当前路线的剩余时间不可用，暂时无法回答还有多久到。",
                              "unavailable", reason="状态中缺少当前路线 ETA。")
            estimate = "几何演示估计" if nav.get("eta_source") == "geometric_demo_estimate" else "网关估计"
            return result(f"当前路线显示约 {route['eta_min']:g} 分钟；这是{estimate}，"
                          "未计算红灯、拥堵或车辆卡住的时间。",
                          "success", {"type": "eta_query", "eta_min": route["eta_min"]},
                          reason="读取当前路线的估计时间，没有发起改道。",
                          evidence=[f"当前路线：{nav.get('current_route_id')}",
                                    f"时间来源：{estimate}"])
        if intent.kind == "traffic_status":
            if state.get("source") == "carla":
                return result("当前 CARLA 演示没有实时路况或拥堵数据；我能读取车速和演示路线估计，"
                              "但不能判断前方是否堵车。",
                              "unavailable", {"type": "traffic_unknown"},
                              reason="没有接入实时交通信息源。",
                              evidence=["可用：车速、TM 状态、几何路线估计",
                                        "缺少：道路拥堵与通行时间"])
            return result("当前内存演示没有真实路况数据，无法判断前方是否拥堵。",
                          "unavailable", {"type": "traffic_unknown"},
                          reason="没有接入实时交通信息源。")
        if intent.kind == "weather_query":
            weather = state.get("environment", {}).get("weather")
            if not isinstance(weather, dict) or not weather.get("preset"):
                return result("当前没有可用的天气观测，无法回答天气问题。", "unavailable",
                              reason="状态快照中没有天气数据。",
                              evidence=["缺少字段：environment.weather"])
            label = weather.get("label") or WEATHER_LABELS.get(weather["preset"], weather["preset"])
            if state.get("source") != "carla" or "precipitation_pct" not in weather:
                return result(f"当前演示记录的天气为「{label}」；内存模式没有真实天气参数。",
                              "success", {"type": "weather_query", "weather": weather},
                              reason="读取状态中的天气标签，没有修改仿真环境。",
                              evidence=[f"天气标签：{label}"])
            parts = [f"雨量 {weather.get('precipitation_pct', '未知')}%",
                     f"雾浓度 {weather.get('fog_density_pct', '未知')}%",
                     f"路面湿度 {weather.get('wetness_pct', '未知')}%",
                     f"太阳高度角 {weather.get('sun_altitude_deg', '未知')}°（负值为夜间）"]
            reply = f"当前 CARLA 天气为「{label}」：" + "，".join(parts) + "。天气变化会同时影响路面附着与摄像头能见度。"
            return result(reply, "success", {"type": "weather_query", "weather": weather},
                          reason="读取 CARLA 天气参数，没有修改仿真环境。",
                          evidence=[f"天气标签：{label}", "来源：CARLA 天气参数"])
        if intent.kind == "set_weather":
            preset = intent.option
            label = WEATHER_LABELS.get(preset, preset or "未知")
            if preset is None:
                return result("我听不出想要哪种天气；可以说“下雨”“起雾”“天黑”或“晴天”。",
                              "clarification", follow_up=True, reason="话术没有映射到具体天气预设。",
                              evidence=["支持预设：" + "、".join(WEATHER_LABELS.values())])
            try:
                ack = self.gateway.set_weather(preset)
            except Exception:
                return result("天气设置请求未获得网关确认，不能说已经切换。", "unavailable",
                              reason="网关调用失败或超时。", evidence=[f"请求预设：{label}"],
                              gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"天气未切换成功（{reason_text}），演示环境保持原天气。",
                              "unavailable", reason="网关回执未确认天气设置。",
                              evidence=[f"请求预设：{label}", f"回执：{reason_text}"], gateway="未确认")
            previous = WEATHER_LABELS.get(ack.get("previous", ""), ack.get("previous") or "未知")
            memory_note = "" if state.get("source") == "carla" else "内存演示仅记录标签，不产生真实天气效果。"
            reply = (f"已确认把仿真天气从「{previous}」切换为「{label}」。"
                     "该设置只作用于仿真环境，不是车辆控制指令；雨雾会降低路面附着与摄像头能见度。" + memory_note)
            return result(reply, "success",
                          {"type": "weather_changed", "preset": preset, "label": label},
                          reason="网关回执确认了新的天气预设。",
                          evidence=[f"原天气：{previous}", f"新天气：{label}"], gateway="设置值已确认")
        if intent.kind == "pedestrian_query":
            if state.get("source") != "carla":
                return result("当前数据源没有行人观测；行人感知只在 CARLA 模式可用。", "unavailable",
                              reason="内存模式没有行人传感器与仿真行人。")
            env = state.get("environment", {})
            pedestrians = env.get("pedestrians")
            sensors = state.get("sensors", {})
            segmentation = sensors.get("segmentation") if isinstance(sensors, dict) else None
            evidence = []
            if not isinstance(pedestrians, dict):
                return result("本帧没有可用的行人观测，无法回答附近是否有行人。", "unavailable",
                              reason="状态快照缺少行人真值字段。")
            count = pedestrians.get("nearby_count_30m")
            nearest = pedestrians.get("nearest_m")
            if count is None:
                return result("本帧没有可用的行人观测；这不代表周围一定没有行人。", "unavailable",
                              reason="行人真值计数缺失。")
            nearby_text = (f"附近 30 米内有 {count} 名行人"
                           if count else "附近 30 米内没有行人")
            nearest_text = f"，最近约 {nearest} 米" if isinstance(nearest, (int, float)) and count else ""
            riders = pedestrians.get("riders_nearby_60m")
            riders_text = f"；60 米内有 {riders} 名骑行者" if isinstance(riders, int) and riders else ""
            view_text = ""
            if isinstance(segmentation, dict) and segmentation.get("status") == "live":
                pixels = (segmentation.get("pixels_by_class") or {})
                people = (pixels.get("pedestrian", 0) + pixels.get("rider", 0))
                view_text = ("。前视语义分割画面中检测到行人或骑行者像素"
                             if segmentation.get("people_in_view")
                             else "。前视画面内未检出行人或骑行者像素")
                evidence.append(f"画面行人+骑行者像素：{people}")
            else:
                view_text = "。前视语义分割数据未就绪，不能确认画面内情况"
            reply = (nearby_text + nearest_text + riders_text + view_text +
                     "。行人位置来自仿真地图真值，不是视觉目标识别。")
            return result(reply, "success",
                          {"type": "pedestrian_query", "pedestrians": pedestrians,
                           "segmentation_live": isinstance(segmentation, dict) and segmentation.get("status") == "live"},
                          reason="结合仿真真值与语义分割像素统计回答。",
                          evidence=evidence + [f"附近行人：{count}", "来源：地图真值 + 语义分割"])
        if intent.kind == "obstacle_query":
            if state.get("source") != "carla":
                return result("当前数据源没有激光雷达、毫米波雷达或障碍物观测。", "unavailable",
                              reason="障碍物感知只在 CARLA 模式可用。")
            sensors = state.get("sensors", {})
            parts, evidence = [], []
            for name, key, describe in (
                    ("lidar", "nearest_obstacle_m", lambda v: f"激光雷达最近回波约 {v['nearest_obstacle_m']:g} 米"),
                    ("depth", "nearest_in_view_m", lambda v: f"前视深度画面最近距离约 {v['nearest_in_view_m']:g} 米"),
                    ("radar", "nearest_m", lambda v: f"毫米波雷达最近目标约 {v['nearest_m']:g} 米（共 {v.get('target_count', 0)} 个）"),
                    ("obstacle", "distance_m", lambda v: f"障碍物传感器最近目标约 {v['distance_m']:g} 米（{v.get('actor_type') or '未知类型'}）")):
                entry = sensors.get(name) if isinstance(sensors, dict) else None
                if not isinstance(entry, dict) or not isinstance(entry.get(key), (int, float)):
                    continue
                value = describe(entry)
                if entry.get("status") != "live":
                    value += f"（数据{entry.get('status', '未就绪')}，未计入当前结论）"
                parts.append(value)
                evidence.append(value)
            if not parts:
                return result("当前没有就绪的障碍物传感器观测，无法给出最近障碍物距离。", "unavailable",
                              reason="激光雷达/毫米波雷达/障碍物传感器均未返回实时数据。", evidence=evidence)
            reply = ("；".join(parts) + "。这些是传感器原始观测，不是目标识别或跟踪结果；"
                     "与同车道前车间距分别计算。")
            return result(reply, "success", {"type": "obstacle_query", "sensors": {
                    name: sensors.get(name) for name in ("lidar", "radar", "depth", "obstacle")
                    if isinstance(sensors, dict) and isinstance(sensors.get(name), dict)}},
                          reason="合并多传感器原始观测并标注各自状态。", evidence=evidence)
        if intent.kind == "light_phase_query":
            light = state.get("environment", {}).get("traffic_light", {})
            if not isinstance(light, dict) or not light.get("affecting"):
                return result("本帧车辆不受任何信号灯约束；没有可回答的变灯时间。", "no_change",
                              {"type": "light_phase"}, reason="当前不在信号灯触发区域内。",
                              evidence=["信号灯影响：否"])
            state_name = {"Red": "红", "Yellow": "黄", "Green": "绿"}.get(light.get("state"), "未知")
            remaining = light.get("state_remaining_s")
            frozen_text = "（该灯已被演示场景冻结，不会自动变化）" if light.get("frozen_by_scenario") else ""
            if not isinstance(remaining, (int, float)):
                reply = f"当前为{state_name}灯，但灯组剩余时间不可用，不能给出变灯时间。"
                return result(reply, "unavailable", {"type": "light_phase", "light": light},
                              reason="灯组计时信息缺失。", evidence=[f"当前灯色：{state_name}"])
            elapsed = light.get("state_elapsed_s")
            elapsed_text = f"，本相位已持续 {elapsed:g} 秒" if isinstance(elapsed, (int, float)) else ""
            next_state = {"红": "绿", "绿": "红", "黄": "红"}.get(state_name, "下一相位")
            reply = (f"当前受{state_name}灯影响{elapsed_text}，按灯组配时估计还剩约 {remaining:g} 秒变{next_state}{frozen_text}。"
                     "这是 CARLA 灯组计时器的估计值，不是车路协同信号。")
            return result(reply, "success", {"type": "light_phase", "light": light},
                          reason="读取灯组计时器估计剩余相位时间。",
                          evidence=[f"当前灯色：{state_name}", f"估计剩余：{remaining} 秒"])
        if intent.kind == "gps_query":
            if state.get("source") != "carla":
                return result("当前数据源没有 GNSS 定位；定位查询只在 CARLA 模式可用。", "unavailable",
                              reason="内存模式没有 GNSS 传感器。")
            sensors = state.get("sensors", {})
            gnss = sensors.get("gnss") if isinstance(sensors, dict) else None
            if not isinstance(gnss, dict) or gnss.get("status") != "live":
                status = gnss.get("status", "未就绪") if isinstance(gnss, dict) else "未就绪"
                return result(f"GNSS 传感器数据未就绪（{status}），不能报告坐标。", "unavailable",
                              reason="GNSS 不在实时状态，禁止编造坐标。",
                              evidence=[f"GNSS 状态：{status}"])
            vehicle = state.get("vehicle", {})
            road = vehicle.get("road_id")
            road_text = f"；地图定位在道路 {road}、车道 {vehicle.get('lane_id', '未知')}" if road is not None else ""
            reply = (f"GNSS 定位：纬度 {gnss.get('latitude')}，经度 {gnss.get('longitude')}，"
                     f"高度 {gnss.get('altitude_m', '未知')} 米{road_text}。"
                     "坐标来自仿真 GNSS 传感器，与真实地球坐标无关。")
            return result(reply, "success", {"type": "gps_query", "gnss": gnss},
                          reason="读取仿真 GNSS 传感器并结合地图道路信息。",
                          evidence=[f"经纬度：{gnss.get('latitude')}, {gnss.get('longitude')}",
                                    f"地图：{state.get('map', '未知')}"])
        if intent.kind == "recorder_control":
            action = intent.option or "start"
            action_label = "开始" if action == "start" else "停止"
            try:
                ack = self.gateway.control_recorder(action)
            except Exception:
                return result(f"{action_label}录制的请求未获得网关确认。", "unavailable",
                              reason="网关调用失败或超时。", evidence=[f"请求动作：{action_label}"],
                              gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"{action_label}录制未执行（{reason_text}）。", "unavailable",
                              reason="网关回执未确认录制操作。",
                              evidence=[f"请求动作：{action_label}", f"回执：{reason_text}"],
                              gateway="未确认")
            path = ack.get("path")
            if action == "start":
                reply = (f"已开始 CARLA 行程录制{f'，文件：{path}' if path else ''}。"
                         "录制记录仿真对象状态、不含传感器附加数据；"
                         "停止后可用 CARLA 回放工具重放（回放会重载仿真世界，需重启演示）。")
            else:
                reply = "CARLA 行程录制已停止；录制文件保留在服务器 tmp/recordings 目录。"
            note = ack.get("note")
            if note:
                reply += f"（{note}）"
            return result(reply, "success", {"type": "recorder", "action": action, "path": path},
                          reason="网关回执确认了录制器状态变化。",
                          evidence=[f"动作：{action_label}录制", "回执：已确认"], gateway="状态已确认")
        if intent.kind == "scenario_injection":
            kind = intent.option or "clear"
            if kind == "clear":
                label = "清除场景"
            elif kind not in SCENARIO_LABELS:
                return result("不支持该注入场景。", "rejected", reason="未知场景类型。")
            else:
                label = SCENARIO_LABELS[kind]
            try:
                ack = self.gateway.inject_scenario(kind)
            except Exception:
                return result(f"场景操作（{label}）未获得网关确认，仿真场景保持不变。", "unavailable",
                              reason="网关调用失败或超时。", evidence=[f"请求场景：{label}"],
                              gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"场景操作（{label}）未执行（{reason_text}）。", "unavailable",
                              reason="网关回执未确认场景操作。",
                              evidence=[f"请求场景：{label}", f"回执：{reason_text}"], gateway="未确认")
            if kind == "clear":
                cleared = ack.get("cleared")
                reply = ("已清除注入场景，交通恢复自主运行。"
                         if cleared else "当前没有注入场景，无需清除。")
                if cleared:
                    reply += f"（刚清除：{SCENARIO_LABELS.get(cleared, cleared)}）"
            else:
                reply = (f"已注入演示场景「{label}」。仿真交通（TM）会自主作出反应，"
                         "DA 不直接控制车辆动作；说“清除场景”可恢复原状。")
            return result(reply, "success", {"type": "scenario", "kind": kind, "label": label},
                          reason="网关回执确认了场景注入/清除。",
                          evidence=[f"场景：{label}", "回执：已确认"], gateway="场景已确认")
        if intent.kind == "speed_control":
            if state.get("source") != "carla":
                return result("当前数据源没有 CARLA 车辆，速度指令不可用。", "unavailable",
                              reason="速度控制只在 CARLA 模式可用。")
            adas = state.get("adas", {})
            limit = adas.get("speed_limit_kmh")
            option = intent.option or "fast"
            if option == "limit":
                if not isinstance(limit, (int, float)) or limit <= 0:
                    return result("当前没有可用的限速基准，不能恢复限速巡航。", "unavailable",
                                  reason="限速缺失。")
                request_value = round(limit)
            elif option.startswith("target:"):
                try:
                    target = int(option.split(":", 1)[1])
                except ValueError:
                    return result("目标车速格式无法识别，请说“开到 40”这样的完整指令。", "clarification",
                                  follow_up=True, reason="话术中的数字无法解析。")
                if not isinstance(limit, (int, float)) or limit <= 0:
                    return result("当前没有可用的限速基准，不能设置绝对车速。", "unavailable",
                                  reason="限速缺失，无法做安全换算。")
                if target > limit:
                    return result(f"目标车速 {target} km/h 超过当前限速 {limit:g} km/h，"
                                  "本请求不发送。可以说一个不超过限速的目标，或“恢复限速”。",
                                  "rejected", reason="超速请求被安全规则拒绝。",
                                  evidence=[f"请求目标：{target} km/h", f"当前限速：{limit:g} km/h"])
                request_value = target
            else:
                request_value = None
            try:
                ack = self.gateway.set_speed(request_value, direction=option if request_value is None else None)
            except Exception:
                return result("车速设置请求未获得网关确认，不能说已经调整。", "unavailable",
                              reason="网关调用失败或超时。",
                              evidence=[f"请求：{option}"], gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"车速未调整（{reason_text}）。", "unavailable",
                              reason="网关回执未确认车速设置。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            previous = ack.get("previous_kmh")
            previous_text = f"从 {previous:g} km/h " if isinstance(previous, (int, float)) else ""
            reply = (f"TM 已确认把目标车速{previous_text}调整为 {ack['target_kmh']:g} km/h"
                     f"（当前限速 {ack.get('speed_limit_kmh', '未知')} km/h，TM 偏移 "
                     f"{ack.get('difference_pct', 0):+g}%）。实际车速还会受跟车、红灯和路况影响；"
                     "车辆仍由 TM 驾驶，这不是直接油门控制。")
            return result(reply, "success",
                          {"type": "speed_target_set", "target_kmh": ack["target_kmh"],
                           "speed_limit_kmh": ack.get("speed_limit_kmh")},
                          reason="目标车速在限速内且网关回执确认。",
                          evidence=[f"新目标：{ack['target_kmh']} km/h",
                                    f"限速：{ack.get('speed_limit_kmh', '未知')} km/h",
                                    f"回执：已确认"], gateway="设置值已确认")
        if intent.kind == "lane_change":
            direction = intent.option
            if direction not in ("left", "right"):
                return result("你想变到哪条车道？请说“向左变道”或“向右变道”；"
                              "不带方向的变道请求不会默认执行。", "clarification", follow_up=True,
                              reason="变道请求缺少方向，不能猜测执行。",
                              evidence=["缺少参数：方向（左/右）"])
            if state.get("source") != "carla":
                return result("当前数据源没有 CARLA 车辆，变道指令不可用。", "unavailable",
                              reason="变道控制只在 CARLA 模式可用。")
            try:
                ack = self.gateway.change_lane(direction)
            except Exception:
                return result("变道请求未获得网关确认，车辆保持当前车道。", "unavailable",
                              reason="网关调用失败或超时。",
                              evidence=[f"请求方向：{'左' if direction == 'left' else '右'}"],
                              gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"变道未执行（{reason_text}），车辆保持当前车道。", "unavailable",
                              reason="网关回执拒绝了变道请求。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            side = "左" if direction == "left" else "右"
            reply = (f"已向 TM 提交向{side}变道路径（车道 {ack.get('from_lane')} → "
                     f"{ack.get('to_lane')}）。变道由 TM 执行并受周围交通影响；"
                     "DA 只下发意图，不直接打方向。")
            return result(reply, "success",
                          {"type": "lane_change", "direction": direction,
                           "from_lane": ack.get("from_lane"), "to_lane": ack.get("to_lane")},
                          reason="方向明确且网关接受了变道路径。",
                          evidence=[f"方向：{side}", f"车道：{ack.get('from_lane')} → {ack.get('to_lane')}",
                                    "回执：路径已提交"], gateway="路径已提交")
        if intent.kind == "route_select":
            route_id = intent.option or "route_b"
            nav = state.get("navigation", {})
            label = {"route_a": "原始路线（经途经点）", "route_b": "备选路线（直达）"}.get(route_id, route_id)
            if nav.get("current_route_id") == route_id:
                return result(f"当前已经在{label}上，本轮无需切换。", "no_change",
                              {"type": "route_unchanged"}, reason="目标路线与当前路线相同。",
                              evidence=[f"当前路线：{route_id}"])
            route = next((r for r in nav.get("routes", []) if r.get("id") == route_id), None)
            if route is None or not isinstance(route.get("eta_min"), (int, float)):
                return result(f"{label}的估计时间不可用，未提交路径请求。", "unavailable",
                              reason="状态中缺少目标路线的 ETA。")
            try:
                ack = self.gateway.change_route(route_id, explicit=True)
            except Exception:
                return result(f"切换到{label}的请求未获得网关确认，保持原路线。", "unavailable",
                              reason="路径提交失败或超时。",
                              evidence=[f"请求路线：{route_id}"], gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted") or ack.get("route_id") != route_id:
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"切换到{label}未获确认（{reason_text}），保持原路线。", "unavailable",
                              reason="路径回执未确认。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            estimate = "几何演示估计" if nav.get("eta_source") == "geometric_demo_estimate" else "网关估计"
            reply = (f"已按你的指定提交{label}路径（{estimate}约 {ack['eta_min']:g} 分钟）。"
                     "这只确认 TM 接收了路径，实际走哪条街由 TM 导航决定，轨迹未逐段验证。")
            return result(reply, "success",
                          {"type": "route_changed", "route_id": route_id, "eta_min": ack["eta_min"],
                           "selection": "explicit", "verification": "path_submitted_not_trajectory_verified"},
                          reason="用户显式指定路线，网关接受了路径请求。",
                          evidence=[f"新路线：{route_id}", f"估计剩余：{ack['eta_min']:g} 分钟",
                                    "轨迹未验证"], gateway="路径已提交；轨迹未验证")
        if intent.kind == "emergency_stop":
            speed = state.get("adas", {}).get("current_speed_kmh")
            try:
                ack = self.gateway.emergency_stop()
            except Exception:
                return result("紧急停车请求未获得网关确认，请立即人工接管。", "unavailable",
                              reason="网关调用失败或超时。",
                              evidence=[f"当时车速：{speed} km/h"], gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"紧急停车未执行（{reason_text}），请立即人工接管。", "unavailable",
                              reason="网关回执未确认紧急停车。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            reply = ("已执行紧急停车：TM 自动驾驶已断开，满刹车并拉起手刹。"
                     "刹停距离取决于当前车速和路面（执行时车速 "
                     f"{speed if isinstance(speed, (int, float)) else '未知'} km/h）。"
                     "紧急停车不依赖数据新鲜度，也不会被告警抢占；恢复说“恢复自动驾驶”。")
            return result(reply, "success",
                          {"type": "emergency_stop", "speed_at_request_kmh": speed,
                           "handbrake": bool(ack.get("handbrake", True))},
                          reason="紧急停车是唯一绕过新鲜度门且不被告警抢占的写操作。",
                          evidence=[f"执行时车速：{speed} km/h", "TM：断开", "手刹：拉起"],
                          gateway="控制已执行")
        if intent.kind == "set_destination":
            heading = intent.option
            if state.get("source") != "carla":
                ack = self.gateway.set_destination(heading)
                note = ack.get("note", "") if isinstance(ack, dict) else ""
                return result(f"内存演示仅记录目的地变更请求（{heading or '随机'}）。{note}",
                              "success" if isinstance(ack, dict) and ack.get("accepted") else "unavailable",
                              {"type": "destination_changed", "heading": heading},
                              reason="内存模式没有真实导航。")
            try:
                ack = self.gateway.set_destination(heading)
            except Exception:
                return result("目的地变更请求未获得网关确认，保持原目的地。", "unavailable",
                              reason="网关调用失败或超时。",
                              evidence=[f"请求方向：{heading or '随机'}"], gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"目的地未变更（{reason_text}），保持原目的地。", "unavailable",
                              reason="网关回执未确认目的地变更。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            requested = ack.get("requested_heading", heading or "未指定")
            actual = ack.get("heading", "未知")
            direction_note = "" if requested == actual else f"（请求{requested}，实际选到{actual}方向候选点）"
            reply = (f"已把目的地改为{actual}方向约 {ack.get('distance_m', '未知')} 米处"
                     f"{direction_note}，并重新提交 TM 路径（几何估计约 {ack.get('eta_min', '未知')} 分钟）。"
                     "车辆将按新目的地导航；这是地图路网内的演示目的地，不是真实地名。")
            return result(reply, "success",
                          {"type": "destination_changed", "heading": actual,
                           "distance_m": ack.get("distance_m"), "eta_min": ack.get("eta_min")},
                          reason="网关接受了新目的地的路径提交。",
                          evidence=[f"方向：{actual}", f"距离：{ack.get('distance_m')} 米",
                                    "路径已提交；轨迹未验证"], gateway="路径已提交")
        if intent.kind == "drive_pause":
            option = intent.option or "pause"
            target_status = "off" if option == "pause" else "active"
            current = state.get("adas", {}).get("noa_status")
            label = "暂停" if option == "pause" else "恢复"
            if current == target_status:
                return result(f"TM 自动驾驶已经处于{'暂停' if target_status == 'off' else '运行'}状态，本轮无需修改。",
                              "no_change", {"type": "drive_state", "status": target_status},
                              reason="目标状态与当前状态相同。")
            try:
                self.gateway.set_noa_status(target_status)
            except Exception as exc:
                return result(f"{label}请求未获确认（{exc}）。", "unavailable",
                              reason="网关调用失败或前置条件不满足。",
                              evidence=[f"目标：{label} TM"], gateway="请求失败")
            if target_status == "off":
                reply = ("TM 自动驾驶已暂停，车辆制动停止。现在可以下达直接控制步进指令"
                         "（“踩油门”“刹车”“左打方向”）；说“恢复自动驾驶”交还 TM。")
            else:
                reply = "TM 自动驾驶已恢复，按当前跟车距离、目标车速和路线设置继续行驶。"
            return result(reply, "success", {"type": "drive_state", "status": target_status},
                          reason="网关确认了驾驶状态切换。",
                          evidence=[f"新状态：{target_status}"], gateway="状态已确认")
        if intent.kind == "direct_control":
            if state.get("source") != "carla":
                return result("当前数据源没有 CARLA 车辆，直接控制不可用。", "unavailable",
                              reason="直接控制只在 CARLA 模式可用。")
            action = intent.option
            noa_status = state.get("adas", {}).get("noa_status")
            if noa_status != "off":
                return result("TM 正在驾驶，直接车辆控制被分层权限拒绝。"
                              "请先说“暂停自动驾驶”；或者用意图级指令：“开到 40”“向左变道”。",
                              "rejected", {"type": "direct_control_denied"},
                              reason="直接控制只在 TM 暂停时开放。",
                              evidence=[f"TM 状态：{noa_status}", "分层：意图指令可用 / 直接控制需暂停 TM"])
            try:
                ack = self.gateway.direct_control(action or "")
            except Exception:
                return result("直接控制请求未获得网关确认，车辆控制保持不变。", "unavailable",
                              reason="网关调用失败或超时。", evidence=[f"动作：{action}"],
                              gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted"):
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"直接控制未执行（{reason_text}）。", "unavailable",
                              reason="网关回执未确认控制动作。",
                              evidence=[f"回执：{reason_text}"], gateway="未确认")
            control = ack.get("control", {})
            reply = (f"已执行直接控制步进（{ack.get('action')}）：油门 {control.get('throttle', 0):.2f}、"
                     f"刹车 {control.get('brake', 0):.2f}、转向 {control.get('steer', 0):.2f}。"
                     "该状态会保持到下一条指令；说“恢复自动驾驶”交还 TM。")
            return result(reply, "success", {"type": "direct_control", "action": ack.get("action"),
                                             "control": control},
                          reason="TM 已暂停且网关确认了控制步进。",
                          evidence=[f"动作：{ack.get('action')}",
                                    f"油门/刹车/转向：{control.get('throttle')}/{control.get('brake')}/{control.get('steer')}"],
                          gateway="控制已执行")
        if intent.kind == "explain_event":
            if not isinstance(event, dict):
                return result("当前没有告警事件，车辆参数和路线没有变化。", "no_change",
                              reason="状态快照中没有可解释的告警。")
            return result(self._event_reply(event, state.get("source") == "carla"),
                          "success", {"type": "adas_event", "code": event.get("code")},
                          reason="依据当前事件码解释告警，没有调用写工具。",
                          evidence=[f"事件码：{event.get('code', '未知')}"])

        if intent.kind == "follow_distance":
            current = state.get("adas", {}).get("follow_distance_level")
            if not isinstance(current, int):
                return result("当前跟车距离状态不可用，未发送设置请求。", "unavailable",
                              reason="缺少当前等级，无法安全计算目标等级。")
            target = intent.target if intent.target is not None else current + intent.direction
            if target == current:
                return result(f"仿真跟车距离已经是 {current} 级，本轮无需修改。",
                              "no_change", {"type": "preference_unchanged", "value": current},
                              reason="目标等级与当前等级相同。",
                              evidence=[f"当前等级：{current}", f"目标等级：{target}"])
            issue = check_preference(state, target)
            if issue:
                return result(f"{issue} 当前为 {current} 级，目标为 {target} 级；未发送设置请求。",
                              "rejected", reason="NOA/TM 状态或目标等级不满足设置条件。",
                              evidence=[f"自动驾驶状态：{state.get('adas', {}).get('noa_status')}",
                                        f"当前等级：{current}", f"目标等级：{target}"])
            try:
                ack = self.gateway.set_preference("follow_distance", target)
            except Exception:
                return result("跟车距离设置请求未获得网关确认，不能说已经调好。",
                              "unavailable", reason="网关调用失败或超时。",
                              evidence=[f"请求目标：{target} 级"], gateway="请求失败")
            if not isinstance(ack, dict) or not ack.get("accepted") or ack.get("value") != target:
                reason_text = ack.get("reason", "缺少明确确认") if isinstance(ack, dict) else "回执无效"
                return result(f"跟车距离未得到网关确认（{reason_text}），页面不把它记为已设置。",
                              "unavailable", reason="网关回执未确认目标值。",
                              evidence=[f"请求目标：{target} 级", f"回执：{reason_text}"],
                              gateway="未确认")
            front = state.get("environment", {}).get("front_vehicle")
            measured = (f"本轮采集的前车间距为 {front['gap_m']:g} 米，设置后的变化需要后续观察。"
                        if isinstance(front, dict) and isinstance(front.get("gap_m"), (int, float))
                        else "本轮没有可用的同车道前车间距，不能确认物理车距变化。")
            reply = (f"CARLA 已确认把 TM 跟车设置从 {current} 级调到 {target} 级（参数 {ack['distance_m']:g} 米）。"
                     + measured
                     if state.get("source") == "carla" and isinstance(ack.get("distance_m"), (int, float))
                     else f"网关已确认把跟车距离从 {current} 级调到 {target} 级。")
            return result(reply, "success",
                          {"type": "preference_changed", "name": "follow_distance", "value": target,
                           "distance_m": ack.get("distance_m")},
                          reason="目标在 1–5 级范围内，TM/NOA 可用，网关回执确认目标值。",
                          evidence=[f"原等级：{current}", f"目标等级：{target}",
                                    f"设置米数：{ack.get('distance_m', '未提供')}"],
                          gateway="设置值已确认")

        if intent.kind == "faster_route":
            if state.get("source") == "carla" and any(x in req.utterance for x in ("堵", "拥堵")):
                return result("我没有实时拥堵数据，不能确认哪条路不堵。本轮不改道；"
                              "如果只想比较演示用几何估计，请明确说“按演示估计切换”。",
                              "clarification", {"type": "traffic_clarification"},
                              follow_up=True, reason="用户要求避堵，但当前只具备几何路线估计。",
                              evidence=["未接入实时交通数据", "未提交路径请求"])
            route, _ = faster_route(state)
            if route is None:
                nav = state.get("navigation", {})
                routes = nav.get("routes", [])
                current_route = next((r for r in routes if r.get("id") == nav.get("current_route_id")), None)
                candidates = [r for r in routes if r.get("id") != nav.get("current_route_id")]
                current_eta = current_route.get("eta_min") if current_route else "未知"
                candidate_eta = candidates[0].get("eta_min") if candidates else "无"
                reply = (f"目前无法确认有更快路线：当前估计 {current_eta} 分钟，"
                         f"候选估计 {candidate_eta} 分钟。未向 CARLA 提交新路径。")
                return result(reply, "no_change", {"type": "route_unchanged"},
                              reason="没有比当前估计更短的候选路线。",
                              evidence=[f"当前 ETA：{current_eta}", f"候选 ETA：{candidate_eta}",
                                        f"来源：{nav.get('eta_source', '网关')}"])
            try:
                ack = self.gateway.change_route(route["id"])
            except Exception:
                return result("备选路径请求没有得到网关确认，页面仍保留原路线。",
                              "unavailable", reason="路径提交失败或超时。",
                              evidence=[f"请求路线：{route['id']}"], gateway="请求失败")
            if (not isinstance(ack, dict) or not ack.get("accepted")
                    or ack.get("route_id") != route["id"]
                    or not isinstance(ack.get("eta_min"), (int, float))):
                return result("备选路径未得到有效回执，不能报告路线已切换。",
                              "unavailable", reason="路径回执未同时确认路线 ID 和 ETA。",
                              evidence=[f"请求路线：{route['id']}"], gateway="未确认")
            current_route_id = state["navigation"]["current_route_id"]
            current_eta = next(r["eta_min"] for r in state["navigation"]["routes"] if r["id"] == current_route_id)
            saving = round(max(0, current_eta - ack["eta_min"]), 1)
            if state.get("source") == "carla":
                reply = (f"CARLA 的 TM 已接收备选路径：几何估计从 {current_eta:g} "
                         f"到 {ack['eta_min']:g} 分钟，差 {saving:g} 分钟。"
                         "这只确认路径提交，尚未验证车辆实际走完新路线。")
            else:
                reply = (f"已切换更快路线，预计节省 {saving:g} 分钟，剩余 {ack['eta_min']:g} 分钟。"
                         if saving > 0 else f"已切换路线，网关返回剩余 {ack['eta_min']:g} 分钟，未确认节省时间。")
            return result(reply, "success",
                          {"type": "route_changed", "route_id": route["id"],
                           "new_remaining_time_min": ack["eta_min"], "saved_time_min": saving,
                           "estimate_source": state.get("navigation", {}).get("eta_source"),
                           "verification": "path_submitted_not_trajectory_verified" if state.get("source") == "carla" else "gateway_confirmed"},
                          reason="候选 ETA 小于当前 ETA，网关接受了路径请求。",
                          evidence=[f"原 ETA：{current_eta:g} 分钟",
                                    f"新 ETA：{ack['eta_min']:g} 分钟",
                                    f"估计来源：{state.get('navigation', {}).get('eta_source', '网关')}"],
                          gateway="路径已提交；轨迹未验证" if state.get("source") == "carla" else "切换已确认")

        return result("请再说清楚您的驾驶需求。", "clarification", follow_up=True,
                      reason="当前话术无法映射到可用的受限工具。")

    @staticmethod
    def _motion_text(state):
        return {
            "moving": "本帧车辆正在移动。",
            "waiting_red_light": "车辆当前受红灯影响；这是停止的可见原因之一。",
            "blocked_by_vehicle": "同车道前方有静止或缓慢车辆，可能阻挡当前行驶。",
            "paused": "仿真 TM 已暂停，车辆处于停止状态。",
            "possibly_stuck": "车辆已持续静止且仍有油门请求，可能受阻或卡住；还不能确认具体障碍。",
        }.get(state, "当前处于静止或原因未知状态，现有证据不足以确定停车原因。")

    def _live_query(self, kind, state, result):
        env = state.get("environment")
        if not isinstance(env, dict) or state.get("source") != "carla":
            return result("当前数据源没有 CARLA 道路与周边观测，暂时无法回答这个问题。", "unavailable",
                          reason="缺少实时环境输入。")
        frame = state.get("telemetry", {}).get("frame")
        evidence = [f"CARLA 仿真帧：{frame}", "来源：仿真世界状态与传感器事件"]
        if kind == "front_distance":
            front = env.get("front_vehicle")
            if not isinstance(front, dict) or not isinstance(front.get("gap_m"), (int, float)):
                return result("本帧的搜索范围内没有可用的同车道前车间距；这不代表周围没有车辆。", "no_change",
                              {"type": "front_distance"}, reason="没有满足同车道、同向、前方条件的车辆观测。", evidence=evidence)
            reply = (f"本帧同车道前车（ID {front.get('id')}）与本车的估算车身间距为 {front['gap_m']:g} 米，"
                     f"前车速度 {front.get('speed_kmh', '未知')} km/h。这由 CARLA 车辆位置与车身尺寸计算。")
            return result(reply, "success", {"type": "front_distance", "front_vehicle": front},
                          reason="读取实测位置计算的前车间距，没有修改跟车设置。", evidence=evidence)
        if kind == "road_context":
            vehicle = state.get("vehicle", {})
            road, lane = vehicle.get("road_id"), vehicle.get("lane_id")
            if road is None or lane is None:
                return result("本帧没有可用的道路或车道信息。", "unavailable", reason="地图 waypoint 不可用。", evidence=evidence)
            pos = vehicle.get("location", {})
            reply = f"当前位于 CARLA 道路 {road}、车道 {lane}，{'在' if vehicle.get('is_junction') else '不在'}路口范围内。"
            if all(isinstance(pos.get(k), (int, float)) for k in ("x", "y")):
                reply += f" 地图坐标约为 x={pos['x']:.1f}、y={pos['y']:.1f} 米。"
            return result(reply, "success", {"type": "road_context", "road_id": road, "lane_id": lane},
                          reason="读取 CARLA 地图道路与车道标识。", evidence=evidence)
        if kind == "recent_events":
            events = state.get("events", [])[-3:]
            if not events:
                return result("本次连接以来暂无已记录的传感器或操作台事件。", "no_change",
                              {"type": "recent_events"}, reason="事件记录为空，不能推断从未发生过事件。", evidence=evidence)
            texts = []
            for item in events:
                source = "CARLA 传感器" if item.get("source") == "carla_sensor" else "操作台模拟注入"
                texts.append(f"{source}／帧 {item.get('frame', '未知')}：{item.get('message') or item.get('code')}")
            return result("最近记录：" + "；".join(texts) + "。", "success", {"type": "recent_events", "events": events},
                          reason="根据已记录事件回答，并区分事件来源。", evidence=evidence)
        motion = env.get("motion", {})
        speed = state.get("adas", {}).get("current_speed_kmh")
        light = env.get("traffic_light", {})
        reply = self._motion_text(motion.get("state"))
        evidence += [f"车速：{speed} km/h", f"静止时间：{motion.get('stationary_s', '未知')} 秒",
                     f"交通灯：{light.get('state') if light.get('affecting') else '当前无生效灯色'}"]
        return result(reply, "success", {"type": "stop_reason", "motion": motion},
                      reason="结合车速、交通灯、前车与持续静止时间给出有证据的说明。", evidence=evidence)

    @staticmethod
    def _event_reply(event: dict, simulated: bool = False) -> str:
        if event.get("code") == "takeover_required":
            prefix = "演示操作台已注入模拟接管告警。" if simulated else "系统提示需要接管。"
            return f"{prefix} 请立即观察路况并按车辆提示接管驾驶。"
        message = str(event.get("message") or "收到未知 ADAS 告警")
        return f"车辆提示：{message}。请留意路况和车辆提示。"

