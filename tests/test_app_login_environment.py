"""CAS password environment fallback is in-memory only and never echoed."""

from unittest.mock import Mock

import pytest

from bb_assistant.app import MainWindow


@pytest.fixture
def login_window(qtbot, tmp_path, monkeypatch):
    window = MainWindow(tmp_path, demo=True)
    qtbot.addWidget(window)
    window.demo = False
    window.username.setText(" test-account ")
    monkeypatch.setattr(window, "start", lambda fn: fn())
    monkeypatch.setattr(window, "save_settings", Mock(return_value=True))
    monkeypatch.setattr(window.workflow, "login", Mock())
    return window


def test_empty_password_uses_environment_without_displaying_or_saving_it(login_window, monkeypatch):
    monkeypatch.setenv("USTC_CAS_PWD", "synthetic-cas-password")
    window = login_window
    assert window.password.text() == ""
    assert "USTC_CAS_PWD" in window.password.placeholderText()
    window.login()
    window.workflow.login.assert_called_once_with("test-account", "synthetic-cas-password")
    assert window.password.text() == ""
    assert "synthetic-cas-password" not in str(window.settings.data)
    assert "synthetic-cas-password" not in window.log_box.toPlainText()
    assert window.redact("failed synthetic-cas-password") == "failed [凭据已隐藏]"


def test_manual_password_takes_precedence_over_environment(login_window, monkeypatch):
    monkeypatch.setenv("USTC_CAS_PWD", "synthetic-env-password")
    login_window.password.setText("synthetic-manual-password")
    login_window.login()
    login_window.workflow.login.assert_called_once_with("test-account", "synthetic-manual-password")
    assert login_window.redact("synthetic-manual-password") == "[凭据已隐藏]"


def test_environment_password_stays_redacted_after_failed_login_and_environment_change(login_window, monkeypatch):
    monkeypatch.setenv("USTC_CAS_PWD", "synthetic-cas-password")
    warnings = []
    monkeypatch.setattr("bb_assistant.app.QMessageBox.warning", lambda *args: warnings.append(args[-1]))
    login_window.login()
    monkeypatch.delenv("USTC_CAS_PWD")
    login_window.fail("login failed: synthetic-cas-password")
    assert warnings == ["login failed: [凭据已隐藏]"]
    assert "synthetic-cas-password" not in login_window.log_box.toPlainText()


@pytest.mark.parametrize("value", [None, ""])
def test_missing_password_explains_environment_fallback(login_window, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("USTC_CAS_PWD", raising=False)
    else:
        monkeypatch.setenv("USTC_CAS_PWD", value)
    failures = []
    monkeypatch.setattr(login_window, "fail", failures.append)
    login_window.login()
    assert len(failures) == 1
    assert "USTC_CAS_PWD" in failures[0]
    assert "重新启动" in failures[0]
    login_window.workflow.login.assert_not_called()
    login_window.save_settings.assert_not_called()


def test_environment_password_does_not_replace_missing_username(login_window, monkeypatch):
    monkeypatch.setenv("USTC_CAS_PWD", "synthetic-cas-password")
    login_window.username.clear()
    failures = []
    monkeypatch.setattr(login_window, "fail", failures.append)
    login_window.login()
    assert failures == ["请填写统一身份认证账号。"]
    login_window.workflow.login.assert_not_called()


def test_environment_fallback_does_not_bypass_demo_mode(login_window, monkeypatch):
    monkeypatch.setenv("USTC_CAS_PWD", "synthetic-cas-password")
    login_window.demo = True
    failures = []
    monkeypatch.setattr(login_window, "fail", failures.append)
    login_window.login()
    assert "演示模式" in failures[0]
    login_window.workflow.login.assert_not_called()
