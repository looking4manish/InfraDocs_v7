"""update.sh end-to-end against STUB git/docker/curl/sleep on PATH — no network, no Docker,
no Mongo. Asserts: in-place update preserves volumes (never 'down -v'), reports old->new
commit, gates on health, and on a health failure exits non-zero with a named reason without
tearing anything down (not wedged)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _exe(path: Path, body: str):
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    deploy = tmp_path / "infradocs"
    (deploy / ".git").mkdir(parents=True)
    dc = deploy / "deploy" / "docker"
    dc.mkdir(parents=True)
    (dc / ".env").write_text("API_PORT=8090\nWEB_PORT=8081\n")
    (dc / "docker-compose.yml").write_text("services: {}\n")

    head = tmp_path / "HEAD"; head.write_text("oldsha")
    calls = tmp_path / "docker_calls"; calls.write_text("")
    bindir = tmp_path / "bin"; bindir.mkdir()

    _exe(bindir / "git", f'''#!/usr/bin/env bash
if [[ "$1" == "rev-parse" ]]; then cat "{head}" 2>/dev/null || echo unknown; exit 0; fi
if [[ "$1" == "fetch" ]]; then exit 0; fi
if [[ "$1" == "reset" ]]; then echo newsha > "{head}"; exit 0; fi
exit 0
''')
    _exe(bindir / "docker", f'''#!/usr/bin/env bash
echo "docker $*" >> "{calls}"
[[ "$1" == "info" ]] && exit 0
if [[ "$1" == "compose" ]]; then
  case "$*" in
    *" ps mongo"*) printf 'mongo Up\\n'; exit 0;;
    *" ps"*)       printf 'api Up\\nmongo Up\\n'; exit 0;;
    *version*)     exit 0;;
    *) exit 0;;
  esac
fi
exit 0
''')
    _exe(bindir / "curl", '#!/usr/bin/env bash\nprintf "%s" "${UPDATE_STUB_HTTP:-200}"\n')
    _exe(bindir / "sleep", '#!/usr/bin/env bash\nexit 0\n')

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["INFRADOCS_DIR"] = str(deploy)
    env["HOME"] = str(tmp_path)
    return {"deploy": deploy, "dc": dc, "calls": calls, "env": env}


def _run(sandbox, http="200"):
    env = dict(sandbox["env"]); env["UPDATE_STUB_HTTP"] = http
    return subprocess.run(["bash", str(ROOT / "update.sh")], env=env,
                          capture_output=True, text=True, timeout=60)


def test_update_preserves_volumes_reports_commits_and_ends_healthy(sandbox):
    r = _run(sandbox, http="200")
    assert r.returncode == 0, r.stderr + r.stdout
    out = r.stdout
    assert "oldsha" in out and "newsha" in out and "->" in out            # before/after commit
    assert "Update complete" in out
    calls = sandbox["calls"].read_text()
    assert "up -d --build" in calls                                       # rebuilt + restarted in place
    assert "down -v" not in calls and " -v" not in calls                 # NEVER tears down the volume
    assert "volume" not in calls.lower()
    assert (sandbox["dc"] / ".env").exists()                              # deploy env (secret/role) preserved
    assert "fleet_sync" not in calls                                      # V7: no separate secret sync step


def test_update_health_failure_exits_nonzero_named_reason_without_wedging(sandbox):
    r = _run(sandbox, http="500")
    assert r.returncode != 0
    assert "API did not return 200" in r.stderr                           # named reason
    calls = sandbox["calls"].read_text()
    assert "down -v" not in calls and "down" not in calls                 # not torn down -> not wedged
