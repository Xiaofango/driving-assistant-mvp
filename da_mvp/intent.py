"""Conservative MVP grammar. Unrecognized text must not cause a write."""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Intent:
    kind: str
    direction: int = 0
    target: int | None = None
    option: str | None = None


WEATHER_SET_PATTERNS = (("暴雨", "hard_rain_noon"), ("大雨", "hard_rain_noon"), ("雨大", "hard_rain_noon"),
                        ("小雨", "rain_noon"), ("下雨", "rain_noon"), ("雨天", "rain_noon"),
                        ("浓雾", "fog_noon"), ("大雾", "fog_noon"), ("起雾", "fog_noon"),
                        ("有雾", "fog_noon"), ("雾天", "fog_noon"),
                        ("多云", "cloudy_noon"),
                        ("夜晚", "clear_night"), ("夜间", "clear_night"), ("晚上", "clear_night"),
                        ("天黑", "clear_night"), ("天亮", "clear_noon"),
                        ("黄昏", "clear_sunset"), ("日落", "clear_sunset"), ("傍晚", "clear_sunset"),
                        ("晴天", "clear_noon"), ("放晴", "clear_noon"), ("雨停", "clear_noon"))


def resolve(utterance: str) -> Intent:
    text = utterance.strip().lower().replace(" ", "")
    if any(x in text for x in ("不要", "不用", "别把", "别调", "不需要", "保持原样")):
        return Intent("no_action")
    if any(x in text for x in ("为什么停", "怎么停", "为什么不走", "怎么不走", "为什么还不走", "停车原因")):
        return Intent("stop_reason")
    if any(x in text for x in ("前车离", "距离前车", "实际车距", "实测车距", "前车距离", "前车多远")):
        return Intent("front_distance")
    if any(x in text for x in ("清除场景", "恢复场景", "结束场景", "取消场景")):
        return Intent("scenario_injection", option="clear")
    if any(x in text for x in ("模拟红灯", "红灯场景", "注入红灯", "模拟信号灯")):
        return Intent("scenario_injection", option="red_light_ahead")
    if any(x in text for x in ("事故车", "抛锚", "坏车", "模拟事故", "前方事故", "模拟拥堵", "模拟堵车")):
        return Intent("scenario_injection", option="stalled_vehicle_ahead")
    if any(x in text for x in ("行人横穿", "模拟行人", "横穿马路")):
        return Intent("scenario_injection", option="pedestrian_crossing")
    if any(x in text for x in ("车道", "哪条路", "当前道路", "当前位置", "在哪条路")) and not any(x in text for x in ("变道", "换道", "切换", "改走")):
        return Intent("road_context")
    if any(x in text for x in ("刚才发生", "最近发生", "最近事件", "碰撞记录", "压线记录")):
        return Intent("recent_events")
    if "录制" in text or "录一下" in text or "帮我录" in text:
        stop = any(x in text for x in ("停止", "结束", "别录"))
        return Intent("recorder_control", option="stop" if stop else "start")
    if (any(x in text for x in ("天气", "能见度", "视线怎么样"))
            or (any(x in text for x in ("下雨", "天黑", "天亮", "雨大", "有雾")) and "吗" in text)):
        return Intent("weather_query")
    for pattern, preset in WEATHER_SET_PATTERNS:
        if pattern in text:
            return Intent("set_weather", option=preset)
    if any(x in text for x in ("障碍", "雷达")):
        return Intent("obstacle_query")
    if ("灯" in text and any(x in text for x in ("还有", "还剩", "几秒", "多久", "变绿"))) or "变绿" in text:
        return Intent("light_phase_query")
    if any(x in text for x in ("行人", "路人", "过马路")):
        return Intent("pedestrian_query")
    if any(x in text for x in ("经纬度", "gps", "我们在哪", "现在位置", "坐标", "在哪")):
        return Intent("gps_query")
    if any(x in text for x in ("超车", "闯红灯", "逆行")):
        return Intent("forbidden")
    if any(x in text for x in ("变道", "换道")):
        direction = "left" if "左" in text else "right" if "右" in text else None
        return Intent("lane_change", option=direction)
    if text in ("开车", "开车吧", "继续开车", "起步吧"):
        return Intent("drive_pause", option="resume")
    if any(x in text for x in ("紧急停车", "立刻停车", "立即停车", "紧急制动", "马上停车")):
        return Intent("emergency_stop")
    if any(x in text for x in ("暂停自动驾驶", "关闭noa", "关掉noa", "靠边停车", "停车", "停下", "刹停")):
        return Intent("drive_pause", option="pause")
    if any(x in text for x in ("恢复自动驾驶", "打开noa", "开启noa", "恢复行驶", "继续开", "起步")):
        return Intent("drive_pause", option="resume")
    if any(x in text for x in ("恢复限速", "恢复正常车速", "按限速开")):
        return Intent("speed_control", option="limit")
    if (any(x in text for x in ("开到", "开至", "提到", "提速", "加速", "开快点", "快点开",
                                "开慢点", "慢点开", "慢一点", "减速", "降速", "慢速"))
            or (any(x in text for x in ("车速", "速度", "时速")) and re.search(r"\d+", text)
                and not any(x in text for x in ("跟车", "车距", "前车", "多少", "几级", "限速")))):
        digits = re.search(r"(\d+)", text)
        if digits and "多快" not in text and any(k in text for k in ("开到", "开至", "提到", "车速", "速度", "时速")):
            return Intent("speed_control", option=f"target:{digits.group(1)}")
        slow = any(x in text for x in ("慢", "减速", "降速"))
        return Intent("speed_control", option="slow" if slow else "fast")
    if any(x in text for x in ("踩油门", "给油", "松油门", "收油", "踩刹车", "刹车", "制动",
                               "左打方向", "向左打", "右打方向", "向右打", "回正方向", "方向回正")):
        if any(x in text for x in ("踩刹车", "刹车", "制动")):
            action = "brake"
        elif any(x in text for x in ("松油门", "收油")):
            action = "throttle_off"
        elif any(x in text for x in ("踩油门", "给油")):
            action = "throttle_up"
        elif "回正" in text:
            action = "steer_center"
        else:
            action = "steer_left" if any(x in text for x in ("左打", "向左")) else "steer_right"
        return Intent("direct_control", option=action)
    if any(x in text for x in ("告警", "报警", "接管", "警报")):
        return Intent("explain_event")
    if any(x in text for x in ("跟车", "车距", "前车")):
        level = re.search(r"(?:到|为|成|设为|设置为|调到|调成)([1-5一二三四五])级", text)
        if level:
            target = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}.get(level.group(1), None)
            return Intent("follow_distance", target=target if target is not None else int(level.group(1)))
        if any(x in text for x in ("远一点", "调远", "加大", "增大", "拉开", "远些", "太近", "有点近")):
            return Intent("follow_distance", 1)
        if any(x in text for x in ("近一点", "调近", "减小", "缩小", "靠近", "近些", "太远", "有点远")):
            return Intent("follow_distance", -1)
        return Intent("status") if any(x in text for x in ("多少", "几级", "现在")) else Intent("clarify")
    if any(x in text for x in ("备选路线", "走备选", "切换到备选", "换成备选", "直达路线")):
        return Intent("route_select", option="route_b")
    if any(x in text for x in ("原始路线", "原路线", "回到原", "走途经点", "恢复路线")):
        return Intent("route_select", option="route_a")
    if any(x in text for x in ("换个目的地", "换一个目的地", "改目的地", "新的目的地")):
        return Intent("set_destination", option="any")
    for heading in ("东北", "东南", "西北", "西南", "东", "南", "西", "北"):
        if f"去{heading}" in text or f"开往{heading}" in text or f"改去{heading}" in text:
            return Intent("set_destination", option=heading)
    if any(x in text for x in ("换一条更快", "换条更快", "更快的路", "更快路线", "不堵车的路线",
                               "避开拥堵", "绕开堵车", "换一条不堵", "前面堵车", "重新规划路线",
                               "按演示估计切换")):
        return Intent("faster_route")
    if any(x in text for x in ("还有多久到", "多久到目的地", "预计多久到", "到目的地还有多久")):
        return Intent("eta_query")
    if any(x in text for x in ("路况怎么样", "现在路况", "拥堵情况", "堵不堵")):
        return Intent("traffic_status")
    if any(x in text for x in ("自动驾驶开了", "noa状态", "驾驶状态", "现在车速",
                               "车速多少", "现在自动驾驶", "我现在开多快", "开多快",
                               "当前速度", "速度多少")):
        return Intent("status")
    return Intent("clarify")

