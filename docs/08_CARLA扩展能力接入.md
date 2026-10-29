# CARLA 扩展能力接入（08）

本文记录在 07（实时数据接入）基础上新接入的 CARLA 能力：多传感器、行人与骑行者、天气、信号灯相位与场景注入、行程录制、同步模式与 OSM 地图导入。所有新能力遵循同一原则：**数据如实标注来源与方法，写操作必须拿到网关回执，禁止把估计值当实测值报告**。

## 1. 扩展传感器套件

全部挂在演示车辆上，数值进入 `snapshot.sensors`，各自带 `status / age_ms / frame`（与画面新鲜度同一套规则，>2 s 记为 stale）：

| 传感器 | CARLA 蓝图 | 关键参数 | 快照字段 |
| --- | --- | --- | --- |
| GNSS | `sensor.other.gnss` | 0.5 s | `latitude / longitude / altitude_m` |
| IMU | `sensor.other.imu` | 0.1 s | `accelerometer[3] / gyroscope[3] / compass_deg` |
| 激光雷达 | `sensor.lidar.ray_cast` | 16 线，30 m，24 k 点/s，0.2 s | `points / nearest_obstacle_m`（前向锥形区域最近回波） |
| 毫米波雷达 | `sensor.other.radar` | 25°×10°，40 m，0.2 s | `targets[]（distance/azimuth/velocity）`、`nearest_m` |
| 语义分割 | `sensor.camera.semantic_segmentation` | 300×168，0.5 s | `pixels_by_class`（行人/骑行者/车辆/交通灯/标志）、`people_in_view` |
| 深度相机 | `sensor.camera.depth` | 300×168，0.5 s | `nearest_in_view_m`（采样像素最小深度） |
| 障碍物检测 | `sensor.other.obstacle` | 18 m，0.2 s | `distance_m / actor_type` |

解析为纯 Python（`struct`/bytes），不依赖 numpy；语义分割与深度图只输出统计值，不进采样缓存当图片存。传感器生成失败不中断演示，错误记入 `telemetry.sensor_errors` 并在快照中显示 waiting。

## 2. 行人与骑行者

启动时在人行道随机生成最多 6 名 AI 行人（`walker.pedestrian.*` + `controller.ai.walker`，自主漫游），并投放最多 2 辆两轮车（骑行者）进入 TM 交通。`environment.pedestrians` 输出：30 m 内行人数量与最近距离（地图真值）、60 m 内骑行者数。行人查询话术同时引用地图真值与前视语义分割像素统计，两者来源分开表述。

## 3. 天气系统

7 个预设：晴天/晴天黄昏/夜晚/多云/中雨/暴雨/浓雾（`carla.WeatherParameters` 预设）。说话术即可切换（“下雨”“起雾”“天黑”“晴天”），或控制台 `POST /api/weather`。每次采样回读 `cloudiness / precipitation / fog_density / wetness / sun_altitude` 等参数进 `environment.weather`；查询话术引用这些实测参数而不是只报预设名。天气是仿真环境写操作：需实时数据、无紧急告警，且拿网关回执。

## 4. 信号灯相位与场景注入

- 相位查询：`environment.traffic_light` 增加 `light_id / state_elapsed_s / state_time_s / state_remaining_s`。剩余时间是 CARLA 灯组计时器的估计值，回复明确标注，不冒充车路协同信号。
- 场景注入（对话话术或 `POST /api/scenario`）：
  - `red_light_ahead`：前方最近信号灯置红并冻结（`set_state` + `freeze(True)`），清除时恢复原配时；
  - `stalled_vehicle_ahead`：同车道前方 45/60 m 注入两辆带手刹的事故车；
  - `pedestrian_crossing`：前方 28 m 生成横穿马路的 AI 行人。
- 注入会改变 TM 交通的真实行为（例如“为什么停了”会回答 blocked_by_vehicle）；同一时间只允许一个场景，清除话术或重置演示可恢复。事件流中分别记录 `scenario_injected / scenario_cleared`。

## 5. 行程录制

`client.start_recorder(path, additional_data=False)`，文件落在项目 `tmp/recordings/`。对话“开始录制/停止录制”或 `POST /api/recorder`。快照 `recorder.active/path` 如实反映状态。注意：CARLA 回放会重载仿真世界，本演示不在线内做回放；回放需重启演示。

## 6. 同步模式

默认仍是异步（服务器自主推进）。控制台 `POST /api/sync-mode {"enabled": true}` 切换：采集线程接管 tick，20 Hz 定步长（`fixed_delta_seconds=0.05`），Traffic Manager 同步开启。切换瞬间清空缓存等新帧，避免旧数据贴新标签。同步模式是基础设施开关（不在对话意图里），用于确定性复现；断连或异常时保持最后模式并在回执中说明失败原因。

## 7. OSM 地图导入

`python -m da_mvp.web_demo --mode carla --osm-map path/to/map.osm`：用 `carla.Osm2Odrl` 把本地 OpenStreetMap 文件转成 OpenDRIVE 路网并生成世界。需要自备 `.osm` 文件（含道路数据）；转换失败直接报错退出，不回退到默认地图假装成功。CARLA 自带地图仍用 `--town` 切换。

## 8. 明确未接入的能力与原因

| 能力 | 原因 |
| --- | --- |
| ROS / ROS2 桥接 | 需要完整 ROS 安装与独立的 bridge 进程，本机 Windows 环境没有；属基础设施级依赖，不适合塞进本项目。待有 ROS 环境时可通过 `Gateway` 协议替换接入。 |
| 在线回放（replay_file） | 回放会停止并重载当前世界，与持续采集线程和已生成 actor 冲突；只做录制，回放请用 CARLA 工具离线进行。 |
| 车辆物理级仿真调参 | TM 拥有执行侧，DA 不直接改车辆动力学；与“DA 不直接控制车辆”的安全边界一致。 |

## 9. API 与快照变更

- `GET /api/agent-input` 的 `schema_version` 升到 **1.1**（纯新增字段：`sensors`、`recorder`、`scenario`、`simulation_mode`、`environment.weather`、`environment.pedestrians`、`traffic_light` 相位字段）。
- 新端点：`POST /api/weather`、`POST /api/recorder`、`POST /api/scenario`、`POST /api/sync-mode`（控制台级，与对话路径同走网关回执校验）。
- 持续采样缓存（300 条）随采样自然携带新字段；导出 JSONL 不变。

## 10. 验收要点

1. `python -m unittest discover -s tests`：76 项全过（含 `tests/test_expanded_capabilities.py` 的意图解析、内存模式拒止、快照只读查询、stale 传感器标注、写操作回执链）。
2. CARLA 实机：快照 `sensors` 各字段为 live 且年龄 <2 s；说话“下雨”后画面出现降雨且 `weather.precipitation_pct > 0`；注入事故车后“为什么停了”引用同车道前车；录制开始后 `tmp/recordings/` 出现文件；同步模式切换后采样继续且帧号连续。
3. 所有新写操作在 CARLA 状态非实时、存在紧急告警时被拒止，回复给出原因，不留半执行状态。
