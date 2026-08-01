# 开发记录

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
- 已在三组真实测试素材上完成“校验 → 工作副本 → 新时间线执行 → 读回 → Resolve 渲染 → FFmpeg 技术验证 → 发布候选”的闭环：游泳 40.00 秒（1080×1920）、猫咪 69.42 秒（1080×1920）、访谈 90.01 秒（1280×720）；三者均为 H.264/AAC、30 fps。
- 历史 `outcome_unknown` 记录保持不变；修复后使用新的运行、时间线和 operation id 完成验证，未对未知操作盲目重试。
- 自动化测试：`pytest -q` 为 `11 passed`；目前仍保留一个不影响结果的 pytest 收集警告（`TestCutCompiler` 名称以 `Test` 开头）。

## 启动

在仓库根目录执行：

```powershell
.\scripts\start.ps1
```

脚本启动 API 与 Worker，Engine MCP 由 Worker 按需启动。默认工作台地址为 `http://127.0.0.1:8787`。
