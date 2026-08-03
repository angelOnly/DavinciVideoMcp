# 开发记录

## 2026-08-03：正确仓库同步与创意 Adapter 基础

- 已确认正式项目目录为 `E:\ai\DavinciVideoMcp`；此前错误目录中的根目录式实现不会直接复制或推送；
- 创意资源现在在 `CapabilityBinding` 前后都进行 SHA-256 核验，并写入工程内 ASCII 内容寻址缓存；Nextcloud 中的中文路径不会直接交给 Resolve；
- Catalog 增加原子能力机制、FTS 检索、五步认证证据门禁与受控登记。仅有文件或预检结果不能把状态提升为 `certified`；
- Engine 增加按机制拆分的直接媒体、LUT、字体和单输入 Fusion Effect Adapter。LUT 支持 `.cube` 规范化、受管部署和 `SetLUT` 读回；字体与 Fusion 的自动 Compiler Mapping 尚未认证，仍会显式拒绝；
- Execution Plan 现在可以表达已绑定的直接媒体放置与 LUT 应用，并在写入前再次校验本地路径、内容哈希、机制和参数；写后继续按目标轨道、帧和资源身份读回；
- 新增只读预检 CLI 以及 Catalog、Adapter、Compiler 单元测试。当前自动化结果为 `44 passed, 18 subtests passed`（实际提交前会重新运行）；
- 本次没有运行批量 Resolve 认证，也没有把任何已有素材改为 `certified`。当前认证能力数仍为 `0`；真实预览必须来自后续每个资源的 Resolve 渲染，不能使用认证测试卡或占位图。

## 2026-08-02：状态更正、候选门禁与首版降级证据链

- 三条 `videos/` 测试素材（游泳、猫咪、Tim O'Reilly 访谈）此前只执行了“按上传顺序截取开头并保留原声”的 Resolve 技术测试；访谈还包含彩条和长时间静态标题。它们没有经过转写、多模态理解、素材理解、故事与选片、声音/视觉/文字规划、能力绑定、工作版复核、收尾或候选验证；
- 因此这些输出的真实身份是 **内部技术预览**，不是 `candidate-v1`，也不能让项目处于 `ready_for_review`。数据库初始化会非破坏性迁移旧的 `testing_preset` 候选记录：保留原文件和审计引用，隐藏旧 VideoVersion，并创建 `technical_preview` 记录；
- `TestCutCompiler` 已更名为 `EngineSmokeCompiler`，且只能处理 `engine_smoke`。正式 `build_candidate` 不会调用它；
- 正式路径已串联完整的专业阶段合同；FunASR、受控 Codex/专业 Skill 运行时或认证能力缺失时会安全进入 `waiting_user`，不会调用 Resolve 伪造候选。Gemini 直连音视频未通过时则转入首版本地降级证据链，不会单独造成等待；
- 当前创意目录认证能力数为 `0`。没有通过发现、部署、执行、读回、渲染认证的音乐、字体、模板和动效不可被绑定、使用或声明支持；
- 自动化反向测试覆盖：技术预览不能发布、仅有 MP4 不能发布、任一专业前置产物缺失不能发布、摘要或哈希基线不一致不能发布、缺少专业前置时不得调用 Engine。
- 本机健康检查已实测：FunASR 能加载本地 ASR、VAD 和标点模型并转写标准中文音频；受限 Codex App Server 已发现六个专业 Skill。
- Gemini 本机配置保存在 `.env`。实测 `gemini-3-flash` 能通过文本、图片与结构化 JSON 探测，也能生成一张测试帧的图片证据；该代理对标准 OpenAI 的 `input_audio` 与 `video_url` 返回 HTTP 400，因此其音频、视频和直接声画能力均为 `available=false`。首版不把 Gemini 图片输入冒充为视频理解，而是使用 FunASR、本地声音测量、按需密集抽帧和 Codex 图片分析继续运行；工作版与候选复核也明确披露抽帧和转写的限制。

## 运行时基线

- Conda 环境：`unofficial-davinci-mcp-win`；
- Python：`3.10.20`；
- Resolve：`DaVinci Resolve Studio 21.0.3.7`；
- 不创建 `.venv`、新的 Conda 环境或其他隔离环境。

## 外部参考（只读对照）

两个参考仓库仅保存在 `workspace/upstream-reference/`，正式产品源码不导入其模块：

- `tooflex-davinci-resolve-mcp`：`e8b3d9215e9c68925b6d49d6327c5ad0b5d92545`；
- `wasserman-unofficial-davinci-mcp`：`22580fb5b35c280aeac05923f6ca784c620a0dc2`。

## 本机实测记录

- FFmpeg/ffprobe 可用；三组 `videos/` 测试素材可通过服务端哈希、探测和解码校验；
- Resolve Scripting API 可安全导入并连接；
- Engine 对 Resolve 写操作使用 operation id、Journal、写后读回与 `outcome_unknown` 保护；
- HEVC 与 Constrained Baseline H.264 进入工作流时会生成 H.264/AAC、CFR 的受管工作副本，原文件不被覆盖；导入结果仍以 Engine 写后读回为准；
- 当已加载 Resolve 项目但当前页面不可读时，Worker 会在写入前停止并报告未就绪；刚启动时的“项目管理器”则允许 Engine 先加载受管项目。

## Resolve 21 渲染兼容性与实机结果

- Resolve 21 的 `GetRenderFormats()` 与 `GetRenderCodecs()` 返回“显示名称 → API 标识符”；实际设置 MP4/H.264 时必须传入 `mp4` / `H264`，不能传入 `MP4` / `H.264`；设置后通过 `GetCurrentRenderFormatAndCodec()` 读回确认。
- Resolve 21 部分成功写操作会返回 `None`；Engine 仅将明确的 `False` 视为拒绝，并使用后续状态、时间线或输出文件读回确认结果。
- 本机中文界面中的渲染任务完成状态为“完成”；Engine 同时识别完成百分比、中文和英文完成状态，避免将已完成渲染误记为 `outcome_unknown`。
- 已在三组真实测试素材上完成“校验 → 工作副本 → 新时间线执行 → 读回 → Resolve 渲染 → FFmpeg 技术验证”的 Engine 技术闭环：游泳 40.00 秒（1080×1920）、猫咪 69.42 秒（1080×1920）、访谈 90.01 秒（1280×720）；三者均为 H.264/AAC、30 fps。该结果不构成专业成片或候选发布证据。
- 历史 `outcome_unknown` 记录保持不变；修复后使用新的运行、时间线和 operation id 完成验证，未对未知操作盲目重试。
- 自动化测试：`pytest -q` 为 `36 passed, 18 subtests passed`；覆盖 Gemini 直连音视频不可用时不发送 MP4、不中断 FunASR + Codex 抽帧证据链、12 张概览抽帧，以及技术预览和缺少专业产物不能发布。`EngineSmokeCompiler` 不再触发 pytest 收集警告；Web `npm run build` 通过。

## 启动

在仓库根目录执行：

```powershell
.\scripts\start.ps1
```

脚本启动 API 与 Worker，Engine MCP 由 Worker 按需启动。默认工作台地址为 `http://127.0.0.1:8787`。
