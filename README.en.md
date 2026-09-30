# BBMarkAssistant

[简体中文](README.md) | **English**

A Windows desktop assistant for reviewing Blackboard homework, built with Python 3.12 and PySide6. It connects to USTC Blackboard through `pyustc`, routes printed documents through MinerU OCR and handwritten/mixed documents to image-capable graders, and stores independent GPT and DeepSeek suggestions under the teacher's instructions. Teachers select and review results before submitting them to Blackboard.

The desktop interface is currently in Chinese.

## Workflow

**pyustc → Blackboard submissions → printed OCR / handwritten images → GPT or DS + teacher system prompt → compare and review → Blackboard upload and readback**

1. **Connect and download.** Sign in through USTC CAS using `pyustc`, synchronize the course roster and assignments, and download submissions within the grading scope. The roster shows student IDs and names.
2. **Prepare the homework.** Automatically classify page previews: printed attachments use local/API OCR; handwritten, mixed, or uncertain attachments send all original pages to the grader. Native text is read directly. Extracted content and routing decisions are cached.
3. **Generate suggested grades.** Provide a maximum score (default: **10**), grading rubric, and reference answer. These form the teacher's **system prompt**; student text/images are separate user content. **GPT 评分** and **DS 评分** independently return scores, comments, evidence, and uncertainties, with both results retained.
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
- Configure MinerU and GPT and/or DeepSeek separately, then save the settings. The explicit one-click action uses DS; individual grading buttons use their named provider.
- Enter API keys in their dedicated credential fields. Optional persistence uses Windows Credential Manager; credentials are not stored in configuration JSON.

Use a separate data directory for each course:

```powershell
.\run.ps1 --data-dir D:/BBMarkData/course-a
```

The data directory is bound to a Blackboard site and course to prevent mixing records.

## Automatic routing and independent graders

The default `recognition.mode=auto` uses the selected grader to classify every page preview. Only attachments confidently classified as entirely printed use OCR text for grading. Any handwriting, mixed content, uncertainty, or failed classification routes the entire attachment to images. DOCX retains native text, tables, formulas, and embedded images. You can explicitly select `ocr` or `vision` in settings to override classification.

Initial classification consumes a visual request; its result is cached by source and configuration. Switching GPT/DS uses each model's classification while reusing valid printed OCR. Prompt edits rerun grading, not OCR. Changed attachments invalidate their caches. The standalone preparation button uses DS for automatic classification. This is a heuristic routing decision, not a guarantee of handwriting accuracy.

GPT defaults to `gpt-5.6-sol` with `wire_api=responses`; DeepSeek defaults to `deepseek-flash` with `wire_api=chat` for fresh configurations. Existing endpoints/models are preserved and may need a vision-capable model selected. Base URLs, model names, timeouts, token limits, protocol, and non-secret custom HTTP headers are independently configurable. Use the explicit `responses_json_mode=false` compatibility option only if a Responses gateway rejects `text.format`; response JSON validation remains strict.

Keys may be supplied via `BBMARK_GPT_API_KEY` and `BBMARK_DEEPSEEK_API_KEY`; each takes precedence over its Windows Credential Manager entry. Environment keys are not automatically copied to the vault or configuration. Restart after changing environment variables. The application does not automatically use Codex credentials.

The workbench has separate score columns and evidence tabs. Select a saved suggestion through **审核采用 / 采用此结果**, edit the final grade and optional comment, then approve. Scores are never averaged automatically. A failed rerun removes that provider's outdated suggestion while preserving the other result. Rubric snapshots remain independent; the UI flags mismatches with current criteria.

## DS Actor + GPT Critic

Click **DS 初评 → GPT 复核** to start collaborative grading. It uses **DS as the actor** to draft and revise a grade, and **GPT as the critic** to check it against the student's work. This is an inference-time review loop: it does not train either model or perform reinforcement learning. Inspect each round in **协作复核** and select **协作结果** as the suggestion for manual review.

1. DS reads the current teacher criteria and prepared text/original images, then produces question-level judgments, a suggested score, and supporting reasons.
2. GPT reads the same source, criteria, and DS judgments. It checks individual questions for misreading, missed errors, or unjustified deductions and supplies evidence and revision requests. Matching total scores do not replace question-level review.
3. When revisions are needed, DS reconsiders the source and critique, then GPT checks the revised result. Unsupported critique is not an instruction to change the student's answer or blindly change the grade.
4. The app retains the initial DS result, each revision, and each GPT review for the teacher to inspect before approving the final score and comment.

Advanced configuration `actor_critic.max_revisions` limits DS revisions to `0`–`2`, default `1`. A value of `0` runs the initial grade and critique without automatic revision. Each revision adds one DS request and one GPT request; valid recognition caches remain reusable.

The collaborative result is stored separately from standalone GPT/DS suggestions. Its status is `accepted` (GPT accepted the review), `needs_human` (unresolved disagreement or human judgment needed), or `incomplete` (request failure or incomplete output). Acceptance is neither a correctness guarantee nor teacher approval. Incomplete runs cannot be adopted as completed collaborative grades, and remaining disagreements stay visible for manual review. The loop never automatically approves or uploads grades.

## MinerU: local or API

### Local installation

From the repository directory:

```powershell
.\setup_mineru.ps1
```

The script installs MinerU into `.mineru-venv/`, downloads and verifies the Standard models under `.mineru/`, and configures the local command using absolute paths with **Advanced** parsing by default. Advanced shares the Standard models and uses more inference computation. DOCX uses Flash for structural extraction, as required by MinerU, followed by OCR of embedded images. The installer backs up existing settings and preserves unrelated options. Restart the application after setup. Model download requires network access; the configured local parser uses the downloaded models.

For another model download source or a custom app data directory:

```powershell
.\setup_mineru.ps1 -ModelSource huggingface
.\setup_mineru.ps1 -AppData D:/BBMarkData/course-a
```

The default setup updates both the source application's `data/` directory and the packaged application's data directory. `-AppData` selects a custom target instead. Models are not bundled with the desktop app; actual OCR performance depends on the machine and the document.

Local commands are JSON argument arrays, executed directly without a shell. `{input}` and `{output}` are replaced for each attachment. Use the settings button **使用已安装的本地 MinerU** to select a discovered local installation.

For an existing installation, select that button and save to switch to `--tier advanced`; working custom commands are not automatically overwritten. Select **本次重新识别已有结果** before running OCR to replace existing extraction results instead of reusing them.

The `command / http / mineru_v1` selector chooses the connection method, not the parsing tier. Keep `command` for local processing, select Advanced in the separate local tier selector, then save. Recognized MinerU 4 commands are updated automatically; other custom commands remain unchanged.

For scans or mixed PDFs, forced visual OCR (`ocr`) can avoid reusing a faulty text layer; clean digital documents may retain `auto`. The optional scanned/handwritten PDF aid adds a local whole-page transcription at 300 DPI alongside the primary extraction and original page numbers. It costs additional time and can still omit or misread content. Treat disagreements as review questions, never as corrected student answers. This option requires the app-managed local MinerU installation.

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

- **Incremental OCR:** **② 识别 / OCR** reuses valid source/configuration-matched caches. Source-verified legacy OCR is reused during migration. To redo preparation, select **本次重新识别已有结果**; this also clears outdated suggestions.
- **Regrade after prompt changes:** **GPT 评分** or **DS 评分** reruns that provider on each eligible unreviewed submission using current criteria and cached inputs. Saving criteria alone does not call the grading API.
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
| Classification API input | Page previews sent to the selected GPT/DS service |
| Grading API input | Routed text/images, teacher rubric, reference answer, and maximum score |
| Blackboard writeback | Approved scores and final comments |

The CAS password is used for login and is not saved in settings. Close the app before backing up the complete data directory. Keep coursework, rosters, grades, credentials, models, and local diagnostic files out of public repositories.

Collaborative grading sends the same prepared student material and teacher criteria to both configured services. It also sends DS judgments to GPT and GPT review feedback to DS.

## Development and packaging

After the first source launch:

```powershell
uv pip install --python .appvenv/Scripts/python.exe -e '.[dev]'
.appvenv/Scripts/python.exe -m pytest -q
.appvenv/Scripts/ruff.exe check src tests scripts
.\build.ps1
```

The Windows build is written to `dist/BBMarkAssistant/`. Keep the entire directory together and launch `BBMarkAssistant.exe`; the executable is not a standalone single-file distribution. The application runtime is included, while MinerU and its models need separate setup.

When build dependencies are already installed, use `.\build.ps1 -SkipDependencyInstall` to package offline without contacting the package index.

For a demo startup screenshot that exits automatically:

```powershell
.\run.ps1 --demo --smoke-test artifacts/demo.png
```
