---
status: approved-baseline
version: 1.4
updated: 2026-08-01
scope: implementation-plan
---

# 本次开发实施计划

## 1. 实施原则

- 每个切片必须产生可观察结果；
- 先完成一条真实闭环，再扩大能力；
- 只实现本文切片，`ROADMAP.md` 不属于本次验收；
- 不一次实现完整素材市场、插件系统和所有视频类型；
- 精确 Schema 只为当前切片建立；
- 每个新增抽象必须由至少两个真实用例证明；
- Resolve 写入安全、上传门禁和素材证据真实性优先于 UI 完整度；
- 首期统一使用 Python 3.10.20 Conda 环境，不提前拆分环境。

## 2. 切片 0：工程骨架与架构约束

实现：

- 根目录和模块结构；
- README 文档入口和架构 import 规则；
- Python 3.10.20 Conda 环境锁定与启动脚本；
- API、Worker、`davinci-engine-mcp` 独立进程入口；
- Worker 通过 stdio 启动 Engine MCP；
- 工程根目录内的 `workspace/data/product.db`、`workspace/data/creative_catalog.db`、`workspace/projects/` 和 `workspace/creative-cache/`；
- `bootstrap.py` 依赖组装；
- 最小项目创建、持久任务、租约、心跳和运行记录；
- Secret 配置规范，不把 Key 写入代码或日志。

退出标准：

- 一个启动命令能够启动 API、Worker 和 Engine MCP；
- API 与 Worker 能同时运行；
- 核心模块不依赖外部 SDK；
- 任务先持久化再由 Worker 领取；
- Worker 退出后租约可以过期；
- 中断后运行记录仍存在；
- 未实现任何假设功能平台。

## 3. 切片 1：系统健康与上传提交门禁

实现：

- FFmpeg/ffprobe 健康检查；
- FunASR 本地模型目录、加载和标准音频健康检查；
- OpenAI-compatible 多模态端点检查；
- 使用配置模型 `gemini-3.5-flash` 实测文本、图片、WAV、带声音 MP4 和结构化输出；
- Codex App Server 与 Engine MCP 启动检查；
- Resolve、Nextcloud 根目录和本地缓存可用性检查；
- Web 上传 staging；
- `uploading / validating / ready / invalid`；
- 内容哈希、ffprobe、解码扫描和稳定工作副本；
- Web 错误、警告、替换和移除操作；
- “开始制作”提交门禁；
- 提交时重新检查哈希并冻结输入。

退出标准：

- 破损、零时长和缺少必需媒体流的文件不能提交；
- 可修复的 VFR 或编码问题生成工作副本后可以提交；
- `uploading`、`validating` 或 `invalid` 会禁用按钮；
- 错误包含可操作原因和时间范围；
- 上传校验结果可供后续媒体证据链复用；
- 系统只启用经过实测的多模态能力，不根据模型名称虚报。

## 4. 切片 2：自研 Engine MCP 的安全 Resolve 闭环

实现：

- Windows Resolve Bootstrap；
- `engine_status`、`inspect_resolve`；
- 最小 ResolveExecutionPlan；
- validate、preview、execute；
- 创建隔离项目/时间线；
- 导入媒体、插入一个视频和音频；
- 写后读回；
- render、inspect、verify；
- operation id 和 Engine Journal；
- Worker 的 Resolve 单写者租约。

退出标准：

- 在 Resolve 21 实际创建时间线并渲染有声视频；
- 响应 `success` 与实际读回一致；
- 重复 operation id 不重复写入；
- 模拟超时后可以只读对账；
- API 进程不直接加载 Resolve 原生模块。

## 5. 切片 3：媒体证据最小链路

实现：

- 复用上传阶段的内容哈希、技术探测和稳定工作副本；
- 分析代理和音频提取；
- FunASR 中文转写、VAD、标点和可选说话人；
- 镜头检测；
- 概览抽帧和联系表；
- Beat/Onset、响度和静音分析；
- `OpenAICompatibleMultimodalAdapter`；
- 根据能力探测选择直接音视频或显式降级模式；
- Evidence Bundle 和缓存；
- 快速动作 dense window。

退出标准：

- 同一短素材能生成可回链转写、镜头、图片、音频和多模态证据；
- FunASR 使用项目本地模型，不自动下载；
- 相同素材再次运行命中缓存；
- 快速动作区间能够生成密集窗口；
- 未授权云端分析时不会上传；
- 第三方反代不支持视频时，系统不会声称已完成直接音视频理解。

## 6. 切片 4：素材理解 Skill

实现：

- `video-project-mcp` 只读查询；
- Codex App Server Thread 和 turn 调用；
- `video-source-understanding`；
- SourceUnderstanding Schema；
- 证据缺口请求与一次补充循环；
- Skill 评测素材。

退出标准：

- 关键语义单元有真实时间范围；
- 不把镜头或句号直接当语义边界；
- 快速动作在证据不足时会请求补充；
- 输出不能包含 Resolve 操作；
- Skill 不声称自己逐帧看过未提供的原始视频。

## 7. 切片 5：创意素材库、目录与检索

实现：

- 配置原始采购库 `C:\Users\13222\Nextcloud\达芬奇素材`；
- 新建并配置认证素材库；
- 工程根目录内的 Testing 工作区和 `workspace/creative-cache/` 内容寻址缓存；
- `workspace/data/creative_catalog.db`；
- 原子能力登记、内容哈希、认证状态和预览引用；
- 结构化硬过滤；
- SQLite FTS5；
- `SemanticIndexPort` 合同；
- 首批真实素材的检索评测，决定是否在本切片启用本地向量后端；
- 候选限制与真实预览；
- Nextcloud 占位检测、本地化、哈希校验和缓存锁；
- Catalog 和索引重建。

退出标准：

- SQLite 不保存媒体和模型 BLOB；
- 原始采购包不会自动进入候选；
- 至少一个素材包拆分为三个原子能力；
- FTS 可以按功能、场景和风格召回；
- 硬约束不会被文本或向量相似度绕过；
- Skill 只看到 5～10 个候选及预览；
- 在线占位文件不会直接交给 Resolve；
- 索引可从认证素材库重建。

## 8. 切片 6：专业计划与最小内部成片

实现：

- `video-edit-director` direction/finalize；
- 声音、视觉、文字 Skill 的条件调用；
- EditPlan 最小 Schema；
- Creative Resolver 混合查询；
- 专业 Skill 比较候选；
- CapabilityBinding 和本地化；
- 编译为视频片段、原声、BGM、简单字幕/标题或基础视觉处理；
- 内部工作时间线和工作渲染。

退出标准：

- 一个真实项目从 Web 输入到内部工作版；
- 未激活的 Skill 不生成空计划；
- Skills 选择可回链素材和真实能力候选；
- 编译器拒绝未支持操作；
- Resolve 只使用经过校验的本地缓存资源。

## 9. 切片 7：已验证 Creative Adapter

优先迁入已经实测的能力：

- MP3/WAV；
- MP4/MOV/PNG；
- LUT；
- 受约束字体；
- 单输入 Fusion 效果；
- 静态重构图。

实现：

- 五步认证工具；
- 标准预览；
- 项目风格套件；
- Adapter 与 Compiler Mapping；
- Golden Render 测试；
- 本机部署库存和项目预检。

退出标准：

- 每类至少一个能力可发现、部署、本地化、执行、读回和渲染；
- 未认证素材不能进入计划；
- 单个资源成功不会泛化到整包；
- 清理本地缓存后能够按哈希重新本地化并重渲染。

## 10. 切片 8：收尾与用户可见 v1

实现：

- 技术 QC；
- 对工作渲染运行当前多模态 Adapter 复核；
- `video-finishing-designer`；
- 一次有界收尾调整；
- 成片候选渲染；
- Web 播放和版本发布。

退出标准：

- v1 是实际可播放视频；
- v1 包含必要声音、文字、视觉和基础收尾；
- 发布后文件和时间线不可覆盖；
- 用户能在时间码上反馈。

## 11. 切片 9：反馈与真实 v2

实现：

- 反馈绑定 v1；
- Codex 原线程继续；
- `video-edit-director` revise；
- 受影响专业 Skill 条件重跑；
- 新 EditPlan、新时间线和 v2；
- 版本比较和修改说明。

退出标准：

- v1 不变；
- v2 解决真实反馈；
- “其他不要动”得到保护；
- 必要连带变化被披露。

## 12. 切片 10：中断、恢复与结果未知

实现：

- 每个 Step 检查点；
- Task lease 与 heartbeat；
- API、Worker、Codex、多模态端点和 Engine MCP 故障模拟；
- Resume；
- Resolve 写入 `outcome_unknown`；
- `reconcile_operation`；
- 单写者租约恢复；
- Nextcloud 下载中断和本地缓存恢复。

退出标准：

- 任一步骤崩溃后不丢项目和历史视频；
- 已完成分析不重复；
- 租约过期后不会无条件从头执行；
- Resolve 写入不会盲目重试；
- 缓存下载中断不会产生可执行的半文件；
- 恢复后能继续原运行或明确等待用户。

## 13. 切片 11：第二个真实项目检验扩展性

使用不同类型、画幅和能力组合的第二个项目验证：

- 模块边界是否真的局部；
- 新能力是否只需 Adapter、Catalog、Binding、Compiler Mapping 和测试；
- 多模态证据是否覆盖非口播内容；
- Skills 是否出现内容重叠或职责缺口；
- SQLite、FTS、可选向量索引、Nextcloud 和本地缓存是否足够；
- 一个 Python 3.10.20 环境是否存在真实依赖冲突。

只有真实重复问题出现后，才增加新的服务、状态、对象、向量后端或 Skill。
