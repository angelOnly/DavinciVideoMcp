---
status: development-note
updated: 2026-08-03
scope: creative-adapter-development
---

# 创意素材 Adapter 开发说明

## 当前结论

本次同步已经把创意能力的受管本地化、Catalog 认证证据门禁，以及 Engine 内按机制拆分的 Adapter 放入正式项目结构。代码存在不代表素材已经通过认证：当前 Catalog 的 `certified` 数仍为 **0**，没有任何资源因此自动进入项目或被声明可用。

旧工作区中把直接媒体、字体、LUT 与 Fusion 测试集中在单一认证脚本的做法，没有整体复制到本项目。这里改为遵守当前架构的边界：

```text
认证素材库
  → Creative Catalog / CapabilityBinding
  → 内容寻址本地缓存
  → Execution Compiler
  → davinci-engine-mcp 内的机制 Adapter
  → Resolve 写入、读回、真实渲染
```

Web、Skill 和 Catalog 都不能直接写 Resolve；所有写入仍由 Worker 经 Execution 和 `davinci-engine-mcp` 处理。

## 本次代码位置

- `src/davinci_app/creative/localization.py`：认证资源在交给 Resolve 前的本地化、前后哈希校验和 ASCII 缓存路径；
- `src/davinci_app/creative/catalog.py`：原子能力登记、FTS 检索、五步认证证据门禁和 `CapabilityBinding`；
- `src/davinci_app/execution/professional_compiler.py`：将已绑定的直接媒体和 LUT 意图编译为精确 Engine 操作；
- `davinci-engine-mcp/src/davinci_engine/creative/adapters.py`：不同机制的 Adapter 实现；
- `davinci-engine-mcp/src/davinci_engine/creative/preflight.py`：管理员可运行的只读静态预检命令；
- `davinci-engine-mcp/src/davinci_engine/execution/plan.py`、`validator.py`、`executor.py`：受管创意操作的计划、校验、执行与读回。

## 当前 Adapter 范围

| 机制 | 已实现 | 当前自动执行边界 |
| --- | --- | --- |
| `audio_asset`、`video_asset`、`video_overlay`、`image_asset` | 文件技术预检、缓存哈希校验、精确轨道/帧放置、写后读回 | 可编译为直接媒体操作，但必须先对该资源完成五步认证 |
| `lut_3d` | `.cube` 行数校验、科学计数法规范化、受管部署、`SetLUT` 与读回 | 可编译到已绑定的源片段；首次部署后必须重启 Resolve 并完成实机认证 |
| `font_file` | 字体身份预检、当前 Windows 用户字体目录/注册表的受控部署、发现检查 | 尚无 Text+ 标题 Compiler Mapping，禁止自动应用 |
| `fusion_effect` | 仅静态单 Macro、单输入、单输出 `.setting` 的安全预检、受管部署、Fusion 图受限连接 | 尚未完成实机合同和 Compiler Mapping，禁止自动应用 |

以下资源继续明确拒绝：标题、生成器、双输入转场、`.drfx/.drp/.drt`、OpenFX、VST、任意脚本按钮或含外部执行入口的 Fusion 模板。它们不是“文件能打开就能用”，而是需要各自的 Adapter 和实机合同。

## 认证与预检不是一回事

管理员可先对一个本地化文件运行只读预检：

```powershell
$env:PYTHONPATH = "$PWD\src;$PWD\davinci-engine-mcp\src"
conda run --no-capture-output -n unofficial-davinci-mcp-win python -m davinci_engine.creative.preflight `
  --mechanism fusion_effect `
  --asset "E:\受管缓存\objects\<hash>\asset.setting"
```

预检只会输出 `ready_for_live_certification`，不会部署、不会写 Catalog、不会连接 Resolve。真正的 `certified` 必须由受控管理员流程保存以下五步都成功的证据：

1. 发现：文件与机制识别正确；
2. 部署：本机安装型资源部署到受管位置，或运行时媒体完成缓存本地化；
3. 执行：只通过 Execution → Engine 将其放入隔离时间线；
4. 读回：Resolve 中的路径、轨道、LUT 或 Fusion 图与计划一致；
5. 渲染：导出真实视频或音频并完成文件级、机制级验证。

任何步骤失败、Resolve 首次发现需要重启、或外部写入结果未知时，状态保持 `testing` 或转入人工处理；绝不能直接把素材标成 `certified`。

## 后续新增一种 Adapter 的固定做法

1. 先定义一个单一机制，例如 `fusion_title`，不要使用“模板”这种混合类别；
2. 在 `davinci_engine.creative.adapters` 实现 `probe`、`install_or_deploy`、`validate`、`apply`、`inspect`、`verify_render` 六个职责；
3. 为危险输入添加静态拒绝测试，为正常输入添加读回合同测试；
4. 仅在实机测试通过后，在 `ProfessionalExecutionCompiler` 增加该机制的精确 Mapping；
5. 用至少一个真实资源走完发现、部署、执行、读回、渲染，并把证据写入 Catalog；
6. 单独生成用户预览。认证用测试卡、彩条、背景或诊断输出不能充当网页预览。

如果一种资源需要多个输入、任意脚本、第三方插件界面或不稳定的 Resolve UI 操作，就应先停在 `manual_only`，而不是为了批量数量强行泛化。

## 已知代价

- 本次是正确仓库内的代码同步和机制收口，不包含对数千个文件的重新批量认证；
- 当前没有可用的 `certified` 资源，因此正式工作流仍会在能力绑定阶段安全停止；
- 字体和单输入 Fusion 已有安全部署/应用基础，但还未在这台机器上重新走完真实渲染合同，不能宣称可以自动剪辑使用；
- 大型素材、渲染和预览文件不进入 Git 或 SQLite；Git 只保存代码、测试、文档和文件引用。
