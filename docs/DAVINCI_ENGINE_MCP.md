---
status: approved-baseline
version: 1.6
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

## 2. 实现参考与能力迁移

本工程目录不包含两个参考 MCP 的源码。开发 `davinci-engine-mcp` 前，必须先取得参考仓库并按本节定位代码；不能只根据功能名称重新猜测实现，也不能把两个 MCP 原样拼接进产品。

### 2.1 参考仓库

| 用途 | 仓库 | 在本项目中的定位 |
|---|---|---|
| Windows Resolve 连接与细粒度控制参考 | [Tooflex/davinci-resolve-mcp](https://github.com/Tooflex/davinci-resolve-mcp) | 参考 Windows Bootstrap、Resolve 项目/媒体池/时间线/轨道/片段/Fusion/调色/音频/播放/渲染的调用方式 |
| 确定性媒体分析与写前预览参考 | [wassermanproductions/unofficial-davinci-mcp](https://github.com/wassermanproductions/unofficial-davinci-mcp) | 参考 Beat、Onset、响度、静音、音乐裁切、结构化片段放置及 `dry_run + confirm` 模式 |

这两个仓库是**开发时的实现参考**，不是产品运行时的两个依赖服务。最终产品只能运行本项目自己的 `davinci-engine-mcp`，Resolve 也只能有一个写入入口。

### 2.2 在新工程中取得参考代码

在工程根目录执行以下命令。参考代码统一放进已经被 Git 忽略的 `workspace/upstream-reference/`，不放进正式源码目录，也不创建 Git Submodule。

```powershell
conda activate unofficial-davinci-mcp-win

$referenceRoot = Join-Path $PWD "workspace\upstream-reference"
New-Item -ItemType Directory -Force $referenceRoot | Out-Null

git clone --depth 1 `
  https://github.com/Tooflex/davinci-resolve-mcp.git `
  "$referenceRoot\tooflex-davinci-resolve-mcp"

git clone --depth 1 `
  https://github.com/wassermanproductions/unofficial-davinci-mcp.git `
  "$referenceRoot\wasserman-unofficial-davinci-mcp"

git -C "$referenceRoot\tooflex-davinci-resolve-mcp" rev-parse HEAD
git -C "$referenceRoot\wasserman-unofficial-davinci-mcp" rev-parse HEAD
```

要求：

- 只使用现有 Conda 环境 `unofficial-davinci-mcp-win`，不得因此创建新环境；
- 不执行两个参考仓库 README 中创建 `.venv` 或新 Conda 环境的命令；
- 不需要把两个参考 MCP 安装为产品运行依赖；
- 不直接从产品代码 `import` 参考仓库模块；
- 开发记录中保存实际参考的 commit SHA，后续升级参考代码时必须重新评审和运行合同测试；
- 不对 `workspace/upstream-reference/` 中的代码做产品改动，迁移后的实现写入本项目正式模块。

### 2.3 迁移总原则

1. **先定义本项目合同，再迁移实现。** 先建立 `ResolveExecutionPlan`、统一结果外层、Readback Contract 和分析证据合同，再把参考行为放入对应模块。
2. **迁移能力，不合并服务器。** 不启动两个上游 MCP，不建立工具转发层，不保留两套 Resolve 连接和两套错误语义。
3. **只有一个 Resolve 连接所有者。** `davinci_engine.resolve.connection` 管理连接；项目、时间线、Fusion、调色和渲染模块复用同一连接上下文。
4. **内部可以细粒度，对外必须高层。** 原子 Resolve 方法保留在内部 Python API，MCP 对外只暴露第 8 节定义的组合工具。
5. **确定性分析只提供证据。** Beat、Onset、静音、响度和音乐裁切结果不能自动升级为选片、切点、删停顿或混音审美决定。
6. **每项能力单独迁移和验收。** 未完成读回与真实渲染测试的能力，不得因参考仓库存在对应函数就声明支持。
7. **上游更新不自动进入产品。** 只能在明确需要时人工比较差异，避免一次 `git pull` 改变已验证行为。

### 2.4 Tooflex：重点参考文件与迁移位置

#### 2.4.1 Windows Bootstrap

重点文件：

- [`resolve_env.py`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/resolve_env.py)
- [`run_server.ps1`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/run_server.ps1)
- [`tests/test_resolve_env.py`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/tests/test_resolve_env.py)

快速定位这些符号：

| 文件 | 重点符号 | 迁移目的 |
|---|---|---|
| `resolve_env.py` | `_resolve_program_dir()`、`_scripting_dir()`、`_script_lib()` | 找到 Resolve 程序、Developer/Scripting 和原生库 |
| `resolve_env.py` | `_set_env_defaults()`、`_preload_matching_python_dll()` | 准备环境变量并预载匹配的 Python Runtime |
| `resolve_env.py` | `scripting_safe_to_import()` | 用一次性子进程隔离 `fusionscript.dll` 的不可捕获崩溃 |
| `resolve_api.py` | `ResolveAPI._find_scripting_module()`、`ResolveAPI._connect_to_resolve()`、`ResolveAPI.refresh()` | 发现模块、连接 Resolve、刷新失效对象 |
| `server.py` | `import resolve_env` 早于 `resolve_api`、`FastMCP` stdio 初始化 | 只参考安全启动顺序，不复制原始工具表面 |

重点参考：

- Resolve Scripting Modules 与 `fusionscript.dll` 的发现；
- `FUSION_PYTHON3_HOME`、`RESOLVE_SCRIPT_*`、`PYTHONPATH` 和 DLL 搜索顺序；
- 在一次性子进程中探测原生模块，避免错误 DLL 让主进程硬崩溃；
- Resolve 未启动或连接失败时，MCP 进程仍保持可用；
- Windows 启动脚本对环境变量和 `PATH` 的隔离方式。

迁入本项目：

```text
davinci-engine-mcp/src/davinci_engine/resolve/bootstrap.py
davinci-engine-mcp/src/davinci_engine/resolve/connection.py
scripts/start_davinci_engine.ps1
```

迁移时必须修改：

- 适配当前已有 Conda 环境 `unofficial-davinci-mcp-win`，不能照搬上游创建 `.venv` 的安装流程；
- 使用当前进程实际 `sys.executable`、`CONDA_PREFIX`、`sys.prefix` 和匹配的 Python DLL 做探测，不假设 python.org 安装路径，也不得机械照抄上游的 `sys.base_prefix`；必须验证 `FUSION_PYTHON3_HOME` 指向当前 `unofficial-davinci-mcp-win` 环境对应的 Runtime，而不是 Anaconda `base` 或其他 Python；
- DLL 搜索路径只加入当前 Conda 环境、其 `Library\bin`、Resolve 程序目录和系统必需目录，不把其他 Python 安装目录混入 Engine 的 `PATH`；
- Bootstrap 只负责配置和安全探测，不能在导入模块时隐式创建项目或写入 Resolve；
- 探测结果返回结构化状态，不用 `print` 字符串作为业务判断依据。

#### 2.4.2 Resolve 细粒度控制

重点文件：

- [`resolve_api.py`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/resolve_api.py)
- [`server.py`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/server.py)
- [`tests/test_api_contract.py`](https://github.com/Tooflex/davinci-resolve-mcp/blob/main/tests/test_api_contract.py)

快速定位这些方法：

```text
ResolveAPI._connect_to_resolve() / refresh()
ResolveAPI.get_current_project() / get_current_timeline()
ResolveAPI.append_to_timeline() / create_timeline_from_clips()
ResolveAPI.get_timeline_items() / set_clip_property()
ResolveAPI.create_fusion_node() / add_color_node()
ResolveAPI.add_track() / set_track_name() / enable_track()
ResolveAPI.set_audio_volume() / set_track_volume()
ResolveAPI.start_render() / get_render_status()
```

这些方法只用于定位 Scripting API 调用。上游的 `append_to_timeline()` 主要返回布尔值，`start_render()` 直接启动渲染；本项目必须补上确切素材身份、源/目标帧、Render Job 身份、计划摘要、写后读回和文件级验证。

`resolve_api.py` 用于理解 Resolve Scripting API 的对象获取和调用顺序，但不要把一个大型 `ResolveAPI` 类原样搬进来。应拆到：

| 参考能力 | 本项目目标模块 | 迁移要求 |
|---|---|---|
| Resolve/ProjectManager 连接 | `resolve/connection.py` | 统一连接生命周期、断线刷新和明确 disconnected 错误 |
| 项目创建、打开、保存和设置 | `resolve/project.py` | 每次写入校验预期项目，不依赖“当前项目”猜测 |
| 媒体池、目录和媒体导入 | `resolve/media_pool.py` | 用内容哈希和规范化本地路径识别素材，不只用文件名 |
| 时间线创建、切换和查询 | `resolve/timeline.py` | 工作时间线使用稳定身份，禁止覆盖用户可见基线 |
| 视频/音频/字幕轨道 | `resolve/tracks.py` | 显式轨道类型、索引、名称和读回合同 |
| 片段插入和属性 | `resolve/clips.py` | 精确源范围、目标帧、轨道和媒体类型；写后查询实际片段 |
| Fusion 节点和合成 | `resolve/fusion.py` | 只暴露受信任的高层 Adapter，不提供任意 Lua |
| Color 节点、LUT 和 Still | `resolve/color.py` | 绑定确切节点并读取应用结果 |
| 片段/轨道音量 | `resolve/audio.py` | 区分技术参数和上游声音设计决定 |
| 播放控制 | `resolve/playback.py` | 仅用于调试、人工复核和受控定位，不作为完成证据 |
| 渲染和队列 | `resolve/render.py` | 渲染任务必须有 job 身份并可查询，输出文件必须二次验证 |

`server.py` 只参考以下部分：

- FastMCP 的 stdio 启动方式；
- 工具与只读资源的注册形式；
- 原子方法怎样调用 Resolve API。

不得迁移：

- 把所有原子工具直接暴露给 Codex；
- 以自然语言字符串作为统一返回合同；
- 任意 `execute_lua`；
- 仅凭片段名称进行关键业务匹配；
- 在模块 import 时创建全局 `ResolveAPI()` 或建立连接；连接必须由 Engine 生命周期显式创建、刷新和关闭；
- 把上游 Server 当作本项目的业务状态所有者。

`test_api_contract.py` 的思路需要迁入合同测试：MCP 工具引用的内部方法必须真实存在，参数语义一致；但测试目标改成本项目自己的 Port、Schema 和模块，而不是复制上游工具清单。

### 2.5 Wasserman：重点参考文件与迁移位置

#### 2.5.1 FFmpeg 基础与确定性分析

重点文件：

- [`engines/fftools.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/fftools.py)
- [`engines/beat_grid.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/beat_grid.py)
- [`engines/loudness.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/loudness.py)
- [`engines/dead_air.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/dead_air.py)
- [`engines/music_cut.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/music_cut.py)

快速定位这些符号：

| 文件 | 重点符号 | 迁移用途 |
|---|---|---|
| `fftools.py` | `ffmpeg_path()`、`ffprobe_path()`、`run()`、`decode_pcm_mono()`、`decode_pcm()`、`encode_wav()`、`ffprobe_json()` | 统一二进制发现、子进程执行、PCM 解码和探测 |
| `beat_grid.py` | `beat_grid()` | BPM、Beat、Onset、强度、方法和近似标记 |
| `loudness.py` | `measure_one()`、`measure()`、`detect_speech_windows()`、`mix_plan()` | loudnorm 测量、语音窗口、增益和 Ducking 候选 |
| `dead_air.py` | `tighten_dialogue()` | 静音区间、保留 handles 和 keep/remove 候选 |
| `music_cut.py` | `cut_music()` | 音乐尾奏、拼接和淡出候选及降级路径 |

迁移映射：

| 参考文件 | 本项目目标模块 | 保留 | 必须改变 |
|---|---|---|---|
| `fftools.py` | `analysis/ffmpeg_runtime.py` | FFmpeg 路径发现、PCM 单声道解码和子进程调用思路 | 统一超时、取消、错误结构和允许路径；不让每个分析器各自拼命令 |
| `beat_grid.py` | `analysis/beat_analyzer.py` | BPM、Beat、Onset、强度及 librosa/能量包络降级 | 输出必须保留 `method`、`approximate` 和警告；Downbeat 只作为猜测证据 |
| `loudness.py` | `analysis/loudness_analyzer.py` | LUFS、LRA、True Peak 测量和基础 Mix 参数计算 | 测量与审美方案分开；默认参数不能直接变成最终混音决定 |
| `dead_air.py` | `analysis/silence_analyzer.py` | `silencedetect`、噪声门限和停顿范围候选 | 只返回候选范围与上下文，不自动删停顿、不直接改时间线 |
| `music_cut.py` | `analysis/music_cut_analyzer.py` | 靠近目标时长的节拍/尾奏候选和淡出参数 | 只生成多个候选及代价，由声音 Skill 选择，不自动写最终音乐剪点 |

所有分析结果必须带：源内容身份、源时间基准、分析器版本、实际方法、是否近似、警告和可回链时间范围。精确字段由代码 Schema 定义。

对应测试可用于理解边界和构造 fixture，但必须改写为本项目合同：

- [`tests/test_beat_grid.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/tests/test_beat_grid.py)
- [`tests/test_loudness.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/tests/test_loudness.py)
- [`tests/test_dead_air.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/tests/test_dead_air.py)
- [`tests/test_music_cut.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/tests/test_music_cut.py)

不得直接复制测试名称和输出结构，让上游测试反向定义本项目产品边界。

#### 2.5.2 结构化 Resolve 写入与预览

重点文件：

- [`davinci_mcp/tools_live.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/davinci_mcp/tools_live.py)
- [`davinci_mcp/resolve_api.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/davinci_mcp/resolve_api.py)
- [`davinci_mcp/registry.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/davinci_mcp/registry.py)
- [`davinci_mcp/server.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/davinci_mcp/server.py)
- [`tests/test_tools_live.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/tests/test_tools_live.py)

快速定位这些符号：

| 文件 | 重点符号 | 本项目处理方式 |
|---|---|---|
| `tools_live.py` | `_guard_mutation()` | 保留“dry-run 后显式确认”的思想，再扩展为计划摘要、operation id 和执行许可 |
| `tools_live.py` | `_normalize_clip_plan()` | 参考路径规范化、未知字段拒绝、秒到帧的显式换算 |
| `tools_live.py` | `_append_payload()` | 参考 `startFrame`、`endFrame`、`recordFrame`、`trackIndex`、`mediaType` 的 Resolve payload |
| `registry.py` | `_DRY_RUN`、`_CONFIRM`、`_CLIP_PLAN` | 只参考语义，最终使用本项目 Pydantic/JSON Schema |
| `server.py` | `_validate_arguments()` 和 stdio JSON-RPC 循环 | 不迁移；该校验是浅层的，本项目使用正式 MCP/FastMCP 与本项目 Schema |

`davinci_mcp/resolve_api.py` 只用于比较 Live 对象访问和错误表达，不作为 Windows 主连接来源；Windows 连接统一由本项目 `resolve/bootstrap.py` 和 `resolve/connection.py` 管理。

`tools_live.py` 重点参考：

- 写操作先形成可检查的 plan；
- `dry_run` 与显式 `confirm` 的保护；
- 片段计划的规范化与范围校验；
- `startFrame`、`endFrame`、`recordFrame`、`trackIndex` 和 `mediaType` 的 Resolve payload；
- 通过实际文件路径匹配媒体池项目；
- LUT 部署、刷新列表、按轨道/片段/节点应用；
- 渲染参数先展开为计划，再提交 Resolve。

本项目不能原样保留 `dry_run + confirm` 作为唯一安全措施，而要升级为：

```text
validate_execution_plan
→ preview_execution_plan
→ plan_digest
→ 产品应用签发执行许可
→ execute_execution_plan(operation_id)
→ readback
→ verify
→ 必要时 reconcile_operation
```

`registry.py` 只用于理解工具 Schema 的语义组织。`tools_compound.py` 不是迁移目标：跨步骤组合和任务编排属于 Product Workflow，不能再在 Engine 内形成第二套工作流。最终工具名、Schema、错误语义和执行顺序均以本项目合同为准。

### 2.6 需要重新定义的核心合同

#### 2.6.1 精确片段放置

上游的文件名或路径级输入不能直接成为产品合同。本项目内部的单个放置操作至少要表达以下语义：

- `asset_id` 与内容哈希；
- 已本地化并通过校验的真实文件路径；
- 源入点和源出点，统一换算为可靠源帧；
- 目标轨道类型和索引；
- 目标 `record_frame`；
- 视频、音频或声画联合媒体类型；
- 预期时长、预期媒体身份和预期读回位置。

具体字段名放在代码 Schema，不在本文维护完整 JSON。编译器必须完成秒、时间码、代理时间和源帧之间的换算，Engine 不从模糊自然语言猜测。

#### 2.6.2 确定性分析证据

分析结果不能只返回一组数字，至少要同时表明：

- 分析的内容哈希和时间范围；
- 使用的算法、依赖和版本；
- 精确或近似模式；
- 时间基准；
- 候选结果与置信/警告；
- 原始测量和可供上游查询的证据引用。

#### 2.6.3 写操作与结果

每个 Resolve 写操作必须关联：

- `operation_id`；
- ResolveExecutionPlan 与摘要哈希；
- 预期项目、时间线和版本；
- 预期副作用；
- 实际请求、响应、读回和验证；
- `succeeded / failed / outcome_unknown` 之一。

不得沿用不同工具各自返回任意字典或字符串的方式。

### 2.7 明确不迁移的能力

以下内容即使参考仓库已经实现，也不进入首期产品：

- 两个上游 MCP Server 本体及其完整工具列表；
- `davinci_mcp/tools_compound.py` 中的一键工作流；跨步骤编排由 Product Workflow 负责；
- 两套 Resolve 连接、两套缓存对象或工具转发；
- [`engines/auto_edit.py`](https://github.com/wassermanproductions/unofficial-davinci-mcp/blob/main/engines/auto_edit.py) 的文件轮换、节拍吸附式自动选片；
- 自动删除静音、自动决定最终音乐切点和自动生成最终 Mix；
- 上游 Whisper/语音桥作为中文转写真相；
- FCPXML/EDL 免费版路线，除非当前产品范围以后明确要求；
- 自动字幕、自动调色或编辑知识工具替代本项目 Skills；
- 任意 Lua、任意插件安装和未知脚本执行；
- 根据“当前项目/当前时间线/当前选中片段”静默写入；
- 只返回字符串 success、却没有真实读回和渲染证据的实现。

### 2.8 能力迁移顺序

| 顺序 | 迁移内容 | 最小验收 |
|---|---|---|
| 1 | 克隆参考仓库并记录 commit | 两个仓库位于 `workspace/upstream-reference/`；正式源码不依赖其 Python 包 |
| 2 | Windows Bootstrap 与连接 | 在现有 Conda 环境中安全探测；Resolve 未开时不拖垮主程序；打开后可刷新连接 |
| 3 | 只读项目与时间线检查 | 能读取确切项目、时间线、轨道、片段和渲染状态 |
| 4 | 精确媒体导入与片段放置 | 根据内容身份、源帧、目标轨道和 `record_frame` 写入并读回一致 |
| 5 | 工作时间线与渲染 | 新建隔离时间线、渲染有声文件并完成文件级验证 |
| 6 | 写入安全 | validate、preview、摘要、operation id、单写者、读回和对账全部通过故障测试 |
| 7 | Beat/Onset/响度/静音/音乐候选 | 对标准音频输出可回链结果，近似模式不被伪装成精确事实 |
| 8 | 已实测创意素材 Adapter | 每项通过发现、部署、执行、读回和真实渲染五步合同 |

不得跳过第 2～6 步，直接迁入大量分析工具或创意模板。

### 2.9 迁移完成的判定

单个能力只有同时满足以下条件才算迁移完成：

1. 已写入本项目自己的模块，不依赖运行参考 MCP；
2. 输入输出符合本项目代码 Schema；
3. 失败返回结构化原因；
4. 写操作具有幂等和预期读回；
5. Resolve Live 测试通过；
6. 涉及画面或声音的能力完成真实渲染验证；
7. 参考仓库不可用或被移走后，本项目仍能独立运行。

## 3. 责任

### 3.1 确定性媒体分析

- ffprobe 技术探测；
- 代理和抽帧支持；
- 镜头检测；
- BPM、Beat、Onset 和能量；
- LUFS、True Peak 和 Loudness Range；
- 静音和对白压缩候选；
- 音乐裁切候选。

FunASR 和云端多模态模型不运行在此 MCP 内。

### 3.2 Resolve 执行

- Windows Resolve 连接；
- 项目、媒体池、时间线和轨道；
- 精确源范围和目标时间放置；
- 片段属性和静态/动态重构；
- Fusion、标题、转场和动态图形适配；
- LUT、调色节点和已认证 Look；
- 音频放置、音量和已认证处理链；
- 渲染、状态查询和结果读回。

### 3.3 创意素材适配

- 识别本机已部署能力；
- 部署经过批准的 LUT、字体和 Fusion 资源；
- 通过能力 Adapter 应用具体资源；
- 验证依赖、版本、输入端口、轨道和画幅；
- 真实渲染认证。

### 3.4 写入安全

- 计划校验；
- dry run/preview；
- 确认摘要；
- operation id 幂等；
- 单写者；
- 写后读回；
- outcome unknown 对账；
- 实际渲染验证。

## 4. 非职责

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

## 5. MCP 客户边界

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

### 5.1 创意资源输入边界

`davinci-engine-mcp` 不扫描整个 Nextcloud 采购库，也不负责向量检索。Creative 模块先完成目录检索、候选选择、本地化和内容哈希校验，再把确切本地缓存对象写入 CapabilityBinding。

Engine 只接受：

- 本地真实文件路径；
- 确切内容哈希；
- 已认证能力身份和约束；
- 已批准的 ResolveExecutionPlan。

在线占位、同步中、未校验或仅有云端引用的文件必须拒绝执行。

## 6. 进程与 Windows 运行时

- 首期固定复用现有 Conda 环境 `unofficial-davinci-mcp-win`（Python 3.10.20），不创建新的运行环境；
- API、Worker 与 Engine 共用同一个 Conda 环境和 `sys.executable`，但 `davinci-engine-mcp` 作为 Worker 启动的独立 stdio 子进程运行；
- 不开放额外 HTTP 端口，不要求用户单独部署服务；
- 通过 Windows Launcher 固定 `FUSION_PYTHON3_HOME`、Resolve Scripting 路径和 DLL 搜索顺序；
- 启动时在隔离子进程中探测 `DaVinciResolveScript` 和 `fusionscript.dll`；
- 原生模块探测失败不能使 API 和业务数据库进程崩溃；
- Resolve 未启动时，分析类工具仍可使用，Resolve 工具返回 disconnected；
- Resolve 重启后允许刷新连接，不要求重启 Product Application；
- 未经产品负责人明确同意，禁止创建新 Conda 环境或 `.venv`；只有出现经过复现且无法解决的依赖冲突，并说明取舍后，才允许拆分。

Product Application 和 API 进程不直接导入 Resolve 原生模块。

## 7. 内部模块

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
│   │   ├── playback.py
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

## 8. MCP 工具表面

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

## 9. 为什么不暴露全部原子工具

如果 Codex 逐个调用 `add_track`、`append_clip`、`apply_lut` 和 `create_fusion_node`：

- 步骤过多；
- 中途失败难以恢复；
- 模型可能遗漏关键操作；
- 产品校验容易被绕过；
- 任意 Lua 带来高风险；
- 版本和写入安全无法集中保证。

因此上游提交 ResolveExecutionPlan，由 MCP 内部把它展开为细粒度 Resolve 调用。

## 10. ResolveExecutionPlan 语义

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

## 11. 两阶段执行

### 11.1 校验

`validate_execution_plan` 检查：

- 当前 Resolve 连接；
- 项目和工作时间线；
- 源素材存在和范围合法；
- 目标轨道和时间合法；
- 能力已经认证且本机已部署；
- 用户可见基线不会被修改；
- 计划没有未支持操作；
- 渲染目标可写。

### 11.2 预览

`preview_execution_plan` 返回：

- 计划摘要；
- 将创建和修改的对象；
- 冲突和警告；
- 计划摘要哈希。

### 11.3 执行

`execute_execution_plan` 必须携带：

- 稳定 operation id；
- 已校验计划身份；
- 未变化的摘要哈希；
- 产品应用签发的执行许可；
- 预期项目和时间线。

如果任何前置事实变化，拒绝执行并要求重新预览。

## 12. 统一结果语义

所有工具使用统一结果外层，核心状态只有：

- `succeeded`；
- `failed`；
- `outcome_unknown`。

`outcome_unknown` 只用于写操作可能已经发生但响应或读回不确定的情况。

普通分析失败、能力不存在和参数非法返回 `failed`，并提供结构化原因。

## 13. 幂等与 Operation Journal

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

## 14. 写后读回

每类操作定义独立 Readback Contract。例如：

- 创建时间线：按稳定名称和标记查询；
- 插入片段：查询目标轨道、开始帧、时长和媒体身份；
- 应用 LUT：读取目标节点或效果状态；
- 挂载 Fusion：检查节点和连接；
- 设置重构图：读取 Zoom/Pan/Tilt/Rotation；
- 提交渲染：查询渲染队列和输出文件。

MCP 响应成功但实际对象不符合预期，整体结果仍为失败。

## 15. 结果未知对账

`reconcile_operation` 只读执行：

1. 读取 Journal 中的预期效果；
2. 刷新 Resolve 连接和对象；
3. 查询目标对象；
4. 分类为“已经发生”“没有发生”“仍无法确认”；
5. 返回证据。

只有明确证明没有发生时，上游才能重新执行。

## 16. 创意能力 Adapter

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

## 17. 能力认证

### 17.1 状态

- `testing`：正在测试，不进入项目执行；
- `certified`：可以自动执行，但仍受明确约束；
- `manual_only`：可人工使用，不进入无人自动链路；
- `unsupported`：不使用。

运行时只使用 `certified`。

### 17.2 五步合同

1. 可发现；
2. 可部署；
3. 可执行；
4. 可读回；
5. 可真实渲染。

### 17.3 初始实测基线

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

## 18. 项目风格套件

Engine 不选择风格，但执行计划只能引用 Creative 模块已经绑定的有限风格套件，例如：

- 少量标题层级；
- 允许的运镜 Recipe；
- 允许的转场类别；
- 音效强度和类别；
- 调色方向；
- 禁止的过度效果。

Engine 验证绑定，不根据模板数量随机选择。

## 19. 渲染与验证

### 19.1 渲染

- 内部工作版和用户候选使用不同输出身份；
- 输出目录和文件名由产品计划决定；
- 渲染任务必须可查询；
- 文件存在不等于渲染完整。

### 19.2 技术验证

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

## 20. 安全

- 不暴露任意 Lua 工具；
- 内部 Lua/Fusion 脚本只能来自版本控制中的可信实现；
- 未知可执行文件和插件不自动安装；
- 管理员部署使用白名单目录；
- 所有写工具默认拒绝直接自然语言请求；
- MCP 只接受结构化、已校验的计划；
- 文件路径限制在允许根目录；
- 渲染和部署路径做规范化与越界检查。


## 21. 测试

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
