"""The host-side updater script, run against throwaway git repos (docker replaced by `true`)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "updater" / "asset-updater.sh"


def _bash_exe() -> str | None:
    """Git's own bash on Windows (the one on PATH may be WSL's, without git)."""
    git = shutil.which("git")
    if git:
        bundled = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if bundled.exists():
            return str(bundled)
    return shutil.which("bash")


BASH = _bash_exe()


def _bash(cwd: Path, command: str, **env) -> subprocess.CompletedProcess:
    exports = " ".join(f"{k}='{v}'" for k, v in env.items())
    return subprocess.run([BASH, "-c", f"export {exports}; {command}" if env else command],
                          cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@pytest.fixture
def repos(tmp_path):
    if BASH is None or _bash(tmp_path, "command -v git").returncode:
        pytest.skip("needs bash and git")
    (tmp_path / "updater.sh").write_bytes(SCRIPT.read_bytes().replace(b"\r\n", b"\n"))
    setup = """
        set -e
        git init -q --bare remote.git
        git clone -q remote.git repo 2>/dev/null
        cd repo && git config user.email t@t && git config user.name t
        echo 1 > f && git add f && git commit -qm first && git push -q origin HEAD 2>/dev/null && cd ..
        git clone -q remote.git other 2>/dev/null
        cd other && git config user.email t@t && git config user.name t
        echo 2 > f && git commit -qam 'feat: 新 "quoted" \\ thing' && git push -q 2>/dev/null
        mkdir data
    """
    assert _bash(tmp_path, setup).returncode == 0
    return tmp_path


def _run(root: Path, *args) -> dict:
    env = {"REPO_DIR": "repo", "DATA_DIR": "data", "COMPOSE": "true", "INSTALLED_SELF": "nowhere/x.sh"}
    result = _bash(root, "bash updater.sh " + " ".join(args), **env)
    assert result.returncode == 0, result.stderr
    return json.loads((root / "data" / "update.status").read_text(encoding="utf-8"))


def test_check_reports_what_is_new(repos):
    status = _run(repos, "--check")
    assert status["state"] == "idle" and status["behind"] == 1
    assert status["pending"][0].endswith('feat: 新 "quoted" \\ thing')


def test_update_request_pulls_and_rebuilds(repos):
    (repos / "data" / "update.request").write_text("update\n")

    status = _run(repos)

    assert status["state"] == "done" and status["behind"] == 0 and status["finished"]
    assert not (repos / "data" / "update.request").exists()
    assert (repos / "repo" / "f").read_text().strip() == "2"


def test_failed_rebuild_is_reported(repos):
    (repos / "data" / "update.request").write_text("update\n")
    env = {"REPO_DIR": "repo", "DATA_DIR": "data", "COMPOSE": "false", "INSTALLED_SELF": "nowhere/x.sh"}

    _bash(repos, "bash updater.sh", **env)

    status = json.loads((repos / "data" / "update.status").read_text(encoding="utf-8"))
    assert status["state"] == "error" and "update.log" in status["error"]


def test_unknown_requests_are_refused(repos):
    (repos / "data" / "update.request").write_text("rm -rf\n")
    assert _run(repos)["state"] == "error"


def test_failed_build_leaves_the_rebuild_pending(repos):
    (repos / "data" / "update.deployed").write_text("old\n")
    (repos / "data" / "update.request").write_text("update\n")
    env = {"REPO_DIR": "repo", "DATA_DIR": "data", "COMPOSE": "false", "INSTALLED_SELF": "nowhere/x.sh"}
    _bash(repos, "bash updater.sh", **env)

    status = json.loads((repos / "data" / "update.status").read_text(encoding="utf-8"))
    assert status["behind"] == 0 and status["deployed"] == "old"  # code pulled, still not built

    (repos / "data" / "update.request").write_text("update\n")
    status = _run(repos)
    assert status["state"] == "done" and status["deployed"] == status["current"]
