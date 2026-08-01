# DavinciMcp 文档基线

本目录是新工程的产品、架构、实施与专业 Skills 基线。

## 阅读顺序

0. `AGENTS.md`：开发原则、语言要求、范围控制和完成后汇报；
1. `docs/PRODUCT.md`：产品目标、用户流程、范围和验收；
2. `docs/ARCHITECTURE.md`：模块、进程、数据、任务和扩展边界；
3. `docs/CREATIVE_LIBRARY.md`：Nextcloud 创意素材库、目录、检索与本地缓存；
4. `docs/MEDIA_INTELLIGENCE.md`：上传校验、FunASR、多模态证据和素材理解实现；
5. `docs/DAVINCI_ENGINE_MCP.md`：自研 Resolve 执行 MCP；
6. `.agents/skills/*/SKILL.md`：各专业任务的判断方法与交接；
7. `docs/IMPLEMENTATION_PLAN.md`：本次开发的纵向切片；
8. `docs/ROADMAP.md`：明确不属于本次开发范围的后续方向。

## 权威范围

- 开发过程与交付说明以 `AGENTS.md` 为准；
- 产品需求以 `docs/PRODUCT.md` 为准；
- 系统实现边界以 `docs/ARCHITECTURE.md` 为准；
- 创意素材管理与检索以 `docs/CREATIVE_LIBRARY.md` 为准；
- 媒体分析与素材理解输入以 `docs/MEDIA_INTELLIGENCE.md` 为准；
- Resolve 执行合同以 `docs/DAVINCI_ENGINE_MCP.md` 为准；
- 专业判断方法以对应 `SKILL.md` 为准；
- 精确字段、枚举和接口参数以代码 Schema 与自动生成接口文档为准。

文档之间出现冲突时，不自行折中：先按以上权威范围定位责任，再向产品负责人确认。

## 本机配置

- `.env.example`：多模态反代、Nextcloud、缓存和工作区配置示例；数据库、项目文件和创意缓存默认统一放在 `./workspace/`；
- `models/manifest.example.yaml`：FunASR 本地模型配置示例；

真实密钥、模型权重、数据库、工作区和本地缓存不得提交 Git。
