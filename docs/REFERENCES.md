# 技术参考

本文件只记录实现依据，不承担产品和架构权威职责。

## Codex

- Codex App Server：线程创建/恢复、turn、事件流、本地图片和结构化输出。  
  https://developers.openai.com/codex/app-server

## 多模态反代

- 当前通过 OpenAI-compatible 接口调用，配置模型 ID 为 `gemini-3.5-flash`；该 ID 是当前反代可用路由名，不要求与上游官方 ID 相同。
- 业务能力以本机启动健康检查为准：必须分别实测文本、图片、音频、带声音视频和结构化输出。
- Base URL、API Key 和模型名只从本机配置读取，不进入文档、日志或 Git。
- OpenAI Python SDK/API 形式参考：  
  https://platform.openai.com/docs/api-reference/chat


## 媒体工具

- FunASR：ASR、VAD、标点、说话人和时间戳。  
  https://github.com/modelscope/FunASR
- FFmpeg/ffprobe：媒体处理、探测和解码检查。  
  https://ffmpeg.org/ffprobe.html
- PySceneDetect：镜头边界 Detector。  
  https://www.scenedetect.com/docs/latest/

## 本地目录和检索

- SQLite WAL：业务数据库和本地 Catalog 的并发基础。  
  https://www.sqlite.org/wal.html
- SQLite FTS5：创意素材全文检索。  
  https://www.sqlite.org/fts5.html
- 语义索引通过 `SemanticIndexPort` 接入；具体后端只有通过本机 Windows、重建和检索评测后才能确定。

## DaVinci Resolve

实际 Scripting API、Fusion 和 Resolve 版本行为，以本机安装目录中的 Blackmagic Design Developer/Scripting 文档和实机测试为准。
