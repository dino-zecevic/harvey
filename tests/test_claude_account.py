"""CLAUDE_CONFIG_DIR in .env picks the Claude login Harvey uses, over the
shell's, and the quota gauge reads that login's token only."""

import hashlib
import os
import subprocess

import pytest

from harvey.claude_runtime import ACCOUNT_VAR, pin_account
from harvey.integrations import quota


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(ACCOUNT_VAR, raising=False)


def test_the_env_file_wins_over_the_shell(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_CONFIG_DIR=~/.claude-second\n")
    monkeypatch.setenv(ACCOUNT_VAR, "/somewhere/else")
    assert pin_account(env_file) == os.path.expanduser("~/.claude-second")
    assert os.environ[ACCOUNT_VAR] == os.path.expanduser("~/.claude-second")


def test_an_empty_setting_leaves_the_shell_alone(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_CONFIG_DIR=\n")
    assert pin_account(env_file) == ""
    monkeypatch.setenv(ACCOUNT_VAR, "/from/shell")
    assert pin_account(env_file) == "/from/shell"
    assert pin_account(tmp_path / "missing.env") == "/from/shell"


def test_keychain_item_follows_the_config_dir(monkeypatch):
    assert quota.keychain_service() == "Claude Code-credentials"
    monkeypatch.setenv(ACCOUNT_VAR, "/Users/sam/.claude-second")
    digest = hashlib.sha256(b"/Users/sam/.claude-second").hexdigest()[:8]
    assert quota.keychain_service() == f"Claude Code-credentials-{digest}"


def test_a_pinned_login_never_reads_another_accounts_token(tmp_path, monkeypatch):
    monkeypatch.setenv(ACCOUNT_VAR, str(tmp_path / "second"))
    monkeypatch.setattr(quota.sys, "platform", "darwin")
    asked = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["security", "dump-keychain"]:
            raise AssertionError("a pinned login must not enumerate other accounts")
        asked.append(cmd[3])
        blob = '{"claudeAiOauth": {"accessToken": "tok-%s"}}' % cmd[3][-8:]
        return subprocess.CompletedProcess(cmd, 0, stdout=blob, stderr="")

    monkeypatch.setattr(quota.subprocess, "run", fake_run)
    token = quota.get_oauth_token()
    assert asked == [quota.keychain_service()]
    assert token == "tok-" + quota.keychain_service()[-8:]


def test_a_credentials_file_in_the_pinned_dir_is_used(tmp_path, monkeypatch):
    second = tmp_path / "second"
    second.mkdir()
    (second / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "file-tok"}}')
    monkeypatch.setenv(ACCOUNT_VAR, str(second))
    assert quota.get_oauth_token() == "file-tok"
