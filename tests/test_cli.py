"""End-to-end tests through the actual mail_filter.py entrypoint (--test).

Skipped where pymilter is not installed -- mail_filter.py imports Milter
unconditionally at module load time, same as the original single-file
script, so these tests need the same environment the milter itself needs
(e.g. the ubuntu-latest CI job, which installs python3-milter)."""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("Milter")

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "src" / "mail_filter.py"


def run_mail_filter(*args):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True,
        text=True,
    )


def test_test_mode_rejects_matching_subject(tmp_path):
    rules_path = tmp_path / "rules.conf"
    rules_path.write_text('if subject /hacked/i { reject "Spam"; }\n', encoding="utf-8")
    conf_path = tmp_path / "mail_filter.conf"
    conf_path.write_text(f"[files]\nrules = {rules_path}\n", encoding="utf-8")

    result = run_mail_filter(
        "--test", "--test-config", str(conf_path), "--subject", "You have been HACKED",
    )
    assert result.returncode == 1
    assert "REJECT" in result.stdout


def test_test_mode_accepts_non_matching_subject(tmp_path):
    rules_path = tmp_path / "rules.conf"
    rules_path.write_text('if subject /hacked/i { reject "Spam"; }\n', encoding="utf-8")
    conf_path = tmp_path / "mail_filter.conf"
    conf_path.write_text(f"[files]\nrules = {rules_path}\n", encoding="utf-8")

    result = run_mail_filter(
        "--test", "--test-config", str(conf_path), "--subject", "A perfectly normal subject",
    )
    assert result.returncode == 0
    assert "ACCEPT" in result.stdout


def _client_name_rule(tmp_path):
    rules_path = tmp_path / "rules.conf"
    rules_path.write_text(
        'if client_name /\\.googleusercontent\\.com$/i and envelope_from /^<support@/i '
        '{ reject "VM"; }\n', encoding="utf-8")
    conf_path = tmp_path / "mail_filter.conf"
    conf_path.write_text(f"[files]\nrules = {rules_path}\n", encoding="utf-8")
    return conf_path


def test_test_mode_client_name_switch(tmp_path):
    conf_path = _client_name_rule(tmp_path)
    common = ("--test", "--test-config", str(conf_path),
              "--envelope-from", "<support@example.net>")

    result = run_mail_filter(*common, "--client-name", "1.2.3.4.bc.googleusercontent.com")
    assert result.returncode == 1
    assert "client_name:      '1.2.3.4.bc.googleusercontent.com'" in result.stdout

    result = run_mail_filter(*common, "--client-name", "mail.example.net")
    assert result.returncode == 0


def test_test_mode_client_name_from_json_context(tmp_path):
    conf_path = _client_name_rule(tmp_path)
    context = tmp_path / "ctx.json"
    context.write_text('{"envelope_from": "<support@example.net>", '
                       '"client_name": "1.2.3.4.bc.googleusercontent.com"}', encoding="utf-8")
    result = run_mail_filter("--test", "--test-config", str(conf_path),
                             "--test-context", str(context))
    assert result.returncode == 1


def test_version_flag():
    result = run_mail_filter("--version")
    assert result.returncode == 0
    assert "mail_filter" in result.stdout
