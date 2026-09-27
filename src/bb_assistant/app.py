from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from .attempts import latest_attempts
from .settings import Settings, default_data_dir
from .mineru_runtime import discover_local_command, validate_ocr_command
from .services import OcrClient
from .storage import Store
from .workflow import Workflow, seed_demo

STATUS = {
    "submitted": "待下载", "downloaded": "已下载", "ocr_done": "已识别", "graded": "待审核",
    "reviewed": "已审核", "uploading": "上传中", "uploaded": "已回传", "error": "处理失败",
    "upload_uncertain": "上传待核验",
    "bb_graded": "BB 已有成绩",
    "bb_reconciliation": "BB 待协调评分",
}
STYLE = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; color: #273548; }
QMainWindow { background: #f5f7fb; }
QWidget#sidebar { background: #10263d; }
QLabel#brand { color: white; font-size: 22px; font-weight: 700; }
QLabel#mutedSide { color: #a3b9cc; }
QListWidget#nav { background: transparent; border: none; outline: none; color: #bfd0df; }
QListWidget#nav::item { padding: 16px 18px; margin: 4px 0; border-radius: 8px; }
QListWidget#nav::item:selected { background: #244764; color: white; }
QLabel#title { font-size: 25px; font-weight: 700; color: #122c45; }
QLabel#subtitle { color: #718096; padding-bottom: 8px; }
QFrame#card { background: white; border: 1px solid #e2e8f0; border-radius: 10px; }
QLabel#metric { font-size: 28px; font-weight: 700; color: #155e75; }
QPushButton { background: white; border: 1px solid #ccd7e3; border-radius: 6px; padding: 8px 14px; }
QPushButton:hover { background: #edf5fb; border-color: #7fadc7; }
QPushButton:disabled { color: #9daaba; background: #f1f4f8; }
QPushButton#primary { background: #117b88; border: 1px solid #117b88; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #0d6873; }
QPushButton#primary:disabled { background: #aac8ce; border-color: #aac8ce; }
QLineEdit, QPlainTextEdit, QTextEdit, QDoubleSpinBox, QComboBox { background: white; border: 1px solid #cdd7e2; border-radius: 5px; padding: 7px; selection-background-color: #badde3; }
QTableWidget { background: white; alternate-background-color: #f8fafc; gridline-color: #eaf0f5; border: 1px solid #dce5ee; border-radius: 6px; }
QTableWidget::item:selected { background: #d6edf0; color: #124753; }
QHeaderView::section { background: #edf3f8; padding: 10px 7px; border: none; border-bottom: 1px solid #dce5ee; font-weight: 600; }
QTabWidget::pane { border: 1px solid #dce5ee; background: white; }
QTabBar::tab { padding: 9px 16px; background: #eef3f8; }
QTabBar::tab:selected { background: white; color: #117b88; }
QProgressBar { border: none; background: #e6edf4; height: 5px; }
QProgressBar::chunk { background: #117b88; }
"""


class Job(QObject):
    done = Signal()
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, fn, workflow):
        super().__init__()
        self.fn, self.workflow = fn, workflow

    @Slot()
    def run(self):
        self.workflow.log = self.log.emit
        try:
            self.fn()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.done.emit()


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.verticalHeader().hide()
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.verticalHeader().setDefaultSectionSize(40)
    return widget


def populate(widget, rows):
    widget.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            item.setToolTip(item.text())
            widget.setItem(i, j, item)


class MainWindow(QMainWindow):
    def __init__(self, root: Path, demo=False):
        super().__init__()
        self.settings = Settings(root)
        self.store = Store(root)
        self.workflow = Workflow(self.settings, self.store, demo)
        self.demo, self.busy = demo, False
        self._redactions = set()
        self.thread = self.job = None
        self.rows, self.students, self.assignments = [], [], []
        self._rubric_assignment = None
        if demo:
            seed_demo(self.store, root)
        self.setWindowTitle("BB 作业批改助手" + (" · 演示模式" if demo else ""))
        self.resize(1460, 940)
        self.setMinimumSize(1160, 760)
        main = QWidget()
        layout = QHBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(206)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(20, 28, 20, 22)
        brand = QLabel("BB 批改助手")
        brand.setObjectName("brand")
        side_layout.addWidget(brand)
        sub = QLabel("作业 · 识别 · 评分 · 复核")
        sub.setObjectName("mutedSide")
        side_layout.addWidget(sub)
        side_layout.addSpacing(26)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.addItems(["01   批改工作台", "02   课程学生名单", "03   评分标准", "04   连接与设置"])
        side_layout.addWidget(self.nav)
        self.connection = QLabel("演示数据 · 未连接 BB" if demo else "尚未登录 BB")
        self.connection.setObjectName("mutedSide")
        self.connection.setWordWrap(True)
        side_layout.addWidget(self.connection)
        side_layout.addSpacing(12)
        label = QLabel("PB23 / PB24\nPB25000001—PB25000262")
        label.setObjectName("mutedSide")
        side_layout.addWidget(label)
        layout.addWidget(side)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(26, 23, 26, 18)
        self.pages = QStackedWidget()
        body_layout.addWidget(self.pages)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        body_layout.addWidget(self.progress)
        foot = QHBoxLayout()
        self.status = QLabel("就绪。先配置服务与评分标准，再登录 BB 同步作业。")
        foot.addWidget(self.status, 1)
        self.cancel_button = QPushButton("停止批次")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        foot.addWidget(self.cancel_button)
        body_layout.addLayout(foot)
        layout.addWidget(body, 1)
        self.setCentralWidget(main)
        self.mutation_buttons = []
        self.build_workbench()
        self.build_students()
        self.build_rubric()
        self.build_settings()
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.refresh()
        if self.settings.ocr_repaired:
            self.log("已自动定位本地 MinerU，OCR 命令已修正。")

    def page(self, title, subtitle):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel(title)
        label.setObjectName("title")
        layout.addWidget(label)
        label = QLabel(subtitle)
        label.setObjectName("subtitle")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.pages.addWidget(page)
        return layout

    def button(self, title, callback, primary=False):
        button = QPushButton(title)
        if primary:
            button.setObjectName("primary")
        button.clicked.connect(callback)
        self.mutation_buttons.append(button)
        return button

    def build_workbench(self):
        layout = self.page("批改工作台", "下载作业 → MinerU 识别 → DeepSeek 建议评分 → 人工审核 → 回传 BB")
        if self.demo:
            banner = QLabel("演示模式：以下姓名、作业与分数均为虚构。此模式禁止连接 BB 和上传成绩。")
            banner.setStyleSheet("background:#fff0cd; color:#895a11; padding:10px; border-radius:6px")
            layout.addWidget(banner)
        metrics = QHBoxLayout()
        self.metrics = []
        for title in ["课程学生", "批改范围内", "待人工审核", "已审核 / 已回传"]:
            card = QFrame()
            card.setObjectName("card")
            cl = QVBoxLayout(card)
            cl.addWidget(QLabel(title))
            number = QLabel("0")
            number.setObjectName("metric")
            self.metrics.append(number)
            cl.addWidget(number)
            metrics.addWidget(card)
        layout.addLayout(metrics)
        pick = QHBoxLayout()
        pick.addWidget(QLabel("当前作业"))
        self.assignment_combo = QComboBox()
        self.assignment_combo.setMinimumWidth(310)
        self.assignment_combo.currentIndexChanged.connect(self.refresh_attempts)
        pick.addWidget(self.assignment_combo, 1)
        pick.addWidget(self.button("同步提交", self.fetch_attempts))
        pick.addWidget(self.button("导出报告", self.export))
        layout.addLayout(pick)
        self.attempt_policy = QLabel("每名学生默认显示并处理最新提交，旧尝试保留在本地记录中。")
        self.attempt_policy.setWordWrap(True)
        layout.addWidget(self.attempt_policy)
        actions = QHBoxLayout()
        for text, stage in [("① 下载", "download"), ("② OCR", "ocr"), ("③ 重新评分", "grade"), ("一键处理未审核作业", "all")]:
            action = self.button(text, lambda checked=False, s=stage: self.process(s), stage == "all")
            if stage == "grade":
                action.setToolTip("每次按当前评分规则、参考答案和满分重新评分所有未审核作业，复用已有 OCR。已审核记录需先撤销审核。")
            actions.addWidget(action)
        actions.addStretch()
        actions.addWidget(self.button("预览并上传已审核成绩", self.preview_upload))
        layout.addLayout(actions)
        ocr_options = QHBoxLayout()
        ocr_options.addWidget(QLabel("OCR 默认只处理尚无识别结果的作业。"))
        self.force_ocr = QCheckBox("本次重新识别已有结果")
        self.force_ocr.setToolTip("仅作用于「② OCR」；重新识别未审核作业，并清除其旧 OCR 和建议分数。执行后自动取消勾选。")
        self.mutation_buttons.append(self.force_ocr)
        ocr_options.addWidget(self.force_ocr)
        ocr_options.addStretch()
        layout.addLayout(ocr_options)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.attempt_table = table(["学号 / 提交", "姓名", "AI 分", "最终分", "状态"])
        self.attempt_table.itemSelectionChanged.connect(self.select_attempt)
        splitter.addWidget(self.attempt_table)
        panel = QWidget()
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(13, 0, 0, 0)
        self.detail_title = QLabel("选择一份作业开始审核")
        self.detail_title.setStyleSheet("font-weight:600; font-size:16px")
        pl.addWidget(self.detail_title)
        self.detail_meta = QLabel("右侧显示 OCR 原文、评分依据和可修改的最终分数。")
        self.detail_meta.setWordWrap(True)
        pl.addWidget(self.detail_meta)
        tabs = QTabWidget()
        self.ocr_text = QPlainTextEdit()
        self.ocr_text.setReadOnly(True)
        self.rationale = QPlainTextEdit()
        self.rationale.setReadOnly(True)
        self.provenance = QPlainTextEdit()
        self.provenance.setReadOnly(True)
        tabs.addTab(self.ocr_text, "OCR 原文")
        tabs.addTab(self.rationale, "评分依据 / 疑点")
        tabs.addTab(self.provenance, "处理记录")
        pl.addWidget(tabs, 1)
        form = QFormLayout()
        self.score = QDoubleSpinBox()
        self.score.setRange(0, 100000)
        self.score.setDecimals(2)
        form.addRow("最终分数", self.score)
        self.comment = QPlainTextEdit()
        self.comment.setMaximumHeight(80)
        self.comment.setPlaceholderText("可留空；填写时会随最终成绩回传给学生")
        form.addRow("最终评语（可选）", self.comment)
        pl.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(self.button("打开原始附件", self.open_attachment))
        self.approve_button = self.button("确认此份审核", self.approve, True)
        row.addWidget(self.approve_button)
        pl.addLayout(row)
        review_actions = QHBoxLayout()
        self.revoke_button = self.button("撤销此份审核", self.revoke_review)
        review_actions.addWidget(self.revoke_button)
        self.reconcile_button = self.button("上传异常人工核验", self.reconcile_upload)
        review_actions.addWidget(self.reconcile_button)
        pl.addLayout(review_actions)
        splitter.addWidget(panel)
        splitter.setSizes([540, 580])
        layout.addWidget(splitter, 1)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(94)
        self.log_box.setPlaceholderText("批次日志会显示在这里；失败作业保留原因，其他作业继续处理。")
        layout.addWidget(self.log_box)

    def build_students(self):
        layout = self.page("课程学生名单", "显示本课程完整学号—姓名对。仅范围内学生的作业进入自动处理与成绩回传。")
        row = QHBoxLayout()
        self.student_search = QLineEdit()
        self.student_search.setPlaceholderText("搜索学号或姓名")
        self.student_search.textChanged.connect(self.filter_students)
        row.addWidget(self.student_search)
        self.scope_only = QCheckBox("只看批改范围内")
        self.scope_only.toggled.connect(self.filter_students)
        row.addWidget(self.scope_only)
        row.addWidget(self.button("同步课程", lambda: self.start(self.workflow.sync)))
        layout.addLayout(row)
        self.student_table = table(["学号", "姓名", "是否在批改范围", "BB 用户 ID"])
        layout.addWidget(self.student_table, 1)

    def build_rubric(self):
        layout = self.page("评分标准", "评分规则、参考答案及满分进入 system prompt；学生 OCR 文本独立提交。每次评分会保存规则快照。")
        self.rubric_target = QLabel("当前尚未选择作业。填写的规则将用于首次选择的作业。")
        self.rubric_target.setWordWrap(True)
        layout.addWidget(self.rubric_target)
        rubric = self.settings.data["rubric"]
        row = QHBoxLayout()
        row.addWidget(QLabel("满分"))
        self.max_score = QDoubleSpinBox()
        self.max_score.setRange(0.01, 100000)
        self.max_score.setValue(float(rubric["max_score"]))
        row.addWidget(self.max_score)
        row.addStretch()
        row.addWidget(self.button("保存评分标准", self.save_rubric, True))
        layout.addLayout(row)
        scoring = QHBoxLayout()
        self.count_errors = QCheckBox("按错题数计分（程序计算基础分）")
        scoring.addWidget(self.count_errors)
        scoring.addWidget(QLabel("统计单位"))
        self.error_unit = QComboBox()
        self.error_unit.addItem("大题", "major_question")
        self.error_unit.addItem("小问", "subquestion")
        scoring.addWidget(self.error_unit)
        scoring.addWidget(QLabel("免扣分题数"))
        self.free_errors = QSpinBox()
        self.free_errors.setRange(0, 1000000)
        scoring.addWidget(self.free_errors)
        scoring.addWidget(QLabel("超出后每题扣"))
        self.error_deduction = QDoubleSpinBox()
        self.error_deduction.setRange(0, 1000000)
        self.error_deduction.setDecimals(3)
        scoring.addWidget(self.error_deduction)
        scoring.addStretch()
        layout.addLayout(scoring)
        note = QLabel("启用后，模型只判断错题，分数由上面的设置计算。迟交等额外调整在人工审核时完成。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.count_errors.toggled.connect(self.update_scoring_controls)
        self.load_scoring_policy(rubric)
        layout.addWidget(QLabel("评分标准 / 给模型的指令"))
        self.rubric_text = QPlainTextEdit(rubric["instructions"])
        self.rubric_text.setPlaceholderText("例如：共 3 题，分别占 3、3、4 分。按步骤给分。答案不足或 OCR 含糊时在疑点中说明，禁止臆造缺失内容。评语控制在 80 字内。")
        layout.addWidget(self.rubric_text, 1)
        layout.addWidget(QLabel("参考答案"))
        self.reference = QPlainTextEdit(rubric["reference_answer"])
        self.reference.setPlaceholderText("粘贴参考答案、题目与评分要点。支持纯文本和 Markdown 公式。")
        layout.addWidget(self.reference, 1)

    def update_scoring_controls(self, enabled):
        for widget in (self.error_unit, self.free_errors, self.error_deduction):
            widget.setEnabled(enabled)

    def load_scoring_policy(self, rubric):
        policy = rubric.get("scoring_policy") or {}
        self.free_errors.setValue(int(policy.get("free_errors", 1)))
        self.error_deduction.setValue(float(policy.get("deduction_per_error", 0.5)))
        self.error_unit.setCurrentIndex(max(0, self.error_unit.findData(policy.get("unit", "subquestion"))))
        enabled = policy.get("mode") == "error_count"
        self.count_errors.setChecked(enabled)
        self.update_scoring_controls(enabled)

    def build_settings(self):
        layout = self.page("连接与设置", "API 地址、模型与调用参数均可修改。密码只用于本次登录；API Key 可选保存到 Windows 凭据管理器。")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        form = QFormLayout(inner)
        form.setSpacing(12)
        data = self.settings.data
        self.course_url = QLineEdit(data["course_url"])
        form.addRow("BB 课程 URL", self.course_url)
        self.username = QLineEdit(data.get("username", ""))
        form.addRow("统一身份认证账号", self.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("留空时使用环境变量 USTC_CAS_PWD")
        form.addRow("统一身份认证密码", self.password)
        self.bb_key = QLineEdit(self.settings.secret("blackboard"))
        self.bb_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("BB REST Token（可选）", self.bb_key)
        form.addRow("", self.button("保存设置并登录 / 同步", self.login, True))
        self.ocr_mode = QComboBox()
        self.ocr_mode.addItems(["command", "http", "mineru_v1"])
        self.ocr_mode.setCurrentText(data["ocr"]["mode"])
        form.addRow("MinerU 模式", self.ocr_mode)
        self.ocr_command = QPlainTextEdit(json.dumps(data["ocr"]["command"], ensure_ascii=False))
        self.ocr_command.setMaximumHeight(74)
        form.addRow("本地命令（JSON 参数数组）", self.ocr_command)
        help_text = QLabel("command 直接调用本机 MinerU，无需启动 HTTP 服务。程序会自动寻找已安装的本地环境。\n{input} 为输入文件，{output} 为单份输出目录；也可填写自己的可执行文件绝对路径。")
        help_text.setWordWrap(True)
        form.addRow("", help_text)
        form.addRow("", self.button("使用已安装的本地 MinerU", self.use_local_mineru))
        self.ocr_status = QLabel()
        self.ocr_status.setWordWrap(True)
        form.addRow("OCR 状态", self.ocr_status)
        self.ocr_endpoint = QLineEdit(data["ocr"]["endpoint"])
        form.addRow("OCR API 地址", self.ocr_endpoint)
        self.ocr_key = QLineEdit(self.settings.secret("ocr"))
        self.ocr_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("OCR API Key（可选）", self.ocr_key)
        self.ocr_mode.currentTextChanged.connect(self.update_ocr_status)
        self.ocr_command.textChanged.connect(self.update_ocr_status)
        self.update_ocr_status()
        self.ds_url = QLineEdit(data["grading"]["base_url"])
        form.addRow("DeepSeek / 兼容 API 地址", self.ds_url)
        self.ds_model = QLineEdit(data["grading"]["model"])
        form.addRow("评分模型", self.ds_model)
        self.ds_key = QLineEdit(self.settings.secret("deepseek"))
        self.ds_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("评分 API Key", self.ds_key)
        self.remember_keys = QCheckBox("保存 API Key 到 Windows 凭据管理器（不保存登录密码）")
        self.remember_keys.setChecked(bool(self.bb_key.text() or self.ds_key.text() or self.ocr_key.text()))
        form.addRow("", self.remember_keys)
        advanced = {k: copy.deepcopy(data[k]) for k in ["bb", "ocr", "grading"]}
        for key in ["base_url", "course_id"]:
            advanced["bb"].pop(key, None)
        for key in ["mode", "command", "endpoint"]:
            advanced["ocr"].pop(key, None)
        for key in ["base_url", "model"]:
            advanced["grading"].pop(key, None)
        self.advanced = QPlainTextEdit(json.dumps(advanced, ensure_ascii=False, indent=2))
        self.advanced.setMinimumHeight(180)
        form.addRow("高级参数（JSON）", self.advanced)
        form.addRow("", QLabel("可配置超时、backend、max_tokens、temperature、extra_body、extra_params 等。\n远程 OCR 会发送附件；评分 API 会接收 OCR 作业内容。"))
        form.addRow("", self.button("保存连接设置", self.save_settings, True))
        form.addRow("数据目录", QLabel(str(self.settings.root)))
        form.addRow("", self.button("打开数据目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.settings.root)))))
        scroll.setWidget(inner)
        layout.addWidget(scroll)

    def update_ocr_status(self):
        local = self.ocr_mode.currentText() == "command"
        self.ocr_command.setEnabled(local)
        self.ocr_endpoint.setEnabled(not local)
        self.ocr_key.setEnabled(not local)
        if not local:
            self.ocr_status.setText("API 模式：请先确认 OCR 服务已经启动，或点击上方按钮切换到本地 MinerU。")
            return
        try:
            config = {"mode": "command", "command": json.loads(self.ocr_command.toPlainText())}
            command = validate_ocr_command(config)
            self.ocr_status.setText("本地命令已找到：" + command[0] + "\n此模式不使用下面的 OCR API 地址。")
        except (ValueError, TypeError):
            self.ocr_status.setText("本地命令尚未找到。已安装的用户可点击上方按钮自动填入路径。")

    def use_local_mineru(self):
        command = discover_local_command()
        if command is None:
            self.fail("尚未找到本地 MinerU。请在程序目录运行 setup_mineru.ps1 完成安装。")
            return
        try:
            extra = json.loads(self.advanced.toPlainText())
            if not isinstance(extra, dict) or not isinstance(extra.get("ocr", {}), dict):
                raise ValueError("高级参数及 ocr 参数须为 JSON 对象。")
            data = copy.deepcopy(self.settings.data)
            data["ocr"].update(mode="command", command=command)
            data["ocr"]["timeout"] = max(float(data["ocr"].get("timeout", 1800)), 1800)
            self.settings.save(data)
            extra.setdefault("ocr", {})["timeout"] = data["ocr"]["timeout"]
            self.advanced.setPlainText(json.dumps(extra, ensure_ascii=False, indent=2))
            self.ocr_mode.setCurrentText("command")
            self.ocr_command.setPlainText(json.dumps(command, ensure_ascii=False))
            self.update_ocr_status()
            self.log("已切换到本地 MinerU 并保存绝对路径。返回工作台，点击「② OCR」即可重试。")
        except (ValueError, TypeError, OSError) as exc:
            self.fail(str(exc))

    def fail(self, message):
        message = self.redact(message)
        self.log(message)
        QMessageBox.warning(self, "需要处理", message)

    def redact(self, message):
        message = str(message)
        for secret in [self.password.text(), self.ds_key.text(), self.ocr_key.text(), self.bb_key.text()]:
            if secret:
                self._redactions.add(secret)
        for name in ["ocr", "deepseek", "blackboard"]:
            value = self.settings.secret(name)
            if value:
                self._redactions.add(value)
        for secret in sorted(self._redactions, key=len, reverse=True):
            message = message.replace(secret, "[凭据已隐藏]")
        return message

    def log(self, message):
        message = self.redact(message)
        self.status.setText(message.splitlines()[0][:105])
        self.log_box.appendPlainText(message)

    def save_rubric(self, checked=False, quiet=False):
        data = copy.deepcopy(self.settings.data)
        data["rubric"] = {"max_score": self.max_score.value(), "instructions": self.rubric_text.toPlainText(), "reference_answer": self.reference.toPlainText()}
        if self.count_errors.isChecked():
            data["rubric"]["scoring_policy"] = {
                "mode": "error_count", "free_errors": self.free_errors.value(),
                "deduction_per_error": self.error_deduction.value(), "unit": self.error_unit.currentData(),
            }
        if self._rubric_assignment:
            data.setdefault("rubrics", {})[self._rubric_assignment] = copy.deepcopy(data["rubric"])
        try:
            self.settings.save(data)
            if not quiet:
                self.log("评分标准已保存。点击「③ 重新评分」按最新规则更新未审核作业的分数和评语，已有 OCR 会直接复用。")
            return True
        except Exception as exc:
            self.fail(str(exc))
            return False

    def save_settings(self, checked=False, quiet=False):
        try:
            data = copy.deepcopy(self.settings.data)
            old_identity = (data.get("username", ""), self.settings.secret("blackboard"))
            extra = json.loads(self.advanced.toPlainText())
            if not isinstance(extra, dict) or set(extra) - {"bb", "ocr", "grading"}:
                raise ValueError("高级参数只接受 bb、ocr、grading 三个配置对象。")
            for key, value in extra.items():
                if not isinstance(value, dict):
                    raise ValueError(f"{key} 必须是 JSON 对象。")
                data[key].update(value)
            command = json.loads(self.ocr_command.toPlainText())
            if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
                raise ValueError("本地命令必须是非空 JSON 字符串数组。")
            data.update(course_url=self.course_url.text().strip(), username=self.username.text().strip())
            data["ocr"].update(mode=self.ocr_mode.currentText(), command=command, endpoint=self.ocr_endpoint.text().strip())
            data["grading"].update(base_url=self.ds_url.text().strip(), model=self.ds_model.text().strip())
            old_bb = self.settings.data["bb"].copy()
            self.settings.save(data)
            self.ocr_command.setPlainText(json.dumps(self.settings.data["ocr"]["command"], ensure_ascii=False))
            self.update_ocr_status()
            self.settings.set_secret("ocr", self.ocr_key.text().strip(), self.remember_keys.isChecked())
            self.settings.set_secret("deepseek", self.ds_key.text().strip(), self.remember_keys.isChecked())
            self.settings.set_secret("blackboard", self.bb_key.text().strip(), self.remember_keys.isChecked())
            if old_bb != self.settings.data["bb"] or old_identity != (data["username"], self.bb_key.text().strip()):
                if self.workflow.client:
                    self.workflow.client.close()
                self.workflow.client = None
                self.connection.setText("设置已变更，请重新登录")
            if not quiet:
                self.log("连接设置已保存。")
            return True
        except Exception as exc:
            self.fail(str(exc))
            return False

    def login(self):
        if self.demo:
            self.fail("演示模式禁止连接真实课程。请关闭后双击普通版程序。")
            return
        username = self.username.text().strip()
        if not username:
            self.fail("请填写统一身份认证账号。")
            return
        password = self.password.text() or os.environ.get("USTC_CAS_PWD", "")
        if not password:
            self.fail("请填写统一身份认证密码，或设置环境变量 USTC_CAS_PWD 后重新启动程序。")
            return
        self._redactions.add(password)
        if self.save_settings(quiet=True):
            self.start(lambda: self.workflow.login(username, password))

    def start(self, fn):
        if self.busy:
            return
        self.busy = True
        self.workflow.cancel.clear()
        self.progress.setRange(0, 0)
        self.cancel_button.setEnabled(True)
        for button in self.mutation_buttons:
            button.setEnabled(False)
        self.assignment_combo.setEnabled(False)
        self.pages.widget(2).setEnabled(False)
        self.pages.widget(3).setEnabled(False)
        self.score.setEnabled(False)
        self.comment.setEnabled(False)
        self.thread = QThread(self)
        self.job = Job(fn, self.workflow)
        self.job.moveToThread(self.thread)
        self.thread.started.connect(self.job.run)
        self.job.log.connect(self.log)
        self.job.failed.connect(self.fail)
        self.job.done.connect(self.thread.quit)
        self.job.done.connect(self.job.deleteLater)
        self.thread.finished.connect(self.finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    @Slot()
    def finished(self):
        self.busy = False
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.cancel_button.setEnabled(False)
        for button in self.mutation_buttons:
            button.setEnabled(True)
        self.assignment_combo.setEnabled(True)
        self.pages.widget(2).setEnabled(True)
        self.pages.widget(3).setEnabled(True)
        self.score.setEnabled(True)
        self.comment.setEnabled(True)
        if self.workflow.client:
            self.connection.setText("已登录 · " + self.settings.data["bb"]["course_id"])
            self.password.clear()
        self.refresh()

    def cancel(self):
        self.workflow.cancel.set()
        self.log("停止请求已提交；当前网络请求或 OCR 命令结束后停止。")

    def refresh(self):
        self.students = self.store.list_students()
        self.assignments = self.store.list_assignments()
        previous = self.assignment_combo.currentData()
        self.assignment_combo.blockSignals(True)
        self.assignment_combo.clear()
        for item in self.assignments:
            self.assignment_combo.addItem(f"{item['title']}  ·  BB 满分 {item['max_score']:g}", item["id"])
        index = self.assignment_combo.findData(previous)
        if index >= 0:
            self.assignment_combo.setCurrentIndex(index)
        self.assignment_combo.blockSignals(False)
        self.filter_students()
        self.refresh_attempts()

    def filter_students(self):
        text = self.student_search.text().strip().lower()
        values = [s for s in self.students if (not self.scope_only.isChecked() or s["in_scope"]) and text in (s["student_id"] + s["name"]).lower()]
        populate(self.student_table, [[s["student_id"], s["name"], "范围内" if s["in_scope"] else "范围外", s["bb_user_id"]] for s in values])

    def refresh_attempts(self, *_):
        previous = self.current_attempt()
        aid = self.assignment_combo.currentData()
        if hasattr(self, "max_score") and aid and aid != self._rubric_assignment:
            first_selection = self._rubric_assignment is None
            if not first_selection:
                self.save_rubric(quiet=True)
            selected_rubric = self.settings.data.get("rubrics", {}).get(aid)
            if selected_rubric is None:
                selected_rubric = self.settings.data["rubric"] if first_selection and not self.settings.data.get("rubrics") else {"max_score": 10, "instructions": "", "reference_answer": ""}
            self._rubric_assignment = aid
            self.max_score.setValue(float(selected_rubric["max_score"]))
            self.rubric_text.setPlainText(selected_rubric["instructions"])
            self.reference.setPlainText(selected_rubric["reference_answer"])
            self.load_scoring_policy(selected_rubric)
            self.settings.data["rubric"] = copy.deepcopy(selected_rubric)
        if hasattr(self, "rubric_target") and aid:
            self.rubric_target.setText("当前作业：" + self.assignment_combo.currentText() + "\n规则按作业分别保存，切换作业时自动保留输入。")
        all_rows = [r for r in self.store.list_attempts(aid) if r["in_scope"]] if aid else []
        try:
            self.rows = latest_attempts(all_rows)
            ignored = len(all_rows) - len(self.rows)
            self.attempt_policy.setText(f"每名学生仅显示并处理最新提交；已忽略 {ignored} 份旧尝试，历史记录仍保留。")
        except ValueError as exc:
            self.rows = []
            self.attempt_policy.setText(str(exc))
            self.log(str(exc))
        self.attempt_table.blockSignals(True)
        populate(self.attempt_table, [[r["student_id"], r["name"], r.get("ai_score"), r.get("reviewed_score"), STATUS.get(r["status"], r["status"])] for r in self.rows])
        self.attempt_table.blockSignals(False)
        counts = [len(self.students), sum(s["in_scope"] for s in self.students), sum(r["status"] == "graded" for r in self.rows), f"{sum(r['status'] == 'reviewed' for r in self.rows)} / {sum(r['status'] == 'uploaded' for r in self.rows)}"]
        for label, count in zip(self.metrics, counts):
            label.setText(str(count))
        if self.rows:
            index = next((i for i, r in enumerate(self.rows) if previous and r["id"] == previous["id"]), 0)
            self.attempt_table.selectRow(index)
        self.select_attempt()

    def current_attempt(self):
        if not hasattr(self, "attempt_table"):
            return None
        index = self.attempt_table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def select_attempt(self):
        row = self.current_attempt()
        if not row:
            self.detail_title.setText("选择一份作业开始审核")
            self.ocr_text.clear()
            self.rationale.clear()
            self.provenance.clear()
            self.comment.clear()
            self.approve_button.setEnabled(False)
            self.revoke_button.setEnabled(False)
            self.reconcile_button.setEnabled(False)
            return
        self.detail_title.setText(f"{row['student_id']}  {row['name']}")
        review_max = row.get("reviewed_max_score") or self.max_score.value()
        ai_max = (row.get("provenance") or {}).get("rubric", {}).get("max_score", "未知")
        self.detail_meta.setText(f"提交：{row.get('submitted_at') or '时间未知'}  |  Attempt：{row['id']}\n{STATUS.get(row['status'], row['status'])}  ·  AI 评分满分 {ai_max}  ·  人工审核满分 {review_max:g}")
        self.ocr_text.setPlainText(row.get("ocr_text") or "尚未识别作业。")
        uncertainty = row.get("uncertainties") or []
        self.rationale.setPlainText((row.get("rationale") or "尚无评分依据") + "\n\n待检查：\n" + "\n".join(str(v) for v in uncertainty) + ("\n\n错误：" + row["error"] if row.get("error") else ""))
        self.provenance.setPlainText(json.dumps({k: row.get(k) for k in ["paths", "provenance", "reviewed_at", "receipt"]}, ensure_ascii=False, indent=2))
        self.score.setValue(row.get("reviewed_score") if row.get("reviewed_score") is not None else row.get("ai_score") or 0)
        self.comment.setPlainText(row.get("reviewed_comment") if row.get("reviewed_score") is not None else row.get("ai_comment") or "")
        enabled = not self.busy and row.get("ai_score") is not None and row["status"] in {"graded", "reviewed"}
        self.approve_button.setEnabled(enabled)
        self.revoke_button.setEnabled(not self.busy and row["status"] == "reviewed")
        self.reconcile_button.setEnabled(not self.busy and row["status"] in {"upload_uncertain", "uploading"})

    def fetch_attempts(self):
        aid = self.assignment_combo.currentData()
        if not aid:
            self.fail("请先登录并同步课程作业。")
        elif self.demo:
            self.log("演示模式使用本地虚构提交记录。")
        else:
            self.start(lambda: self.workflow.sync_attempts(aid))

    def process(self, stage):
        if self.busy:
            return
        aid = self.assignment_combo.currentData()
        if not aid or not self.rows:
            self.fail("请先选择作业并同步学生提交。")
            return
        if not self.save_rubric(quiet=True) or not self.save_settings(quiet=True):
            return
        force_ocr = stage == "ocr" and self.force_ocr.isChecked()
        if stage == "ocr":
            self.force_ocr.setChecked(False)
        self.start(lambda: self.workflow.process(aid, stage, force_ocr=force_ocr))

    def approve(self):
        row = self.current_attempt()
        if not row or self.busy:
            return
        try:
            if row["id"] not in {item["id"] for item in self.store.list_attempts(row["assignment_id"], latest_only=True)}:
                raise ValueError("这份作业已有更新的提交，请刷新列表并审核最新尝试。")
            ai_max = (row.get("provenance") or {}).get("rubric", {}).get("max_score")
            if ai_max is None or abs(float(ai_max) - self.max_score.value()) > 0.0001:
                raise ValueError("当前满分与这份 AI 评分使用的满分不同。请恢复原评分尺度，或按新满分重新评分后再审核。")
            if not self.save_rubric(quiet=True):
                return
            self.store.approve(row["id"], self.score.value(), self.comment.toPlainText().strip(), self.max_score.value())
            self.log(f"{row['student_id']} 已人工审核：{self.score.value():g} 分。")
            self.refresh_attempts()
        except Exception as exc:
            self.fail(str(exc))

    def open_attachment(self):
        row = self.current_attempt()
        if not row or not row.get("paths"):
            self.fail("当前作业没有本地附件。")
            return
        paths = [Path(p) for p in row["paths"]]
        # Open a directory if there are several files; executable submissions are never launched.
        target = paths[0] if len(paths) == 1 and paths[0].suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".docx"} else paths[0].parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def revoke_review(self):
        row = self.current_attempt()
        if row and not self.busy:
            try:
                self.store.revoke_review(row["id"])
                self.log(f"{row['student_id']} 审核已撤销，可以重新识别或评分。")
                self.refresh_attempts()
            except Exception as exc:
                self.fail(str(exc))

    def reconcile_upload(self):
        row = self.current_attempt()
        if not row or self.busy:
            return
        result, ok = QInputDialog.getItem(self, "人工核验上传结果", "请先在 BB 中核对这份 attempt 的实际成绩与评语。", ["已确认分数、评语均与审核结果一致", "已确认本次成绩未写入，需要重试"], 0, False)
        if not ok:
            return
        note, ok = QInputDialog.getMultiLineText(self, "记录核验依据", "填写你核对的 BB 提交版本、实际分数及评语等：")
        if ok and note.strip():
            try:
                if result.startswith("已确认分数"):
                    self.store.confirm_uploaded_after_check(row["id"], receipt={"verified": True, "manually_confirmed": True, "score": row["reviewed_score"]}, note=note.strip())
                    self.log("已记录人工核验，标记为已回传。")
                else:
                    self.store.reset_upload_after_check(row["id"], confirmed_not_uploaded=True, note=note.strip())
                    self.log("已记录人工核验，请再次核对后审核或上传。")
                self.refresh_attempts()
            except Exception as exc:
                self.fail(str(exc))

    def preview_upload(self):
        if self.demo:
            self.fail("演示模式禁止上传；普通模式中仅人工审核的成绩可进入回传。")
            return
        rows = [r for r in self.rows if r["status"] == "reviewed"]
        if not rows:
            self.fail("当前作业没有已人工审核且尚未上传的成绩。")
            return
        if len({r["student_id"] for r in rows}) != len(rows):
            self.fail("同一学生有多份已审核提交，请核对 attempt 并只保留要回传的一份审核结果。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("核对最终成绩并提交 BB")
        dialog.resize(920, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"课程 {self.settings.data['bb']['course_id']} · {self.assignment_combo.currentText()}\n将写入以下 {len(rows)} 份最终分数和评语。提交后逐项回读核验。"))
        preview = table(["学号", "姓名", "Attempt", "最终分", "评语"])
        populate(preview, [[r["student_id"], r["name"], r["id"], r["reviewed_score"], r["reviewed_comment"]] for r in rows])
        layout.addWidget(preview)
        confirmed = QCheckBox("我已核对以上名单、提交版本、分数和评语，确认回传 BB。")
        layout.addWidget(confirmed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("上传最终成绩")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        confirmed.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start(lambda: self.workflow.upload([r["id"] for r in rows]))

    def export(self):
        aid = self.assignment_combo.currentData()
        if not aid:
            self.fail("请先选择作业。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出批改报告", str(self.settings.root / "grading-report.csv"), "CSV 表格 (*.csv);;JSON 完整记录 (*.json)")
        if path:
            try:
                self.store.export_report(aid, Path(path))
                self.log(f"报告已导出：{path}")
            except Exception as exc:
                self.fail(str(exc))

    def closeEvent(self, event):
        if self.busy:
            self.workflow.cancel.set()
            self.log("正在停止批次，请等待当前请求结束后关闭窗口。")
            event.ignore()
        else:
            event.accept()


def main():
    parser = argparse.ArgumentParser(description="BB 作业批改助手")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--smoke-test", type=Path, help="保存窗口截图后退出，用于构建验证")
    parser.add_argument("--check-ocr", type=Path, help="用指定测试文件验证 OCR，结果写入独立目录，不连接 BB 或评分服务")
    parser.add_argument("--check-ocr-output", type=Path, help="OCR 验证输出目录（与 --check-ocr 一起使用）")
    args = parser.parse_args()
    if args.check_ocr:
        if args.check_ocr_output is None:
            parser.error("--check-ocr 需要 --check-ocr-output")
        check_settings = Settings(args.data_dir or default_data_dir())
        check_client = OcrClient(check_settings.data["ocr"])
        args.check_ocr_output.mkdir(parents=True, exist_ok=True)
        try:
            text = check_client.extract(args.check_ocr, args.check_ocr_output)
            result = {"ok": True, "characters": len(text), "metadata": check_client.last_metadata}
        except Exception as exc:
            result = {"ok": False, "error_type": type(exc).__name__}
        (args.check_ocr_output / "check-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return sys.exit(0 if result["ok"] else 1)
    app = QApplication(sys.argv[:1])
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    root = args.data_dir or default_data_dir()
    if args.demo:
        root = root / "demo"
    window = MainWindow(root, demo=args.demo)
    window.show()
    if args.smoke_test:
        def capture():
            args.smoke_test.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.smoke_test))
            app.quit()
        QTimer.singleShot(1000, capture)
    sys.exit(app.exec())
