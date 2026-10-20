# Note2LaTeX Agent

数学笔记图片 / PDF → 视觉 API 转录 → 有界工具调用 Agent 校对 → Markdown、HTML、LaTeX。

这是重建后的独立工程，不再包含 DocLayout-YOLO、Torch、本地权重或旧站点代码。
参考 [course2md 中文说明](https://github.com/mizorewww/course2md/blob/main/readme.zh.md)
和 [设计文档](https://github.com/mizorewww/course2md/blob/main/docs/DESIGN.md) 的引擎分层、
阶段化处理、保留原始结果及版本管理思路；没有复制它的视频流水线或源码。

## 当前能力与边界

- JPEG / PNG / WebP、多图、PDF 输入；每文件最多 25 MB，每任务最多 12 页。
- 中文和公式由远端视觉模型识别，不需要本地训练、GPU、Torch。
- 稳定块编号、逐页检查点、取消、断点恢复、事件日志。
- Agent 使用 read_block / replace_block / finish_review；禁止任意代码执行。
- 用户反馈指定块编号和版本，局部修改不得更改其他块、位置或顺序；新版本成功后才发布。
- 支持几何图、多边形、弧线、坐标轴及虚线辅助线输出 SVG 和原生 TikZ，不把原照片嵌入输出。
- 支持定义、定理、引理、推论、例题、证明等数学环境盒式排版，与全文公式统一连续编号。
- 复杂图形、模糊手写不保证识别准确；无法识别时标记待核对，不生成假占位图。
- 保留阅读顺序及图文块关系，非照片像素级布局复刻；目前不包含上传圈选/裁剪网页。
- notes / article / homework 三个基础模板目前区别主要为页边距。
- HTML 安全展示文字、矢量图和公式源码，尚未集成数学排版器。
- PDF 编译为可选 Docker 隔离功能；默认只生成 Markdown / HTML / LaTeX 和结构化 JSON。

## 环境配置

Python 3.11+，开发环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\note2latex doctor
```

配置字段见 `.env.example`，运行时从 `.env` 或环境变量读取。
`API_BASE_URL` 为 OpenAI-compatible Chat Completions 根地址（通常以 `/v1` 结尾）；
程序追加 `/chat/completions`。不绑定任何特定供应商。

`VISION_MODEL` 必须支持图像输入，`AGENT_MODEL` 必须同时支持图像输入和工具调用，
可以使用同一个模型。只有文本能力的模型无法直接识别手写照片。
`API_KEY` 不得提交版本库；本项目不继承历史聊天中的任何密钥。
配置字段存在并不代表供应商一定兼容，真实接口需要单独验证。
上传笔记会发送至配置的 API 供应商，请确认其数据处理条款。

## 使用入口

```powershell
.\.venv\Scripts\note2latex serve
.\.venv\Scripts\note2latex convert .\example.png --title "数学笔记" --template notes
.\.venv\Scripts\python -m pytest -q
```

本地 API 默认 `http://127.0.0.1:8000`，交互接口说明 `/docs`。
当前为 API + CLI 工程，不是已部署的公网网站。

接口：
- `POST /api/jobs`：multipart `files`、`title`、`template`。
- `GET /api/jobs`、`GET /api/jobs/{id}`：任务列表与状态。
- `GET /api/jobs/{id}/events`：阶段与 Agent 操作记录。
- `GET /api/jobs/{id}/document`：读取块编号和结构化结果。
- `POST /api/jobs/{id}/refine`：JSON 包含 `message`、`target`、`revision`。
- `POST /api/jobs/{id}/cancel`、`POST /api/jobs/{id}/resume`：取消与恢复。
- `GET /api/jobs/{id}/download/{filename}`：note.md、note.html、note.tex、note.pdf、bundle.zip。

校对失败时保留原始转录并标记 `needs_review`；局部修改失败保留上一版。
PDF 未启用时下载 PDF 返回 404，不冒充已编译成功。
Markdown 引用的 SVG 在 bundle.zip 中，单独下载 Markdown 不包含资源。

## 工程结构

```text
note2latex/
  config.py       环境配置
  ingest.py       图片规范化、PDF 按页渲染
  provider.py     兼容 API、超时、有限重试、错误脱敏
  schema.py       内容块、数学白名单、向量图元校验
  agent.py        转录提示词、工具校对、局部修改边界
  store.py        SQLite 状态、事件记录
  engine.py       检查点、任务恢复、原子版本发布
  render.py       Markdown / HTML / LaTeX / SVG / TikZ
  api.py          FastAPI
  cli.py          doctor / serve / convert
compiler/         可选隔离 XeLaTeX 镜像
tests/            不依赖真实 API 的回归测试
```

结果位于 `data/jobs/{id}/versions/{revision}/`；原始转录 `raw.json`，
逐页识别结果 `checkpoints/`，原照片规范化副本 `sources/`。
数据默认留在本地，不自动清理。任务日志不存 API 密钥，但包含文档编辑理由。

## 可选 PDF

```powershell
docker build -t note2latex-tex:local compiler
```

启用 `COMPILE_PDF=true` 后，使用无网络、只读根文件系统、资源限制、禁用
shell-escape 的容器编译 XeLaTeX。容器只挂载临时导出目录，不挂载源照片或密钥。
没有 Docker 或编译失败会将任务标记失败，保留检查点及上一版。
模型输出仍需审核，隔离和命令白名单不是对所有恶意 TeX 的形式化安全证明。

## 部署约束

当前定位单用户本地服务，只允许一个服务进程操作同一数据目录；
不要同时运行 CLI 转换和 API 服务处理同一数据目录。
重启时未完成任务变为 interrupted，显式恢复后复用检查点。
取消在 API 调用之间生效，不能立即中止正在等待的远端请求。

非回环地址启动必须配置 `SERVICE_TOKEN`，接口使用 `Authorization: Bearer ...`。
生产公网部署还需 TLS、反向代理请求体限制（建议 50 MB）、限流、用户级权限、
磁盘配额及沙箱加固；当前上传大小检查发生在 multipart 解析之后，不能代替网关限制。
不要直接将默认开发服务暴露公网。

后续优先使用有代表性的中文手写公式样本建立准确率评测，再优化提示词、
分块策略和图形表达；不要把模拟测试通过理解为真实 OCR 准确率已验证。
