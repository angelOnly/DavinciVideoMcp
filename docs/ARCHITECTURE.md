---
status: approved-baseline
version: 1.6
updated: 2026-08-01
scope: architecture
---

# DavinciMcp 系统架构

## 1. 架构目标

本架构必须同时满足：

1. 核心业务稳定：项目、素材、版本和反馈不依赖某个模型、MCP 或 Resolve 进程的内部状态；
2. 外围能力可替换：Codex、多模态模型、FunASR、素材存储和 Resolve 实现通过 Ports/Adapters 隔离；
3. 模块边界清晰：每类决定只有一个责任所有者；
4. 新增功能局部化：新增字幕、插件、素材类型或分析器时，不修改无关模块；
5. 写入可靠：所有 Resolve 写操作可预览、可追踪、可读回、可对账；
6. 架构简单：不为当前没有证据的未来需求引入平台级复杂度。

首期架构基线：

> **采用模块化单体，并固定复用现有 Conda 环境 `unofficial-davinci-mcp-win`（Python 3.10.20）。API、Worker 和 `davinci-engine-mcp` 共用代码库、依赖和同一个 Python 解释器，但作为独立本地进程运行；Worker 通过 stdio 调用 Engine MCP。任务先持久化再由 Worker 领取，同一 Resolve 实例只允许一个活动写入者。首期不引入微服务、外部消息队列或通用工作流平台，但必须具备有限任务状态、租约与心跳、幂等、超时、安全重试、恢复、读回和对账。未经产品负责人明确同意，不得创建新的 Conda 环境、`.venv` 或其他 Python 虚拟环境。**

只有出现已复现且无法解决的依赖冲突，并先说明单环境方案的代价与拆分方案的复杂度后，才允许申请拆分 Python 环境。

## 2. 核心不变量

以下规则必须由代码和测试保护：

- Product Application 是项目、运行、用户素材状态、视频版本、反馈和批准状态的唯一业务状态所有者；
- Codex Thread、Web UI、Nextcloud 同步状态和 Resolve 对象都不保存第二份业务状态；
- Skills 不写数据库、不管理工作流、不部署素材、不调用 Resolve；
- 用户勾选用于本次任务的素材未全部通过服务端权威校验时，不得开始任务；
- 大型媒体、模型、模板和渲染文件不存进 SQLite；SQLite 只保存业务事实、元数据、索引和引用；
- Nextcloud 占位文件不能直接交给 Resolve，必须先本地化并校验内容哈希；
- 只有 Execution 模块可以调用 `davinci-engine-mcp` 的写工具；
- 同一 Resolve 实例同一时刻只有一个活动写入者；
- 用户可见版本不可覆盖；
- 每条反馈绑定确切视频版本；
- 每次 Resolve 写入对应已校验的 ResolveExecutionPlan；
- 工具返回 `success` 后仍要读取真实 Resolve 对象和渲染结果；
- 外部写入可能已发生但响应不确定时，进入 `outcome_unknown`，对账前不得重发；
- 未认证创意能力不能进入自动执行计划；
- 多模态模型的能力以实际探测结果为准，不能只根据模型列表或名称推断；
- 模型输出必须通过代码 Schema 和业务规则校验；

## 3. 运行拓扑

```text
┌────────────────────────────────────────────────────────────┐
│                           Web                              │
│ 项目 / 上传 / 校验 / 简报 / 进度 / 播放 / 反馈 / 版本 / 交付 │
└──────────────────────────┬─────────────────────────────────┘
                           │ HTTP + SSE
                           ▼
┌────────────────────────────────────────────────────────────┐
│               Product Application API Process             │
│ Application Services · SQLite · Project File Store         │
│ 项目、素材状态、运行、版本、反馈、目录引用、Codex Thread 绑定 │
└──────────────┬─────────────────────────────┬───────────────┘
               │                             │ 持久任务
               ▼                             ▼
┌──────────────────────────┐      ┌───────────────────────────┐
│ video-project-mcp        │      │ Workflow Worker Process   │
│ Codex/Skills 的项目接口   │      │ 租约、心跳、步骤与唯一写者   │
└──────────────┬───────────┘      └───────┬──────────┬────────┘
               │                           │          │
               ▼                           ▼          ▼
┌──────────────────────────┐   ┌─────────────────┐  ┌──────────────────────┐
│ Codex App Server         │   │ Multimodal API  │  │ davinci-engine-mcp  │
│ 持久线程 + Skills         │   │ OpenAI-compatible│  │ 本地 stdio 子进程     │
└──────────────────────────┘   └─────────────────┘  └──────────┬───────────┘
                                                              ▼
                                                     DaVinci Resolve Studio
```

API、Worker 和 Engine MCP 使用现有 Conda 环境 `unofficial-davinci-mcp-win` 与同一仓库，但保持独立进程：

- API 进程负责快速请求，不加载 Resolve 原生模块；
- Worker 执行长任务、持有任务租约并管理唯一 Resolve 写入租约；
- Worker 通过 stdio 启动和调用 `davinci-engine-mcp`；
- Engine MCP 崩溃时，API、SQLite 和用户历史仍然可用；
- 统一启动器负责启动进程和健康检查，用户不需要手工打开多个终端。

## 4. 物理技术基线

首期使用：

- Python 环境：现有 Conda 环境 `unofficial-davinci-mcp-win`；
- Python 版本：3.10.20；
- Web API：FastAPI 或等价轻量框架；
- 前端：TypeScript + React；
- 业务数据库：本机 SQLite WAL；
- 创意目录数据库：独立本机 SQLite + FTS5，可重建；
- 向量索引：通过 `SemanticIndexPort` 可选接入本地嵌入式索引；
- 文件存储：本地项目工作区 + Nextcloud 素材库 + 本地内容寻址缓存；
- 进度：HTTP 查询 + SSE；
- 后台任务：数据库驱动的持久任务和 Step Journal，不引入外部消息队列；
- Codex：Codex App Server；
- Resolve 引擎：同一现有 Conda 环境中的独立 `davinci-engine-mcp` stdio 进程；
- 视频工具：FFmpeg/ffprobe、PySceneDetect 或等价实现；
- 中文转写：本地 FunASR 模型；
- 多模态理解：`OpenAICompatibleMultimodalAdapter`，当前配置模型 ID 为 `gemini-3.5-flash`；
- DaVinci：Resolve Studio 21 的本机实测行为作为执行真相。

如果 FunASR/PyTorch 与 Resolve 原生依赖出现经过复现的不可解决冲突，先记录冲突和可选取舍并向产品负责人确认；获准后才可将某个 Adapter 移到独立环境或子进程，不得预先拆分。


### 4.1 Conda 环境合同

首期运行环境是已经存在的 Conda 环境，不是由项目脚本新建：

```text
环境管理器：Conda / Anaconda
环境名：unofficial-davinci-mcp-win
Python：3.10.20
```

开发和启动必须满足：

- 依赖安装在该环境中，使用 `python -m pip ...` 或经过确认的 Conda 安装命令；
- API、Worker 和 Engine MCP 的 `sys.executable` 必须相同；
- 启动器使用 `conda run -n unofficial-davinci-mcp-win ...`，或要求先显式 `conda activate unofficial-davinci-mcp-win`；
- 启动时校验 `CONDA_DEFAULT_ENV`、`sys.version_info` 和 `sys.executable`；不匹配时停止并给出修复提示；
- 禁止项目脚本自动运行 `conda create`、`python -m venv`、`uv venv`、Poetry 自动建环境或创建 `.venv`；
- 变更依赖前先导出当前包清单，便于回退，但不得因此自动克隆或创建第二个环境。

## 5. 模块化单体

```text
src/davinci_app/
├── project/       # 项目、用户素材、输入快照、运行、版本、反馈、批准
├── media/         # 上传验证、代理、证据生成、缓存与证据融合
├── editorial/     # Skill 调用、素材理解、方向、EditPlan、修订
├── creative/      # 素材目录、检索、候选、认证能力、绑定、风格套件
├── execution/     # 校验、编译、engine MCP、读回、渲染、QC
├── workflow/      # 持久任务、租约、步骤、恢复和条件路由
├── interfaces/    # HTTP、SSE、video-project-mcp
├── adapters/      # Codex、多模态、FunASR、存储、索引、engine MCP
└── bootstrap.py   # 唯一真实依赖组装入口
```

### 5.1 Project 模块

拥有：

- Project；
- 用户可编辑项目简报；
- UploadedAsset 业务身份和校验状态；
- 每次运行冻结的输入快照；
- EditingRun；
- 用户可见 VideoVersion；
- Feedback；
- 用户批准和交付记录；
- Codex Thread 绑定。

只有 Project Application Service 可以冻结输入、创建用户可见版本或修改批准状态。

### 5.2 Media 模块

拥有：

- 上传 staging 与服务端素材校验；
- 源素材身份和内容哈希；
- 技术探测和稳定工作副本；
- 分析代理；
- FunASR 转写；
- 镜头图；
- 抽帧和联系表；
- 节拍、响度、静音等确定性证据；
- 多模态证据；
- Evidence Bundle 和证据缓存。

Media 不作最终选片和结构决定。

### 5.3 Editorial 模块

拥有：

- SkillInvocation；
- SourceUnderstanding；
- EditorialDirection；
- 专业声音、视觉、文字和收尾建议；
- EditPlan；
- 用户反馈后的修订方案。

Editorial 不调用 Resolve，不保存项目工作流状态，也不直接访问整个创意素材库。

### 5.4 Creative 模块

拥有：

- 原始采购库与认证素材库的目录引用；
- 经过认证的能力目录；
- 能力约束和真实预览；
- 结构化过滤、FTS 和可选语义召回；
- 项目风格套件；
- 从创意意图到少量候选的查询；
- 确切 CapabilityBinding；
- Nextcloud 本地化和本地缓存引用；
- 本机部署库存的比较结果。

Creative 不决定内容结构，也不执行 Resolve 写入。

### 5.5 Execution 模块

拥有：

- EditPlan 业务校验；
- CapabilityBinding 校验；
- ResolveExecutionPlan 编译；
- `davinci-engine-mcp` 客户端；
- Resolve 写入租约；
- 写后读回；
- 渲染与技术 QC；
- operation reconciliation。

只有 Execution 可以发起 Resolve 写操作。

### 5.6 Workflow 模块

唯一负责跨模块编排：

```text
Project → Media → Editorial → Creative → Execution → Project Version
```

其他模块不能自行启动下一专业 Skill，也不能绕过 Workflow 直接调用其他模块的内部实现。

## 6. 依赖规则

依赖方向固定为：

```text
interfaces / adapters
        ↓
application services / workflow
        ↓
domain contracts
```

核心模块不得直接依赖：

- FastAPI；
- Codex App Server SDK；
- OpenAI SDK 或具体多模态供应商 SDK；
- FunASR；
- MCP SDK；
- DaVinciResolveScript；
- SQLite ORM；
- FFmpeg Python 包；
- Nextcloud 客户端实现；
- LanceDB 或其他具体向量后端。

所有外部能力通过 Port 进入：

- `ProjectRepository`；
- `ProjectFileStorePort`；
- `UploadValidatorPort`；
- `MediaEnginePort`；
- `TranscriberPort`；
- `MultimodalAnalyzerPort`；
- `AgentRuntimePort`；
- `CreativeCatalogRepository`；
- `CreativeLibraryStorePort`；
- `LocalAssetCachePort`；
- `AssetSearchPort`；
- `SemanticIndexPort`；
- `ResolveEnginePort`。

真实实现只在 `bootstrap.py` 组装。

## 7. 两个 MCP 的边界

### 7.1 `video-project-mcp`

这是 Codex/Skills 的项目上下文接口，属于 Product Application 的一个 Interface，不是独立业务系统。

它提供高层、项目作用域的查询，例如：

- 获取本次委托简报；
- 获取素材证据目录；
- 读取某个时间窗口的转写、图片和多模态证据；
- 根据当前创意意图获取少量能力候选和约束；
- 获取基线视频、EditPlan 和用户反馈。

它不：

- 直接写数据库；
- 直接操作 Resolve；
- 暴露任意底层文件系统；
- 暴露完整素材库；
- 维护第二份项目状态。

Skill 结果通过 Codex turn 的结构化输出返回 Workflow，由应用层校验和保存。

### 7.2 `davinci-engine-mcp`

这是 Worker 私有的本地 stdio 执行接口，统一提供：

- Windows Resolve 连接及项目、时间线、轨道、片段、Fusion、调色、音频和渲染控制；
- 节拍、Onset、响度、静音和音乐结构等确定性媒体分析；
- 创意素材部署、应用和认证适配；
- 执行计划校验、写入安全、读回、对账和渲染验证。

Codex 和 Skills 不直接连接它。完整设计见 `DAVINCI_ENGINE_MCP.md`。

## 8. 系统健康检查

系统健康与单个素材校验是两件事。

启动器和 Worker 必须检查：

- SQLite 目录可写且 WAL 正常；
- 项目工作区、Nextcloud 原始库、认证库和本地缓存路径可访问；
- FFmpeg/ffprobe 可执行；
- FunASR 配置的本地模型存在、能够加载并处理标准音频；
- OpenAI-compatible 多模态端点可访问；
- 配置模型 `gemini-3.5-flash` 能完成文本、图片、音频、视频和结构化结果测试中的哪些项；
- Codex App Server 可启动；
- `davinci-engine-mcp` 可启动；
- Resolve 是否连接，以及当前哪些执行能力可用。

能力探测结果以结构化矩阵保存，例如：

```text
supports_text
supports_image
supports_audio
supports_video
supports_video_audio
supports_structured_output
max_observed_payload
```

模型列表中出现某个 ID 只证明它可被路由，不证明音频和视频输入可用。

如果项目要求的能力不可用：

- 不影响的项目可以显式使用降级模式并披露限制；
- 会明显影响承诺质量的项目禁止开始，直到能力恢复或用户调整要求；
- 不允许静默把图片+转写模式宣称为直接音视频理解。

## 9. 用户素材校验门

### 9.1 流程

```text
浏览器预检
→ 上传到 staging
→ 内容哈希
→ ffprobe/图片探测
→ 分段或完整解码检查
→ 必要时生成稳定工作副本
→ ready / invalid
→ 提交时重新确认身份
```

浏览器预检只负责快速反馈；服务端结果是唯一真相。

### 9.2 状态

Web 只展示：

- `uploading`；
- `validating`；
- `ready`；
- `invalid`。

警告附着在 `ready` 上，不再增加更多状态。

### 9.3 复用

上传校验生成的技术探测、内容哈希和稳定工作副本必须被后续 Media Evidence 流程复用，不能在任务开始后无条件重复执行。

## 10. Codex 与 Skills 运行链路

### 10.1 Thread 绑定

- 新委托：`thread/start`；
- 后续修改：`thread/resume` 后在同一线程 `turn/start`；
- Thread ID 保存在 Project 模块；
- Thread 是否在运行只影响智能任务，不改变视频版本事实。

### 10.2 一次 Skill 调用

```text
Workflow Step
  → 组装 SkillInvocation
  → Codex App Server turn/start
      - assignment task text
      - 显式 skill input
      - 关键 localImage 联系表或预览
      - outputSchema
  → Skill 通过 video-project-mcp 读取所需证据或少量候选
  → 返回结构化结果
  → 应用层 Schema 与业务校验
  → 保存结果或请求补充证据
```

视频和音频的直接理解由 `MultimodalAnalyzerPort` 的当前实现完成。Codex 使用结构化证据和联系表，不假装自己已经直接观看原始视频。

### 10.3 调用规则

- Skills 不互相调用；
- Skill 可以声明需要其他专业责任或补充证据；
- Workflow 决定是否启动后续步骤；
- 输出格式失败时允许一次结构修复，仍失败则该 Step 失败；
- Skill 不拥有重试、状态迁移和数据库逻辑。

## 11. 媒体理解链路

```text
Validated Asset
  → 复用技术探测与稳定工作副本
  → 分析代理与音频提取
  → FunASR 转写/VAD/说话人
  → 镜头检测与分层抽帧
  → davinci-engine-mcp 节拍/响度/静音分析
  → OpenAI-compatible 多模态分析（当前模型 gemini-3.5-flash）
  → Evidence Fusion
  → video-source-understanding
  → SourceUnderstanding
```

任何云端模型的时间分辨率都不作为精确切点真相。快速动作、精确动作边界和切点候选必须使用本地密集帧、缩短时间窗口、慢放代理或其他确定性证据补充。完整细节见 `MEDIA_INTELLIGENCE.md`。

## 12. 创意素材存储与检索

完整方案见 `CREATIVE_LIBRARY.md`，架构边界如下。

### 12.1 存储分层

```text
Nextcloud 原始采购库
→ Nextcloud 认证素材库
→ 工程目录 workspace/data/creative_catalog.db + FTS5 + 可选语义索引
→ 工程目录 workspace/creative-cache
→ Resolve
```

默认路径可配置为：

```text
原始采购库：C:\Users\13222\Nextcloud\达芬奇素材
认证素材库：C:\Users\13222\Nextcloud\达芬奇认证素材库
```

SQLite 数据库统一放在工程根目录的 `workspace/data`，工程根目录不放入 Nextcloud 同步目录。

### 12.2 数据库边界

- `product.db` 保存项目、用户素材、任务、步骤、版本、反馈和结果引用；
- `creative_catalog.db` 保存创意能力元数据、认证结果、FTS 索引、存储引用和预览引用；
- `creative_catalog.db` 是可重建目录，不承担项目业务事务；
- 大文件、模型权重和预览文件只保存路径与内容哈希，不保存 BLOB。

### 12.3 混合检索

候选检索固定为：

```text
结构化硬过滤
→ FTS5 全文召回
→ 可选语义向量召回
→ 规则与项目风格重排
→ 返回 5～10 个候选和真实预览
```

向量后端只能通过 `SemanticIndexPort` 接入。没有向量索引时，结构化过滤和 FTS5 仍必须可用；启用向量时不修改 Skills、Catalog 或 Resolve 层。

### 12.4 本地化

选中 Nextcloud 资源后：

```text
确认文件已下载
→ 校验内容哈希
→ 复制或链接到本地内容寻址缓存
→ 为当前运行加锁
→ Resolve 使用缓存路径
→ 运行结束后解除锁定
```

云朵占位、同步中或哈希不一致的文件不得进入 CapabilityBinding。

## 13. 创意意图到执行的分层

### 13.1 EditPlan

表达创意决定：

- 使用哪段真实内容；
- 内容顺序和时间关系；
- 音乐、文字、视觉和收尾意图；
- 每项效果承担的功能、强度和保护条件；
- 可接受的简单退化。

EditPlan 不写具体 MCP 调用和任意 Lua。

### 13.2 CapabilityBinding

Creative 模块将抽象意图绑定到：

- 已认证资源；
- 确切版本或内容哈希；
- 已验证约束；
- 本地缓存身份；
- 需要的适配器；
- 真实可用参数范围。

涉及审美的音乐、音效、模板和 Look，先返回少量真实候选给专业 Skill 比较；最终可执行性由代码确认。

### 13.3 ResolveExecutionPlan

确定性编译器将 EditPlan 和 CapabilityBinding 转成精确操作：

- 项目和工作时间线；
- 轨道；
- 源媒体范围；
- 目标时间和时长；
- 片段属性；
- 音频放置；
- 创意能力应用；
- 调色和收尾操作；
- 渲染目标；
- 每项操作的预期读回结果。

编译器不临场改变故事、换素材或发明风格。

## 14. 持久任务、租约与工作流

### 14.1 提交与领取

任务必须先写入 `product.db`，再由 Worker 原子领取：

```text
创建 queued task
→ Worker 取得 lease
→ 标记 running
→ 周期性 heartbeat 续租
→ 每步写 Step Journal
→ 完成后释放 lease
```

租约过期不等于任务可以从头重跑。新 Worker 必须先检查最后一步和外部副作用。

### 14.2 步骤

首期不使用通用工作流 DSL。每个步骤实现：

```text
should_run(context)
execute(context) -> StepResult
```

推荐步骤：

```text
ValidateUploads
FreezeInput
PrepareMedia
BuildEvidence
UnderstandSources
CreateDirection
RunSpecialists
FinalizeEditPlan
SearchAndBindCapabilities
CompileExecutionPlan
ExecuteWorkTimeline
VerifyTimeline
RenderWorkPreview
PlanFinishing
ApplyFinishing
RenderCandidate
VerifyCandidate
PublishVideoVersion
```

修订运行复用同一组步骤，但从基线视频、基线计划和反馈开始。

### 14.3 状态

Run 只保留：

- `queued`；
- `running`；
- `waiting_user`；
- `succeeded`；
- `failed`；
- `cancelled`；
- `outcome_unknown`。

`outcome_unknown` 只表示外部写操作可能已发生但无法确认，不用于普通模型失败、素材缺失或用户未回复。

### 14.4 超时与重试

- 只读、探测、抽帧和确定性分析可使用有上限的安全重试；
- 模型格式错误可允许一次结构修复；
- 用户要求冲突进入 `waiting_user`；
- Resolve 写入超时先进入对账，不能自动重发；
- 租约过期后的恢复必须从 Step Journal 和 operation journal 判断下一动作。

## 15. Resolve 单写者与恢复

### 15.1 单写者

Workflow Worker 获取本机 Resolve 写入租约后才能调用写工具。API、Codex、Skills 和其他 Worker 都不能写 Resolve。

### 15.2 操作记录

每个写操作具备稳定 `operation_id`，并关联：

- 计划；
- 预期项目和时间线；
- 预期变化；
- 发送和完成时间；
- 响应；
- 读回与验证结果。

精确字段由代码 Schema 定义。

### 15.3 结果未知

若连接在写入期间中断：

1. 不重发；
2. 调用 `reconcile_operation`；
3. 只读查询目标项目、时间线、轨道、片段或渲染任务；
4. 确认已成功、未执行或仍无法确认；
5. 只有证明未执行时才允许重新提交。

## 16. 内部工作版与用户版本

- 每次 EditingRun 使用独立工作时间线；
- 在发布成片候选前，内部工作时间线可以接受本次运行的收尾调整；
- 一旦发布为用户可见 VideoVersion，该时间线和渲染成为不可覆盖基线；
- 后续反馈创建新的 EditingRun、新时间线和新版本；
- 用户批准不修改视频内容，只改变该版本的业务状态并触发交付。

## 17. 文件与数据库存储

### 17.1 工程内运行目录

应用管理的数据库、项目工作区和创意缓存统一放在当前工程根目录下：

```text
<project-root>/
└── workspace/
    ├── data/
    │   ├── product.db
    │   └── creative_catalog.db
    ├── projects/<project-id>/
    │   ├── staging/       # 上传临时文件
    │   ├── source/        # 已验证源文件或受管引用
    │   ├── working/       # 稳定工作副本
    │   ├── proxy/         # 分析代理
    │   ├── evidence/      # 转写、镜头图、联系表、多模态证据
    │   ├── plans/         # 结构化计划快照
    │   ├── renders/       # 内部预览和用户版本
    │   └── delivery/      # 批准版本派生输出
    └── creative-cache/
        └── objects/<content-hash>
```

`<project-root>` 指工程根目录。所有相对路径按工程根目录解析，不能依赖当前进程工作目录。`workspace/` 不提交 Git，工程根目录不放在 Nextcloud 同步目录中。

### 17.2 Nextcloud 创意素材源

```text
Nextcloud/
├── 达芬奇素材/          # 原始采购库，不直接运行
└── 达芬奇认证素材库/    # 拆分、预览、认证后的原子资源
```

Nextcloud 只保存原始采购文件和认证资源源文件；被项目选中的资源必须先本地化到 `<project-root>/workspace/creative-cache/objects/`，Resolve 不直接使用云端占位路径。

源文件、用户原始反馈和用户可见渲染不被原地覆盖。

## 18. 缓存与失效

媒体证据缓存以：

```text
源内容哈希 + 分析器身份/版本 + 关键配置
```

作为身份。

增量规则：

- 上传校验已经完成的技术探测直接复用；
- 源文件变化只重建该素材相关证据；
- 只更换多模态模型，只重做多模态证据；
- 只更新 FunASR 热词，只重做转写和相关融合；
- 只请求新候选边界，只生成对应 dense window；
- 只修改字幕样式，不重做素材理解；
- 创意向量索引可从 Catalog 元数据和预览重新生成；
- 本地创意缓存可清理，但被活动运行锁定的对象不得清理。

## 19. 新增功能的局部影响

### 新增一种创意模板

只需：

- 新增或配置 Creative Adapter；
- 上架能力元数据和预览；
- 更新 FTS/语义索引；
- 增加编译映射；
- 通过能力合同测试。

不修改项目、版本、反馈、素材理解和 Codex Thread。

### 新增一种素材存储后端

实现 `CreativeLibraryStorePort` 和本地化合同，不修改 Catalog、Skills 和 ResolveExecutionPlan。

### 更换向量后端

只替换 `SemanticIndexPort`；结构化过滤、FTS、重排和候选合同保持不变。

### 新增一种音频分析器

实现 `MediaEnginePort` 的新能力并在 Evidence Fusion 中登记，不修改 Resolve 和 Skills。

### 更换多模态模型或反代

只替换 `MultimodalAnalyzerPort` 实现或配置，保持 MultimodalEvidence 合同，并重新运行能力探测。

### 更换 Resolve 实现

只替换 `ResolveEnginePort`/`davinci-engine-mcp`，EditPlan 和产品状态不变。

### 新增专业 Skill

只增加：

- Skill 文件；
- 输出 Schema；
- 条件 Workflow Step；
- 导演综合规则和评测。

不得顺便修改 Resolve Adapter、数据库或其他无关专业 Skill。

## 20. 架构强制测试

### 20.1 依赖测试

CI 检查核心模块不能导入 FastAPI、OpenAI SDK、FunASR、MCP、Resolve 原生库、Nextcloud 实现或向量后端；除 Execution Adapter 外不得调用 Engine MCP。

### 20.2 Port 合同测试

所有替代实现必须通过同一合同：

- Upload Validator；
- Transcriber；
- Multimodal Analyzer；
- Agent Runtime；
- Resolve Engine；
- Creative Library Store；
- Local Asset Cache；
- Asset Search / Semantic Index；
- Creative Adapter；
- Repository。

### 20.3 上传门禁测试

验证：

- 破损、零时长、缺少必需流的文件不能提交；
- 可修复的 VFR/编码问题生成工作副本后可提交；
- `validating` 或 `invalid` 素材使按钮禁用；
- 提交时哈希变化会拒绝冻结输入。

### 20.4 Worker 恢复测试

在每个步骤前后模拟进程退出，验证：

- lease 能过期并被安全接管；
- 已完成步骤不重复；
- 用户版本不覆盖；
- Resolve 写入不重复；
- 可从检查点继续。

### 20.5 Creative Library 测试

验证：

- Nextcloud 占位文件不会直接进入计划；
- 本地化后哈希一致；
- 结构化硬约束不会被语义相似度绕过；
- Skill 只收到有限候选；
- 索引删除后可从 Catalog 重建。

### 20.6 Golden Render 测试

每种认证创意能力使用标准项目实际渲染，检查轨道、时间、透明、文字、LUT、声音和输出文件。

### 20.7 Skill 评测

使用固定素材检查：

- 时间范围是否可回链；
- 是否把稀疏证据当连续事实；
- 是否保留未知；
- 是否遵守用户硬约束；
- 是否使用未认证能力；
- 是否产生可被确定性编译器理解的结果。

## 21. 推荐代码结构

```text
repo/
├── README.md
├── AGENTS.md
├── docs/
├── .agents/skills/
├── web/
├── src/davinci_app/
│   ├── project/
│   ├── media/
│   ├── editorial/
│   ├── creative/
│   ├── execution/
│   ├── workflow/
│   ├── interfaces/
│   ├── adapters/
│   └── bootstrap.py
├── davinci-engine-mcp/
│   ├── src/davinci_engine/
│   └── tests/
├── models/
│   ├── README.md
│   └── manifest.example.yaml
└── tests/
    ├── unit/
    ├── architecture/
    ├── contract/
    ├── workflow/
    └── end_to_end/
```

`models/` 中的实际模型权重不提交 Git，只通过本机配置引用。
