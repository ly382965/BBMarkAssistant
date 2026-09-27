# BBMarkAssistant

[简体中文](README.md) | **English**

A Windows desktop assistant for reviewing Blackboard homework, built with Python 3.12 and PySide6. It connects to USTC Blackboard through `pyustc`, extracts submissions with MinerU, and uses the DeepSeek API to suggest grades under the teacher's instructions. Teachers review the results before submitting them to Blackboard.

The desktop interface is currently in Chinese.

## Workflow

**pyustc → Blackboard submissions → MinerU OCR (local / API) → DeepSeek API + teacher system prompt → human review → Blackboard upload and readback**

1. **Connect and download.** Sign in through USTC CAS using `pyustc`, synchronize the course roster and assignments, and download submissions within the grading scope. The roster shows student IDs and names.
2. **Extract the homework.** Run MinerU locally or use a configured OCR API. Extracted text and formulas are retained for inspection and reuse.
3. **Generate suggested grades.** Provide a maximum score (default: **10**), grading rubric, and reference answer. These form the teacher's **system prompt**; the student's OCR text is sent separately as user content. The model returns a suggested score, brief comment, rationale, and uncertainties.
4. **Review manually.** Compare the original attachment with OCR and grading evidence, edit the score and final comment, and approve each submission. Final comments may be empty.
5. **Submit the approved results.** Preview the student list, attempt versions, grades, and comments, then confirm upload. The app checks that each attempt is still current and reads the result back from Blackboard after writing.

## Quick start on Windows

Install [uv](https://docs.astral.sh/uv/) and Git, then run in PowerShell:

```powershell
git clone https://github.com/ly382965/BBMarkAssistant.git
cd BBMarkAssistant
.\run.ps1 --demo
```

The launcher creates a project-local Python runtime in `.runtime/` and an application environment in `.appvenv/`. Demo mode uses fictional records in a separate data directory and cannot connect to a real course or upload grades.

Start the application for a real course:

```powershell
.\run.ps1
```

In **连接与设置** (Connection and settings):

- Paste your Blackboard course URL and enter your CAS username.
- Enter the CAS password, or leave the password field empty to use **`USTC_CAS_PWD`** from the application's environment. A typed password takes precedence. Restart the app after changing its environment.
- Configure MinerU and the grading API, then save the settings.
- Enter API keys in their dedicated credential fields. Optional persistence uses Windows Credential Manager; credentials are not stored in configuration JSON.

Use a separate data directory for each course:

```powershell
.\run.ps1 --data-dir D:/BBMarkData/course-a
```

The data directory is bound to a Blackboard site and course to prevent mixing records.

## MinerU: local or API

### Local installation

From the repository directory:

```powershell
.\setup_mineru.ps1
```

The script installs MinerU into `.mineru-venv/`, downloads and verifies the Standard models under `.mineru/`, and configures the local command using absolute paths. It backs up existing settings and preserves unrelated options. Restart the application after setup. Model download requires network access; the configured local parser uses the downloaded models.

For another model download source or a custom app data directory:

```powershell
.\setup_mineru.ps1 -ModelSource huggingface
.\setup_mineru.ps1 -AppData D:/BBMarkData/course-a
```

The default setup updates both the source application's `data/` directory and the packaged application's data directory. `-AppData` selects a custom target instead. Models are not bundled with the desktop app; actual OCR performance depends on the machine and the document.

Local commands are JSON argument arrays, executed directly without a shell. `{input}` and `{output}` are replaced for each attachment. Use the settings button **使用已安装的本地 MinerU** to select a discovered local installation.

### API options

Select the OCR mode in settings. Additional options are available in the advanced configuration JSON.

| Mode | Configuration |
|---|---|
| Local command | `ocr.mode=command`; executable and arguments in `command` |
| MinerU V1 | `ocr.mode=mineru_v1`; server base URL in `endpoint`; upload, polling, and download handled by the app |
| Legacy MinerU FastAPI | `ocr.mode=http`, `protocol=legacy_file_parse`; full `/file_parse` endpoint |
| Generic OCR service | `ocr.mode=http`, `protocol=generic`; multipart upload with a JSON response containing Markdown or text |

OCR timeouts, authentication header/scheme, and extra request parameters are configurable. Remote OCR sends attachments to the selected service; local OCR does not send them to a remote OCR service.

Supported inputs include PDF, common images, DOCX, UTF-8 text/Markdown, and ZIP archives containing supported files. DOCX processing combines native text, tables, formulas, and OCR of embedded images. Certain unsupported embedded objects or external content require export to PDF. Incomplete extraction produces an error rather than silently grading only part of the submission.

## Grading and review

In **评分标准** (Grading criteria), save the maximum score, rubric, and reference answer for the selected assignment. The grading API base URL, model, API key, temperature, token limit, input limit, timeout, and extra request options are configurable. DeepSeek is the default; compatible chat-completions services can also be configured.

- **Incremental OCR:** **② OCR** processes submissions without existing OCR text. To redo extraction, explicitly select **本次重新识别已有结果**; this also clears outdated suggested grades for affected submissions.
- **Regrade after prompt changes:** **③ 重新评分** uses the current criteria on each eligible unreviewed submission and reuses its OCR text. Saving criteria alone does not call the grading API.
- **Latest attempt only:** when a student submits multiple attempts, the current list and default processing use the latest attempt. Older attempts remain in local history. Upload checks Blackboard again for a newer attempt.
- **Protected review state:** approved, uploaded, and unresolved upload records are not overwritten by normal processing. Revoke approval before regrading an approved submission.
- **Optional deterministic scoring:** enable error-count scoring, select questions or subquestions, and set the free-error allowance and deduction. The model identifies errors; the app calculates the base score. For example, maximum 10, one free error, and a 0.5 deduction gives `max(0, 10 - max(0, errors - 1) * 0.5)`. Apply additional adjustments, such as late penalties, during review.

OCR, formulas, handwriting, and the model's error judgments still need human review. Empty text, oversized input, malformed responses, and invalid scores stop processing rather than silently truncating homework or assigning zero.

## Upload behavior and Blackboard scope

Only explicitly approved results can be uploaded. Empty final comments stay empty. The configured maximum score must agree with the Blackboard assignment's scale.

Uploads run one at a time and are not automatically retried. A failure known to occur before sending leaves the record approved. A timeout or mismatched readback after sending marks the record **上传待核验** (upload needs verification) and stops the batch. Check Blackboard before resolving that state or retrying.

The integration targets **USTC Blackboard Original individual assignments**, including its grade-center and inline-grading forms. REST options are also available where institution permissions allow them. Group assignments, anonymous grading, and unfamiliar forms are not universally supported. Student selection currently includes standard IDs starting with `PB23` or `PB24`, plus `PB25000001`–`PB25000262` inclusive. This project-specific rule is in [`storage.py`](src/bb_assistant/storage.py); adapt it and the corresponding sidebar label for another grading scope. The default course URL is an example and must be replaced with your actual course URL.

## Data and credentials

| Item | Location or destination |
|---|---|
| Source app data | Repository `data/`, or `--data-dir` |
| Packaged app data | `%LOCALAPPDATA%/BBMarkAssistant`, or `--data-dir` |
| Records and audit events | `assistant.sqlite3` within the data directory |
| Original attachments and OCR | `submissions/` within the data directory |
| Batch reports and exports | `reports/`; JSON and CSV exports are available |
| Non-secret configuration | `settings.json` |
| Persisted API credentials | Windows Credential Manager |
| Grading API input | OCR text, teacher rubric, reference answer, and maximum score |
| Blackboard writeback | Approved scores and final comments |

The CAS password is used for login and is not saved in settings. Close the app before backing up the complete data directory. Keep coursework, rosters, grades, credentials, models, and local diagnostic files out of public repositories.

## Development and packaging

After the first source launch:

```powershell
uv pip install --python .appvenv/Scripts/python.exe -e '.[dev]'
.appvenv/Scripts/python.exe -m pytest -q
.appvenv/Scripts/ruff.exe check src tests scripts
.\build.ps1
```

The Windows build is written to `dist/BBMarkAssistant/`. Keep the entire directory together and launch `BBMarkAssistant.exe`; the executable is not a standalone single-file distribution. The application runtime is included, while MinerU and its models need separate setup.

For a demo startup screenshot that exits automatically:

```powershell
.\run.ps1 --demo --smoke-test artifacts/demo.png
```
