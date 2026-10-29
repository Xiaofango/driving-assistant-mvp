# Driving Assistant MVP

座舱驾驶助手(子 Agent)的仿真原型:持续感知 CARLA 仿真世界,理解自然语言指令,在分层安全规则内控制车辆与环境,每一步留痕可审计。

> 环境要求:Windows + Python 3.12 + [CARLA 0.9.16](https://carla.readthedocs.io/en/latest/build_system/)。依赖见 `requirements.txt`(同时提供 `requirements-carla.txt`,内容一致)。
> 本仓库只包含代码、测试与文档;`materials/`(原始内部资料)与 `.build/`(审核截图)仅保存在本地,不入库。

**核心特性**

- 实时感知:7 类车载传感器 + 地图真值 + 天气/行人/交通灯,按新世界帧持续采集,数据新鲜度如实标注;
- 受控执行:语音指令直接作用于仿真世界(TM 意图指令 / 暂停后直接步进控制两层权限),全部有网关回执;
- 安全边界:超速/超车/闯红灯/逆行拒止,紧急告警抢占一切写操作(唯一例外是紧急停车),数据过期不执行;
- 可审计可集成:每轮交互保留原话/意图/依据帧/回执/前后状态,`/api/agent-input` 输出带版本号的标准输入。

## 环境安装(从零部署)

按顺序执行一次即可。前置条件:Windows 10/11、[Python 3.12](https://www.python.org/downloads/)、约 15 GB 空闲磁盘(CARLA 约 7.8 GB + 虚拟环境)。显卡:CARLA 官方建议独显 8 GB 显存;核显(如 Radeon 780M)在低画质模式下可运行本项目。

**1. 获取代码**

```powershell
git clone https://github.com/Xiaofango/driving-assistant-mvp.git DA
cd DA
```

**2. 安装 CARLA 0.9.16(服务端)**

从 [CARLA 0.9.16 Windows 版](https://github.com/carla-simulator/carla/releases/tag/0.9.16)下载 `CARLA_0.9.16.zip`,解压到任意目录,默认脚本期望的路径是 `D:\CARLA_MVP\CARLA_0.9.16`(其他路径见第 4 步的参数)。若启动报缺 DLL,安装[DirectX End-User Runtime](https://www.microsoft.com/en-us/download/details.aspx?id=35)。

**3. 创建 Python 虚拟环境并安装依赖**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**4. 验证安装**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests    # 95 项测试应全部通过
.\scripts\start_demo.ps1                                    # 一键启动,浏览器自动打开演示页
```

CARLA 安装在非默认路径时:`.\scripts\start_demo.ps1 -InstallDir <你的CARLA目录>`(分步启动则给 `start_carla_low.ps1` 传同样的参数)。连接远程 CARLA 服务端(如另一台有独显的机器):`.\.venv\Scripts\python.exe -m da_mvp.web_demo --mode carla --carla-host <地址>`。

安装部署遇到问题(端口占用、世界冻结、画面不出、车辆不动)见 [docs/11_运维与故障排查.md](docs/11_运维与故障排查.md)。

## 快速开始

### 方式一:一键启动(CARLA 实时模式,推荐)

```powershell
.\scripts\start_demo.ps1
```

启动本机低画质 CARLA(端口 2000),就绪后启动网页服务并自动打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。需要 CARLA 0.9.16 安装在 `D:\CARLA_MVP\CARLA_0.9.16`(可在脚本里改)。偶发首次连接超时,等几秒重跑一次即可。

### 方式二:分步启动

```powershell
.\scripts\start_carla_low.ps1          # 终端 1:CARLA 服务端(离屏低画质)
.\.venv\Scripts\python.exe -m da_mvp.web_demo --mode carla    # 终端 2:网页服务
```

`Ctrl+C` 停止网页服务,程序会清理自己生成的仿真车辆与传感器。`--carla-host/--carla-port` 可连远端 CARLA;`--town` 换地图;`--osm-map 本地.osm` 用 OpenStreetMap 数据生成自定义路网。

### 方式三:内存模式(无 CARLA,只演示对话逻辑)

```powershell
.\.venv\Scripts\python.exe -m da_mvp.web_demo --mode memory
```

## 语音指令清单

所有指令经意图解析 → 安全检查 → 网关执行 → 回执留痕。不认识的话术会追问而不写入。

| 类别 | 话术示例 | 效果 |
| --- | --- | --- |
| 状态查询 | 为什么停了 / 前车离我多远 / 当前道路和车道 / 我现在开多快 / 最近发生了什么 | 引用当前仿真帧回答,标注数据来源与方法 |
| 感知查询 | 现在天气怎么样 / 附近有行人吗 / 最近障碍物多远 / 红灯还有多久变绿 / 我们现在在哪 | 多传感器/真值融合,来源分开表述 |
| 驾驶指令(意图层) | 开到 40 / 开慢点 / 加速 / 恢复限速 / 向左变道 / 向右变道 / 暂停自动驾驶 / 恢复自动驾驶 | 由 Traffic Manager 执行,遵守交通规则;超速目标直接拒止 |
| 路线与目的地 | 换一条更快的路 / 走备选路线 / 回到原始路线 / 去东区 / 去南区 / 换个目的地 | 提交新路径并如实标注"轨迹未验证" |
| 环境与场景 | 下雨 / 起雾 / 天黑 / 晴天 / 模拟红灯场景 / 前面放一辆事故车 / 模拟行人横穿马路 / 清除场景 | 改变仿真天气或注入演示场景,TM 交通自主反应 |
| 行程录制 | 开始录制 / 停止录制 | 文件落 `tmp/recordings/`(不含传感器附加数据) |
| 紧急动作 | 紧急停车 / 立刻停车 | 断开 TM + 满刹 + 手刹;唯一不依赖数据新鲜度、不被告警抢占的写操作 |
| 直接控制(第二层) | 踩油门 / 松油门 / 刹车 / 左打方向 / 右打方向 / 回正方向 | 仅在 TM 暂停后开放;TM 驾驶中被分层权限拒绝 |
| 拒止项 | 帮我超车 / 闯红灯 / 逆行 | 无条件拒绝并说明原因;模糊的"帮我变道"会追问方向 |

## HTTP API

| 接口 | 说明 |
| --- | --- |
| `GET /api/state` | 页面全量状态(快照 + 交互历史 + 网关调用记录) |
| `GET /api/agent-input` | 带版本号的只读输入 `{"schema_version":"1.1","snapshot":{...}}`,供上层系统读取 |
| `GET /api/frame` | 前视/车外摄像头 JPEG(过期返回 503) |
| `GET /api/telemetry-stream` | 持续采样统计 + 最近 30 条输入 + 健康状态 |
| `GET /api/telemetry-export.jsonl` | 导出最近 300 条真实采样(JSONL) |
| `POST /api/command` | 对话指令 `{"utterance":"..."}` |
| `POST /api/reset` `/api/event` `/api/clear-event` `/api/noa` `/api/camera-mode` | 操作台:重置 / 注入接管告警 / 清除告警 / TM 开关 / 摄像头视角 |
| `POST /api/weather` `/api/recorder` `/api/scenario` `/api/sync-mode` | 操作台:天气 / 录制 / 场景注入 / 同步模式(20 Hz) |

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

95 项测试覆盖:意图解析、安全门(过期/告警/越界)、网关回执链、分层权限、内存模式拒止、Web 传输与采样缓存。

## 项目结构

```
DA/
├── da_mvp/                 # 主包
│   ├── carla_gateway.py    # CARLA 网关主类:连接/生命周期/采集循环/快照(组合各 Mixin)
│   ├── carla_scene.py      # 场景生成:演示车/路线/NPC/行人/骑行者
│   ├── carla_sensors.py    # 传感器套件:摄像头/事件传感器/扩展感知及全部回调
│   ├── carla_actions.py    # 写操作:偏好/路线/天气/录制/场景/驾驶指令/同步模式
│   ├── carla_config.py     # CARLA 相关常量与天气预设映射
│   ├── gateway.py          # Gateway 协议 + 内存实现
│   ├── service.py          # DA 服务:意图分发/安全检查/回执处理/回复生成
│   ├── intent.py           # 受限自然语言意图解析
│   ├── safety.py           # 新鲜度/权限/路线安全检查
│   ├── telemetry.py        # 采样缓存/健康度/动作解释(纯计算,与 CARLA 解耦)
│   ├── web_demo.py         # 本机 HTTP 服务
│   └── web/                # 单页前端(index.html/app.js/style.css)
├── tests/                  # 95 项单元测试
├── scripts/                # start_demo.ps1 / start_carla_low.ps1
├── docs/                   # 说明文档(见 docs/README.md 导航)
├── materials/source/       # 原始需求材料
├── slides/                 # 演示稿
├── .build/                 # 历史审核证据截图(docs 06/07 引用)
└── tmp/recordings/         # 行程录制输出(按需生成)
```

## 文档与排障

完整文档见 [docs/README.md](docs/README.md)(按主题分组,含推荐阅读顺序)。演示环境异常(页面不响应/世界冻结/车不动)请先看 [docs/11_运维与故障排查.md](docs/11_运维与故障排查.md)。

代码通过 `Gateway` 协议隔离执行侧。CARLA 模式仅操作仿真车辆;PC/MQTT/NeMo 的正式集成仍需公共平台提供契约。
