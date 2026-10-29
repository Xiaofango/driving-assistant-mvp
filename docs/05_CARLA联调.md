# CARLA 仿真联调说明

2026-10-28 更新：后台已持续采集世界帧、车辆与周边环境、独立摄像头时间和传感器事件；默认使用 CARLA。最新字段、输入接口与新鲜度规则见 [实时数据接入](07_CARLA实时数据接入.md)。下面的 2026-10-26 验收记录为历史记录。

版本：0.2，2026-10-26。本机 CARLA 0.9.16 已低画质启动并通过实时画面、车速、跟车距离、路径提交和接管告警验证。

## 接入内容

- CARLA 0.9.16 仿真车由 Traffic Manager 自动行驶，前视 RGB 摄像头画面在演示页面实时显示。
- 页面车速由车辆速度向量计算；限速来自 CARLA 车辆 API。
- DA 的“跟车距离调远一点”映射到 Traffic Manager `distance_to_leading_vehicle`，等级 1–5 对应 3、5、7、9、12 米。
- DA 的“换一条更快的路”向 Traffic Manager 提交同一目的地的较短路径，初始路线额外经过一个途经点。路线 ETA 用几何距离按 30 km/h 计算，仅用于演示比较，不是导航引擎路况预测。
- 接管告警由演示面板注入，尚无真实 DMS/ADAS 告警源。
- 页面“关闭 NOA”是仿真操作台开关，映射到 Traffic Manager autopilot，并在关闭时让仿真车制动；DA 自然语言不能直接开关或控制车辆。

## 本机限制

本机为 Radeon 780M 集成显卡。CARLA 官方的 0.9.16 Windows 快速入门建议独显、至少 8 GB 显存。低画质模式已在本机运行并产生 800×450 的实时摄像头画面；长时间稳定性和帧率尚未测定。服务端包约 7.8 GB，安装在 D 盘，避免占满工作区所在的 C 盘。首次启动还需要微软 [DirectX End-User Runtime](https://www.microsoft.com/en-us/download/details.aspx?id=35) 的旧版组件。

## 启动与验收

1. 在项目根目录执行 `.\scripts\start_carla_low.ps1`，等待 2000 端口可访问。脚本使用低画质和 `-RenderOffScreen`；Off-screen 仍渲染摄像头，不要使用 `no_rendering_mode`，它会使摄像头无数据。
2. 用根目录 `requirements-carla.txt` 安装客户端依赖。
3. `python -m da_mvp.web_demo --mode carla`，打开 `http://127.0.0.1:8765`。
4. 等待“车辆运行画面”出现，道路景物移动，车速从 0 变化。
5. 输入“跟车距离调远一点”：页面等级与米数变化，DA 回复含 CARLA 设置结果。
6. 输入“换一条更快的路”：页面当前路线更新，回复明确为几何估计，仿真车辆继续行驶。
7. 触发接管告警后再尝试写操作：告警优先，路线/跟车状态不改变。

若服务端启动失败或摄像头无画面，不应把内存模式页面当作 CARLA 联动成功。`--carla-host <地址>` 可复用外部有独显的 CARLA 服务端。

## 2026-10-26 本机验收记录

- 地图：`Town10HD_Opt`。前视和车外摄像头均返回可解码 JPEG，道路和车辆可见；采样车速从 28.8 km/h 降至停车。
- “跟车距离调远一点”：Traffic Manager 设置成功，页面从 2 级 / 5 米变为 3 级 / 7 米。
- “换一条更快的路”：Traffic Manager 接受 `set_path` 调用，当前路线从 `route_a` 变为 `route_b`。一次实时采样的几何 ETA 为 0.4 分钟与 0.1 分钟。该数字不是实际路况 ETA，路径提交成功也不等于逐段行驶轨迹已核验。
- 切换车外视角后返回约 115 KB 的 JPEG。注入接管告警后，再次调整跟车距离得到 `no_change`，网关没有收到写调用。
- `python -m unittest discover -s tests -v`：14 项通过。

## 来源

- [CARLA 0.9.16 Windows 快速入门](https://carla.readthedocs.io/en/0.9.16/start_quickstart/)
- [CARLA Python API：Traffic Manager](https://carla.readthedocs.io/en/0.9.16/python_api/)
- [CARLA 摄像头与传感器](https://carla.readthedocs.io/en/0.9.16/tuto_G_retrieve_data/)
- [CARLA 低画质选项](https://carla.readthedocs.io/en/latest/adv_rendering_options/)
