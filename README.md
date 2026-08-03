# DavinciMcp 文档基线

本目录是新工程的产品、架构、实施与专业 Skills 基线。

## 当前开发状态（重要）

- 当前 Web 仅是开发测试工作台，不是正式自动剪辑交付界面；
- `videos/` 中三条素材只用于验证受管 Resolve 写入、读回和渲染，输出只能称为**内部技术预览**；
- 内部技术预览、内部工作版和用户可见的成片候选是三类不同产物。技术预览不包含转写、多模态理解、素材理解、故事选片、字幕、音乐、音效、降噪、运镜、动效或收尾，绝不能标记为 `candidate-v1` 或 `ready_for_review`；
- 正式成片候选必须有完整媒体证据、素材理解、EditPlan、CapabilityBinding、工作版复核、收尾方案和候选验证；缺少任一项时，任务会安全停在等待条件状态，不会调用 Resolve 继续“凑成片”。Gemini 直连音视频不可用本身不是停机条件：首版改用 FunASR、本地声音测量、按需密集抽帧和 Codex 图片分析，并披露其边界；
- 当前创意库的认证能力数为 `0`。未经“发现、部署、执行、读回、渲染”认证的音乐、字体、模板、动效和其他创意资源均不可使用，也不可声明支持。

## 阅读顺序

0. `AGENTS.md`：开发原则、语言要求、范围控制和完成后汇报；
1. `docs/PRODUCT.md`：产品目标、用户流程、范围和验收；
2. `docs/ARCHITECTURE.md`：模块、进程、数据、任务和扩展边界；
3. `docs/CREATIVE_LIBRARY.md`：Nextcloud 创意素材库、目录、检索与本地缓存；
4. `docs/MEDIA_INTELLIGENCE.md`：上传校验、FunASR、多模态证据和素材理解实现；
5. `docs/DAVINCI_ENGINE_MCP.md`：自研 Resolve 执行 MCP、参考仓库、能力迁移与验收合同；
6. `docs/CREATIVE_ADAPTER_DEVELOPMENT.md`：创意能力 Adapter 的当前实现、认证边界和新增方式；
7. `.agents/skills/*/SKILL.md`：各专业任务的判断方法与交接；
8. `docs/IMPLEMENTATION_PLAN.md`：本次开发的纵向切片；

## 权威范围

- 开发过程与交付说明以 `AGENTS.md` 为准；
- 产品需求以 `docs/PRODUCT.md` 为准；
- 系统实现边界以 `docs/ARCHITECTURE.md` 为准；
- 创意素材管理与检索以 `docs/CREATIVE_LIBRARY.md` 为准；
- 媒体分析与素材理解输入以 `docs/MEDIA_INTELLIGENCE.md` 为准；
- Resolve 执行合同、外部实现参考和能力迁移边界以 `docs/DAVINCI_ENGINE_MCP.md` 为准；
- 专业判断方法以对应 `SKILL.md` 为准；
- 精确字段、枚举和接口参数以代码 Schema 与自动生成接口文档为准。

文档之间出现冲突时，不自行折中：先按以上权威范围定位责任，再向产品负责人确认。

## 本机配置

- `.env.example`：多模态反代、Nextcloud、缓存和工作区配置示例；数据库、项目文件和创意缓存默认统一放在 `./workspace/`；
- `models/manifest.example.yaml`：FunASR 本地模型配置示例；

真实密钥、模型权重、数据库、工作区和本地缓存不得提交 Git。


## 固定运行环境

本项目首期**复用现有 Conda 环境**，不得自动创建新的 Python 环境：

```text
Conda 环境名：unofficial-davinci-mcp-win
Python 版本：3.10.20
```

开发、安装依赖和启动前使用：

```powershell
conda activate unofficial-davinci-mcp-win
python --version
where python
```

API、Worker 和 `davinci-engine-mcp` 必须使用同一个解释器。未经产品负责人明确同意，不得执行 `conda create`、`python -m venv`、`uv venv`、Poetry 自动建环境或创建 `.venv`。项目采用 `src` 目录布局，`scripts/start.ps1` 会先设置必要的 `PYTHONPATH` 再调用统一启动器；手动执行模块命令时也应先设置该路径：

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\davinci-engine-mcp\src"
conda run --no-capture-output -n unofficial-davinci-mcp-win python -m davinci_app
```

启动时必须校验 `CONDA_DEFAULT_ENV`、Python 版本和 `sys.executable`；不符合时直接停止并提示激活现有环境，不能自行创建替代环境。

## 本机启动

在仓库根目录执行：

```powershell
.\scripts\start.ps1
```

该脚本只复用 `unofficial-davinci-mcp-win`，启动 API 与持久 Worker；Engine MCP 仅由 Worker 按需以 stdio 子进程启动。
