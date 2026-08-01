---
status: approved-baseline
version: 1.4
updated: 2026-08-01
scope: davinci-engine-mcp
---

# davinci-engine-mcp 设计

## 1. 定位

`davinci-engine-mcp` 是本项目自研的本地确定性媒体与 DaVinci Resolve 执行引擎。

它统一提供：

- Windows Resolve Bootstrap，以及项目、媒体池、时间线、轨道、片段、Fusion、调色、音频、播放和渲染控制；
- Beat、Onset、音乐裁切、响度、静音和基础 Mix 等确定性媒体分析；
- 已验证的 LUT、字体、Fusion 效果、静态重构图和媒体素材适配；
- 执行计划校验、幂等、读回、对账、能力认证和真实渲染验证。

该 MCP 只承担确定性分析与执行，不拥有项目业务状态，也不作专业剪辑和审美决定。

## 2. 责任

### 2.1 确定性媒体分析

- ffprobe 技术探测；
- 代理和抽帧支持；
- 镜头检测；
- BPM、Beat、Onset 和能量；
- LUFS、True Peak 和 Loudness Range；
- 静音和对白压缩候选；
- 音乐裁切候选。

FunASR 和云端多模态模型不运行在此 MCP 内。

### 2.2 Resolve 执行

- Windows Resolve 连接；
- 项目、媒体池、时间线和轨道；
- 精确源范围和目标时间放置；
- 片段属性和静态/动态重构；
- Fusion、标题、转场和动态图形适配；
- LUT、调色节点和已认证 Look；
- 音频放置、音量和已认证处理链；
- 渲染、状态查询和结果读回。

### 2.3 创意素材适配

- 识别本机已部署能力；
- 部署经过批准的 LUT、字体和 Fusion 资源；
- 通过能力 Adapter 应用具体资源；
- 验证依赖、版本、输入端口、轨道和画幅；
- 真实渲染认证。

### 2.4 写入安全

- 计划校验；
- dry run/preview；
- 确认摘要；
- operation id 幂等；
- 单写者；
- 写后读回；
- outcome unknown 对账；
- 实际渲染验证。

## 3. 非职责

`davinci-engine-mcp` 不负责：

- 用户目标、项目简报和业务状态；
- Codex Thread；
- 专业素材解释；
- 自动决定选哪些片段；
- 自动判断何时使用音乐、字幕和动效；
- 用户反馈诊断；
- 生成用户可见版本记录；
- 声称审美或用户满意；
- 任意脚本执行入口；
- 自动批量安装未知 EXE、DLL、OFX 或插件。

不使用基于文件轮换和节拍吸附的自动选片作为产品默认逻辑，也不暴露任意 `execute_lua`。

## 4. MCP 客户边界

唯一正常客户端是 Product Workflow Worker。

```text
Codex + Skills
  → Product Application
  → Validator / Compiler
  → Workflow Worker
  → davinci-engine-mcp
  → DaVinci Resolve
```

Web、Codex 和 Skills 不直接调用此 MCP。

### 4.1 创意资源输入边界

`davinci-engine-mcp` 不扫描整个 Nextcloud 采购库，也不负责向量检索。Creative 模块先完成目录检索、候选选择、本地化和内容哈希校验，再把确切本地缓存对象写入 CapabilityBinding。

Engine 只接受：

- 本地真实文件路径；
- 确切内容哈希；
- 已认证能力身份和约束；
- 已批准的 ResolveExecutionPlan。

在线占位、同步中、未校验或仅有云端引用的文件必须拒绝执行。

## 5. 进程与 Windows 运行时

- 首期统一使用项目的 Python 3.10.20 Conda 环境；
- API、Worker 与 Engine 共用依赖，但 `davinci-engine-mcp` 作为 Worker 启动的独立 stdio 子进程运行；
- 不开放额外 HTTP 端口，不要求用户单独部署服务；
- 通过 Windows Launcher 固定 `FUSION_PYTHON3_HOME`、Resolve Scripting 路径和 DLL 搜索顺序；
- 启动时在隔离子进程中探测 `DaVinciResolveScript` 和 `fusionscript.dll`；
- 原生模块探测失败不能使 API 和业务数据库进程崩溃；
- Resolve 未启动时，分析类工具仍可使用，Resolve 工具返回 disconnected；
- Resolve 重启后允许刷新连接，不要求重启 Product Application；
- 只有出现经过复现且无法解决的依赖冲突时，才拆分 Python 环境。

Product Application 和 API 进程不直接导入 Resolve 原生模块。

## 6. 内部模块

```text
davinci-engine-mcp/
├── src/davinci_engine/
│   ├── contracts/
│   ├── analysis/
│   │   ├── media_probe.py
│   │   ├── scene_detector.py
│   │   ├── frame_extractor.py
│   │   ├── beat_analyzer.py
│   │   ├── loudness_analyzer.py
│   │   ├── silence_analyzer.py
│   │   └── music_cut_analyzer.py
│   ├── resolve/
│   │   ├── bootstrap.py
│   │   ├── connection.py
│   │   ├── project.py
│   │   ├── media_pool.py
│   │   ├── timeline.py
│   │   ├── tracks.py
│   │   ├── clips.py
│   │   ├── fusion.py
│   │   ├── color.py
│   │   ├── audio.py
│   │   └── render.py
│   ├── creative/
│   │   ├── inventory.py
│   │   ├── registry.py
│   │   └── adapters/
│   ├── execution/
│   │   ├── validator.py
│   │   ├── preview.py
│   │   ├── executor.py
│   │   ├── journal.py
│   │   ├── reconciler.py
│   │   └── verifier.py
│   ├── mcp/
│   │   ├── server.py
│   │   ├── tools.py
│   │   └── resources.py
│   └── bootstrap.py
└── tests/
```

内部 Resolve API 可以保持细粒度，但 MCP 对外表面使用高层组合工具。

## 7. MCP 工具表面

首期只暴露必要工具：

| 工具 | 作用 | 是否修改 Resolve |
|---|---|---|
| `engine_status` | 检查 Resolve、FFmpeg、分析模块和能力库存 | 否 |
| `analyze_media` | 技术探测、镜头、抽帧、节拍、响度、静音等确定性分析 | 否 |
| `inspect_resolve` | 读取项目、时间线、轨道、片段、Fusion、调色和渲染状态 | 否 |
| `list_installed_capabilities` | 读取本机已部署素材、模板和插件能力 | 否 |
| `validate_execution_plan` | 校验计划、资源、轨道、时间和机器能力 | 否 |
| `preview_execution_plan` | 返回将发生的变化和不可执行项 | 否 |
| `execute_execution_plan` | 执行已经批准且摘要未变化的计划 | 是 |
| `reconcile_operation` | 对账结果未知的写操作 | 否 |
| `render_version` | 提交指定工作时间线的渲染 | 是 |
| `inspect_render` | 查看渲染任务和文件 | 否 |
| `verify_render` | 检查时长、声画、黑帧、离线媒体、响度等 | 否 |

创意资源上架和批量部署属于管理员/开发者 CLI 或受限管理接口，不暴露给普通 Codex 工作流。`list_installed_capabilities` 只返回当前工作站已部署库存，不承担完整云端素材目录检索。

## 8. 为什么不暴露全部原子工具

如果 Codex 逐个调用 `add_track`、`append_clip`、`apply_lut` 和 `create_fusion_node`：

- 步骤过多；
- 中途失败难以恢复；
- 模型可能遗漏关键操作；
- 产品校验容易被绕过；
- 任意 Lua 带来高风险；
- 版本和写入安全无法集中保证。

因此上游提交 ResolveExecutionPlan，由 MCP 内部把它展开为细粒度 Resolve 调用。

## 9. ResolveExecutionPlan 语义

执行计划必须足够精确地描述：

- 目标项目和工作时间线；
- 需要创建或复用的轨道；
- 源素材和源入出范围；
- 目标轨道、时间、时长和层级；
- 片段属性；
- 音频放置和层级；
- 认证能力及其参数；
- 调色和收尾操作；
- 渲染配置；
- 每项操作的预期读回状态。

精确字段在代码 Schema 中定义，不在本文维护完整 JSON。

## 10. 两阶段执行

### 10.1 校验

`validate_execution_plan` 检查：

- 当前 Resolve 连接；
- 项目和工作时间线；
- 源素材存在和范围合法；
- 目标轨道和时间合法；
- 能力已经认证且本机已部署；
- 用户可见基线不会被修改；
- 计划没有未支持操作；
- 渲染目标可写。

### 10.2 预览

`preview_execution_plan` 返回：

- 计划摘要；
- 将创建和修改的对象；
- 冲突和警告；
- 计划摘要哈希。

### 10.3 执行

`execute_execution_plan` 必须携带：

- 稳定 operation id；
- 已校验计划身份；
- 未变化的摘要哈希；
- 产品应用签发的执行许可；
- 预期项目和时间线。

如果任何前置事实变化，拒绝执行并要求重新预览。

## 11. 统一结果语义

所有工具使用统一结果外层，核心状态只有：

- `succeeded`；
- `failed`；
- `outcome_unknown`。

`outcome_unknown` 只用于写操作可能已经发生但响应或读回不确定的情况。

普通分析失败、能力不存在和参数非法返回 `failed`，并提供结构化原因。

## 12. 幂等与 Operation Journal

Engine 保存一个小型本地执行 Journal，用于基础设施级幂等和对账。它不是项目业务数据库。

每个写操作关联：

- operation id；
- 计划身份；
- 预期效果；
- 实际请求；
- 发送和结束状态；
- 读回；
- 验证结果。

重复收到相同 operation id 时：

- 已成功：返回原成功结果；
- 正在执行：返回进行中/不可重复；
- 结果未知：要求对账；
- 已明确失败且未产生副作用：允许由上游决定重新提交新尝试。

## 13. 写后读回

每类操作定义独立 Readback Contract。例如：

- 创建时间线：按稳定名称和标记查询；
- 插入片段：查询目标轨道、开始帧、时长和媒体身份；
- 应用 LUT：读取目标节点或效果状态；
- 挂载 Fusion：检查节点和连接；
- 设置重构图：读取 Zoom/Pan/Tilt/Rotation；
- 提交渲染：查询渲染队列和输出文件。

MCP 响应成功但实际对象不符合预期，整体结果仍为失败。

## 14. 结果未知对账

`reconcile_operation` 只读执行：

1. 读取 Journal 中的预期效果；
2. 刷新 Resolve 连接和对象；
3. 查询目标对象；
4. 分类为“已经发生”“没有发生”“仍无法确认”；
5. 返回证据。

只有明确证明没有发生时，上游才能重新执行。

## 15. 创意能力 Adapter

统一接口概念：

```text
probe
install_or_deploy(local_cached_asset)
validate
apply
inspect
verify_render
```

`local_cached_asset` 必须由上游 Creative 模块完成本地化和哈希验证。Adapter 不直接从 Nextcloud 下载未知资源。

建议适配器：

- `AudioAssetAdapter`；
- `VideoOverlayAdapter`；
- `LutAdapter`；
- `FontAdapter`；
- `FusionEffectAdapter`；
- `FusionTitleAdapter`；
- `TransitionAdapter`；
- `MotionRecipeAdapter`；
- `OpenFxAdapter`；
- `AudioPluginChainAdapter`。

每个适配器只处理一种明确机制，不使用一个“运镜模板”类别混合 Transform、Fusion 和 OpenFX。

## 16. 能力认证

### 16.1 状态

- `testing`：正在测试，不进入项目执行；
- `certified`：可以自动执行，但仍受明确约束；
- `manual_only`：可人工使用，不进入无人自动链路；
- `unsupported`：不使用。

运行时只使用 `certified`。

### 16.2 五步合同

1. 可发现；
2. 可部署；
3. 可执行；
4. 可读回；
5. 可真实渲染。

### 16.3 初始实测基线

已经证明可通过适配器进入自动链路的类别：

- MP3/WAV 音乐和音效；
- MP4/MOV/PNG 直接媒体；
- `.cube` LUT；
- 明确字重和字符集的字体；
- 单输入、单输出 Fusion 片段效果；
- 静态缩放、平移、倾斜和旋转重构图。

仍需逐类认证：

- 通用标题和生成器；
- 双输入转场；
- 透明多轨合成的全部边界；
- 关键帧运镜和主体跟踪；
- `.drp/.drt/.drfx` 项目级资源；
- OpenFX、VST 和需要自定义界面的插件。

单个能力测试成功不能泛化到同目录所有资源。

## 17. 项目风格套件

Engine 不选择风格，但执行计划只能引用 Creative 模块已经绑定的有限风格套件，例如：

- 少量标题层级；
- 允许的运镜 Recipe；
- 允许的转场类别；
- 音效强度和类别；
- 调色方向；
- 禁止的过度效果。

Engine 验证绑定，不根据模板数量随机选择。

## 18. 渲染与验证

### 18.1 渲染

- 内部工作版和用户候选使用不同输出身份；
- 输出目录和文件名由产品计划决定；
- 渲染任务必须可查询；
- 文件存在不等于渲染完整。

### 18.2 技术验证

至少检查：

- 文件可解码；
- 时长与计划容差；
- 音频存在；
- 黑帧/长静音/离线媒体；
- 画幅和分辨率；
- 响度和峰值（存在交付标准时）；
- 关键能力实际出现在渲染中；
- 用户版本文件不被覆盖。

审美问题不转成统一硬分数。

## 19. 安全

- 不暴露任意 Lua 工具；
- 内部 Lua/Fusion 脚本只能来自版本控制中的可信实现；
- 未知可执行文件和插件不自动安装；
- 管理员部署使用白名单目录；
- 所有写工具默认拒绝直接自然语言请求；
- MCP 只接受结构化、已校验的计划；
- 文件路径限制在允许根目录；
- 渲染和部署路径做规范化与越界检查。


## 20. 测试

### 单元测试

分析算法、计划校验、路径安全、摘要哈希、幂等和 Adapter 逻辑。

### 合同测试

所有 Adapter 和 Port 使用同一测试套件。

### Resolve Live 测试

在隔离项目验证项目、时间线、轨道、片段、Fusion、LUT、字体、重构图和渲染。

### Golden Render

为每种认证能力保存标准输入和预期结构证据，比较：

- 是否出现；
- 时间是否正确；
- 透明和轨道是否正确；
- 声音是否存在；
- 重启 Resolve 后结果是否保留。

### 故障测试

在发送写请求、收到响应和读回各阶段模拟断连，验证不会重复写入。
