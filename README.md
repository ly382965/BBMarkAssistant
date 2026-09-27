# BBMarkAssistant · BB 作业批改助手

简体中文 | [English](README.en.md)

面向 Windows 的 Blackboard 作业批改桌面应用。通过 **pyustc** 登录中国科学技术大学统一身份认证，使用 **MinerU** 提取作业内容，再按教师提供的 **system prompt** 调用 **DeepSeek API** 生成建议分数和简短评语。教师逐份审核后，应用将最终结果提交到 BB 并回读核验。

## 核心工作流

```text
pyustc 统一身份认证 → Blackboard 同步课程、学生与作业
                              ↓
                    下载每名学生的最新提交
                              ↓
                  MinerU OCR（本地命令 / API）
                              ↓
         教师评分标准 + 参考答案 + 满分 → system prompt
                    OCR 正文 → 学生答案输入
                              ↓
             DeepSeek API → 建议分数、评语、依据与疑点
                              ↓
                  人工检查、修改并确认审核
                              ↓
                  提交 BB → 回读分数和评语
```

1. **接入 BB**：使用 `pyustc` 完成 CAS 登录，同步当前课程的学号—姓名名单、作业和提交记录。
2. **下载与 OCR**：每名学生默认取最新一次尝试，下载附件；选择本地 MinerU 命令或 OCR API，将正文、公式和图片内容整理成文本。
3. **按教师规则评分**：满分（默认 **10**）、评分标准和参考答案进入 system prompt；学生 OCR 内容作为独立输入。DeepSeek 返回建议分数、简短评语、评分依据和待检查项。
4. **人工审核**：对照原附件、OCR 和评分依据，修改最终分数及评语，逐份确认。**最终评语可以为空**。
5. **提交最终结果**：预览审核名单后上传。应用再次核对最新尝试、课程与满分，逐份提交并回读确认。

评分 API 的输出是建议值，只有人工审核后的结果可以回传。

## 快速开始

需要 Windows、[Git](https://git-scm.com/) 和 [uv](https://docs.astral.sh/uv/)。启动脚本会安装项目内的 Python 3.12 与应用依赖。

```powershell
git clone https://github.com/ly382965/BBMarkAssistant.git
cd BBMarkAssistant

# 使用虚构数据体验界面，不连接 BB，也不上传成绩
.\run.ps1 --demo

# 正常启动
.\run.ps1
```

也可以单独指定课程数据目录：

```powershell
.\run.ps1 --data-dir D:/BBData/course-a
```

每个数据目录绑定一个 BB 站点和课程。切换课程时使用不同目录。

## 首次配置与使用

1. 在“连接与设置”粘贴自己的 **BB 课程 URL**。仓库默认地址只是示例，不能代替实际课程地址。
2. 填写统一认证账号。密码可在界面输入；密码框留空时读取环境变量 **`USTC_CAS_PWD`**。手填密码优先，应用不将密码保存到配置文件；修改环境变量后重新启动应用。
3. 配置 MinerU 和 DeepSeek。服务地址、模型、API Key、超时等均可调整；API Key 可选择保存在 Windows 凭据管理器。
4. 登录后选择作业并“同步提交”，到“评分标准”填写满分、评分规则和参考答案。
5. 使用“① 下载”“② OCR”“③ 重新评分”，或“一键处理未审核作业”。
6. 检查每份作业，修改最终分数和可选评语，点击“确认此份审核”。
7. 点击“预览并上传已审核成绩”，核对名单、提交版本和最终结果后提交。

当前负责学生范围为 `PB23*`、`PB24*`，以及 `PB25000001`–`PB25000262`（含两端），并要求完整的标准学号格式。完整课程名单仍可查看。这是本项目的默认规则；用于其他班级时，请修改 [storage.py](src/bb_assistant/storage.py) 的 `is_in_scope()`，并同步界面中的范围说明。

## MinerU：本地或 API

### 本地命令模式

在项目目录运行：

```powershell
.\setup_mineru.ps1

# 可选：指定模型来源或自定义数据目录
.\setup_mineru.ps1 -ModelSource huggingface -AppData D:/BBData/course-a
```

脚本创建独立的 `.mineru-venv/`，下载并验证 Standard 模型，将模型与缓存放在 `.mineru/`，然后配置应用的本地命令绝对路径。安装和模型下载需要网络；应用与源码仓库不捆绑模型。

默认安装器会配置项目 `data/` 和 `%LOCALAPPDATA%/BBMarkAssistant`；传入 `-AppData` 时只配置指定目录。安装完成后重启应用，必要时点击“使用已安装的本地 MinerU”。

本地 OCR 直接调用命令，无需启动 HTTP 服务。自定义命令使用 JSON 参数数组，支持 `{input}`、`{output}` 占位符，不经过 shell。

### API 模式

已有 OCR 服务时，在设置中选择对应协议并填写地址、认证与超时：

| 服务 | 配置方式 |
| --- | --- |
| MinerU 4 V1 | `ocr.mode=mineru_v1`，填写服务根地址；应用处理上传、轮询和结果下载 |
| MinerU 旧版 FastAPI | `ocr.mode=http`、`protocol=legacy_file_parse`，填写 `/file_parse` 地址 |
| 通用 OCR 服务 | `ocr.mode=http`、`protocol=generic`，multipart 上传，返回 markdown/text |
| DeepSeek / 兼容评分服务 | 配置 `grading.base_url`、`model`、API Key、输入/输出限制与超时 |
| Blackboard | 配置课程 URL、连接模式、超时与高级端点；REST 模式需相应授权 |

高级 JSON 可配置 OCR 额外表单参数、认证请求头以及评分 API 的 `extra_body`。凭据使用专用输入框，不放进高级 JSON。

## 评分规则与重复处理

评分标准按作业保存。教师提供满分、评分规则和参考答案，应用将它们与结构化输出约束组成 system prompt；学生答案不作为教师指令执行。

- **增量 OCR**：默认只识别没有 OCR 正文的未审核提交。需要重做时勾选“本次重新识别已有结果”。
- **每次重新评分**：点击“③ 重新评分”会使用当前规则重新计算未审核提交的建议分，复用已有 OCR，适合修改 prompt 后重试。
- **保护审核结果**：已审核记录须先撤销审核，才能重新 OCR 或评分；已上传及上传待核验记录不会被批次覆盖。
- **最新尝试优先**：同一学生多次提交时，只显示并处理最新尝试，旧记录保留在本地。

对于“错一个小问仍给满分”这样的规则，可以启用“按错题数计分”，选择大题或小问，设置免扣题数与每题扣分。模型只给出错题与依据，程序按公式计算基础分。例如满分 10、免扣 1、每题扣 0.5：错 0–1 个小问得 10 分，错 2 个得 9.5 分。迟交等额外调整在人工审核时完成。

## 附件与上传行为

支持 PDF、常见图片、DOCX、UTF-8 TXT/Markdown，以及包含受支持文件的 ZIP。DOCX 处理正文、表格、Word 原生公式和内嵌图片；可验证的旧版 Equation Editor/MathType 公式优先解析为 LaTeX，必要时 OCR 本地预览。未知嵌入内容或无法完整识别的文档会报错，可导出为 PDF 后重试。

附件失败或内容过长时，不会静默漏读、截断或补零分。模型仍可能误判，需人工核对原文与识别结果。

上传仅针对最新提交的人工审核结果，保留空评语。写入前校验不通过时显示“未发送成绩”，审核结果保留；发送后超时、回读失败或不一致时标记“上传待核验”并停止批次。先到 BB 核对，再通过“上传异常人工核验”记录结果，程序不会自动重复写入。

当前重点适配 USTC Blackboard Original 的个人作业。其他学校或 BB 版本、分组作业、匿名评分等可能需要额外适配。

## 数据存储

源码版默认使用项目 `data/`；打包版默认使用 `%LOCALAPPDATA%/BBMarkAssistant`；演示模式使用独立的 `demo/` 子目录。数据库保存名单、处理结果、审核状态和上传审计，附件、OCR 输出与报告保存在同一数据目录。支持导出 CSV 和 JSON。

本地 OCR 在本机处理附件；使用 OCR API 时会向配置的服务发送附件，使用评分 API 时会发送 OCR 正文、规则和参考答案。学生数据、设置、凭据、模型及本地诊断文件均不属于公开仓库内容。

## 开发与打包

首次运行 `run.ps1` 创建应用环境后：

```powershell
uv pip install --python .appvenv/Scripts/python.exe -e '.[dev]'
.\.appvenv\Scripts\python.exe -m pytest -q
.\.appvenv\Scripts\python.exe -m ruff check src tests scripts
.\build.ps1
```

输出为 `dist/BBMarkAssistant/BBMarkAssistant.exe`。分发时保留整个 `BBMarkAssistant` 目录，不要只复制 exe。OCR 模型需另行安装或使用 API。

测试使用合成数据和模拟接口，不需要真实账号或 API Key，也不会向 BB 写入成绩。
