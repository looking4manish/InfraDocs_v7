"""install.sh — onboarding fork, failure-class behaviour, and the address pick-list
(end-to-end, stubbed binaries).

The terminal installer shares one bring-up, then forks at onboarding (UI wizard vs CLI:
primary / secondary / standalone). These tests drive the WHOLE script against stub
git/docker/curl/python/sleep + tailscale/ip/hostname on PATH — no network, no Docker, no
real repo touched — and assert the behaviour successive UAT runs exposed:

  * UI path stands the stack up + emits the wizard URL, no CLI prompts.
  * STANDALONE skips the reachable-address step, the peer prompts, AND the address
    auto-detection entirely.
  * primary/secondary offer an auto-detected NUMBERED address pick-list (tailscale / LAN /
    localhost / manual), defaulting to the first peer-reachable candidate (tailscale here),
    never localhost; picking a number selects it; manual still accepts a typed value; an
    invalid choice re-prompts; no detected candidates falls back to the manual prompt; and
    --advertise-url / env bypasses the picker.
  * Quitting onboarding after a healthy bring-up leaves the stack UP (no teardown) +
    prints how to finish later; a BRING-UP failure still tears the half-built stack down.

The pure detection/validation logic is unit-tested in test_cli_install.py; the stubs here
just let us exercise install.sh's control flow.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"

# Default detected interfaces for the stub `ip -j -4 addr`: loopback + a LAN nic + the
# tailscale nic (same IP the tailscale CLI reports, to prove de-duplication).
_DEFAULT_IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
    {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "10.0.0.12"}]},
    {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.70.18.9"}]},
])


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path):
    """A self-contained checkout + a PATH of stub binaries that record their calls.

    python3 records argv then short-circuits the API-driven subcommands (`complete` -> OK,
    `check-*` -> rc from STUB_CHECK_*_RC) while letting `render-env`/`detect-addresses` run
    for real. `sleep` is a no-op (instant health loops). `curl` health probes return
    STUB_HTTP_CODE (default 200). `tailscale`/`ip`/`hostname` feed address detection from
    STUB_TAILSCALE_IP / STUB_IP_JSON / STUB_HOSTNAME_I (overridable per-test).
    """
    if not shutil.which("bash"):
        pytest.skip("bash not available")

    deploy_dir = tmp_path / "infradocs"
    (deploy_dir / "app").mkdir(parents=True)
    (deploy_dir / "deploy" / "docker").mkdir(parents=True)
    (deploy_dir / ".git").mkdir()  # so install.sh takes the "update existing checkout" path
    shutil.copy(ROOT / "app" / "__init__.py", deploy_dir / "app" / "__init__.py")
    shutil.copy(ROOT / "app" / "cli_install.py", deploy_dir / "app" / "cli_install.py")
    shutil.copy(ROOT / "deploy" / "docker" / "deploy.sh", deploy_dir / "deploy" / "docker" / "deploy.sh")
    (deploy_dir / "deploy" / "docker" / "docker-compose.yml").write_text("services: {}\n")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"

    _write_exe(bindir / "git", "#!/usr/bin/env bash\nexit 0\n")
    _write_exe(bindir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    _write_exe(
        bindir / "docker",
        '#!/usr/bin/env bash\n'
        'printf "docker %s\\n" "$*" >> "' + str(calls) + '"\n'
        'case " $* " in *" ps"*) [[ -n "${STUB_WEB_STATE:-}" ]] && printf "web   %s\\n" "$STUB_WEB_STATE";; esac\n'
        'exit 0\n',
    )
    _write_exe(
        bindir / "curl",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in *http_code*) echo "${STUB_HTTP_CODE:-200}"; exit 0;; esac; done\n'
        "echo 203.0.113.9\nexit 0\n",
    )
    # Address-detection stubs.
    _write_exe(
        bindir / "tailscale",
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "ip" ]]; then\n'
        '  [[ -n "${STUB_TAILSCALE_IP:-}" ]] && { echo "$STUB_TAILSCALE_IP"; exit 0; }\n'
        "  exit 1\n"
        "fi\nexit 0\n",
    )
    _write_exe(
        bindir / "ip",
        '#!/usr/bin/env bash\nprintf "%s" "${STUB_IP_JSON:-}"\nexit 0\n',
    )
    _write_exe(
        bindir / "hostname",
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        '  -I) echo "${STUB_HOSTNAME_I:-}" ;;\n'
        '  *)  echo "${STUB_HOSTNAME:-ci-node}" ;;\n'
        "esac\n",
    )
    _write_exe(
        bindir / "python3",
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        'printf "python %s\\n" "$args" >> "' + str(calls) + '"\n'
        'case " $args " in\n'
        '  *" complete "*)       echo OK; exit 0 ;;\n'
        '  *" check-primary "*)  exit "${STUB_CHECK_PRIMARY_RC:-0}" ;;\n'
        '  *" check-priority "*) exit "${STUB_CHECK_PRIORITY_RC:-0}" ;;\n'
        '  *" port-check "*)\n'
        '    pv=""; nxt=0; for a in "$@"; do [[ $nxt == 1 ]] && { pv="$a"; nxt=0; }; [[ "$a" == "--port" ]] && nxt=1; done\n'
        '    case " ${STUB_INUSE_PORTS:-} " in *" $pv "*) exit 3;; esac; exit 0 ;;\n'
        '  *" find-free-port "*) [[ -n "${STUB_NO_FREE:-}" ]] && exit 1; echo "${STUB_FREE_PORT:-8091}"; exit 0 ;;\n'
        "esac\n"
        'exec "' + sys.executable + '" "$@"\n',
    )

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["INFRADOCS_DIR"] = str(deploy_dir)
    env["INFRADOCS_MODE"] = "detached"  # skip the mode prompt in onboarding-flow tests
    env["INFRADOCS_SERVER_ID"] = "ci-node"  # deterministic; avoids the node-id prompt
    env["INFRADOCS_ADVERTISE_URL"] = ""      # don't let a real env leak the picker bypass
    # Detection defaults: a tailscale addr + a LAN addr.
    env["STUB_TAILSCALE_IP"] = "100.70.18.9"
    env["STUB_IP_JSON"] = _DEFAULT_IP_JSON
    env["STUB_HOSTNAME_I"] = "10.0.0.12"
    return {"env": env, "deploy_dir": deploy_dir, "calls": calls}


def _run(sandbox, *args, stdin_text="", extra_env=None):
    env = dict(sandbox["env"])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _calls(sandbox):
    return sandbox["calls"].read_text() if sandbox["calls"].exists() else ""


def _stack_up(out):
    assert "stack is up and healthy (API + Mongo + web)" in out


def _no_teardown(sandbox):
    assert "down" not in _calls(sandbox), "a healthy stack must NOT be torn down"


# ----------------------- UI path (unchanged) --------------------------------


def test_ui_path_brings_stack_up_and_emits_url_without_cli_prompts(sandbox):
    r = _run(sandbox, "--onboard=ui")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"installer failed:\n{out}"
    calls = _calls(sandbox)

    assert "render-env" in calls, "the .env was never rendered (shared bring-up skipped)"
    assert (sandbox["deploy_dir"] / "deploy" / "docker" / ".env").exists()
    assert "compose" in calls and " up" in calls, "the Docker stack was never started"

    assert "OPEN THE SETUP WIZARD" in out
    assert "http://localhost:8081" in out
    assert "UI" in out and "NOT yet configured" in out

    assert "complete" not in calls, "UI path must not enroll via the CLI"
    assert "check-primary" not in calls and "check-priority" not in calls


# ----------------------- standalone (skips address + detection) -------------


def test_standalone_skips_address_detection_and_completes(sandbox):
    # No stdin: standalone needs no address, no peers, no checks, no detection.
    r = _run(sandbox, "--onboard=cli", "--role=standalone")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"standalone install failed:\n{out}"
    calls = _calls(sandbox)

    _stack_up(out)
    assert "standalone node configured" in out and "no cluster" in out
    assert "standalone node ready" in out
    assert "complete" in calls and "standalone" in calls
    assert "check-primary" not in calls and "check-priority" not in calls
    assert "pick how other nodes" not in out, "standalone must not show the address picker"
    _no_teardown(sandbox)


# ----------------------- address pick-list (primary/secondary) --------------


def test_picker_lists_candidates_and_defaults_to_tailscale(sandbox):
    # Empty line at the menu = accept the default ([1]).
    r = _run(sandbox, "--onboard=cli", "--role=primary", stdin_text="\n")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"picker default failed:\n{out}"

    _stack_up(out)
    assert "detect-addresses" in _calls(sandbox)
    # The full menu, with labels + the always-present escape options.
    assert "pick how other nodes + browsers reach this box" in out
    assert "http://100.70.18.9:8081" in out and "tailscale" in out
    assert "http://10.0.0.12:8081" in out and "lan / eth0" in out
    assert "http://localhost:8081" in out and "this host only" in out
    assert "enter manually" in out
    # Default = the first (peer-reachable) candidate, i.e. tailscale — NOT localhost.
    assert "address:   http://100.70.18.9:8081" in out
    assert "complete" in _calls(sandbox)
    _no_teardown(sandbox)


def test_picker_pick_number_selects_that_address(sandbox):
    # "2" = the LAN candidate.
    r = _run(sandbox, "--onboard=cli", "--role=primary", stdin_text="2\n")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "address:   http://10.0.0.12:8081" in out
    _no_teardown(sandbox)


def test_picker_manual_option_accepts_typed_value(sandbox):
    # Menu is 1) tailscale 2) lan 3) localhost 4) enter manually -> choose 4, then type.
    r = _run(sandbox, "--onboard=cli", "--role=primary",
             stdin_text="4\nhttp://typed-host:8081\n")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "address:   http://typed-host:8081" in out
    _no_teardown(sandbox)


def test_picker_invalid_choice_reprompts(sandbox):
    # "9" is out of range -> re-prompt; then "1" selects tailscale.
    r = _run(sandbox, "--onboard=cli", "--role=primary", stdin_text="9\n1\n")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "enter a number 1-4" in out, "an invalid menu choice must re-prompt"
    assert "address:   http://100.70.18.9:8081" in out
    _no_teardown(sandbox)


def test_no_candidates_falls_back_to_manual_prompt(sandbox):
    # Detection finds nothing -> free-text prompt (which still re-prompts on empty).
    env = {"STUB_TAILSCALE_IP": "", "STUB_IP_JSON": "", "STUB_HOSTNAME_I": ""}
    r = _run(sandbox, "--onboard=cli", "--role=primary",
             stdin_text="\nhttp://manual-fallback:8081\n", extra_env=env)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "detect-addresses" in _calls(sandbox)        # detection ran...
    assert "pick how other nodes" not in out             # ...but produced no menu
    assert "this can't be empty" in out                  # manual prompt re-prompts on empty
    assert "address:   http://manual-fallback:8081" in out
    _no_teardown(sandbox)


def test_advertise_url_override_bypasses_picker(sandbox):
    # Scripted: --advertise-url skips detection AND the menu entirely.
    r = _run(sandbox, "--onboard=cli", "--role=primary",
             "--advertise-url=http://scripted-host:8081")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "detect-addresses" not in _calls(sandbox), "an override must not run detection"
    assert "pick how other nodes" not in out
    assert "address:   http://scripted-host:8081" in out
    assert "complete" in _calls(sandbox)
    _no_teardown(sandbox)


# ----------------------- recoverable onboarding / failure classes -----------


def test_quit_after_healthy_bringup_leaves_stack_up_with_resume_instructions(sandbox):
    # 'q' at the address picker bails out.
    r = _run(sandbox, "--onboard=cli", "--role=primary", stdin_text="q\n")
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"quitting onboarding should exit cleanly, got:\n{out}"
    calls = _calls(sandbox)

    _stack_up(out)
    assert "finish onboarding later" in out.lower()
    assert "the stack is UP" in out
    assert "install.sh --onboard=cli" in out
    assert "setup wizard" in out and "http://localhost:8081" in out
    assert "complete" not in calls
    _no_teardown(sandbox)


def test_bringup_failure_still_tears_down_half_built_stack(sandbox):
    # Stack never reports healthy -> BRING-UP failure, teardown is correct.
    r = _run(sandbox, "--onboard=cli", "--role=standalone",
             extra_env={"STUB_HTTP_CODE": "000"})
    out = r.stdout + r.stderr
    assert r.returncode != 0, "an unhealthy bring-up must fail"
    assert "never became healthy" in out or "never became reachable" in out
    assert "down" in _calls(sandbox), "a half-built stack should be torn down"


# ----------------------- override validation --------------------------------


def test_bad_onboard_value_is_rejected_before_building(sandbox):
    r = _run(sandbox, "--onboard=bogus")
    assert r.returncode != 0
    assert "invalid --onboard" in (r.stdout + r.stderr)
    assert "compose" not in _calls(sandbox)


def test_bad_role_value_is_rejected_before_building(sandbox):
    r = _run(sandbox, "--onboard=cli", "--role=bogus")
    assert r.returncode != 0
    assert "invalid --role" in (r.stdout + r.stderr)
    assert "compose" not in _calls(sandbox)


# ----------------------- install mode: detached (default) vs normal ---------

def test_mode_detached_severs_origin(sandbox):
    r = _run(sandbox, "--onboard=ui", "--mode=detached")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "mode = detached" in out
    assert "detached" in out and ("not re-pulled" in out or "severed origin" in out or "yours to modify" in out)


def test_mode_normal_stays_synced_to_origin(sandbox):
    r = _run(sandbox, "--onboard=ui", "--mode=normal")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "mode = normal" in out and "synced to origin" in out


def test_mode_prompt_defaults_to_detached(sandbox):
    # no --mode and INFRADOCS_MODE cleared -> prompt; closed stdin picks the [D]efault.
    r = _run(sandbox, "--onboard=ui", extra_env={"INFRADOCS_MODE": ""})
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "mode = detached" in out


def test_bad_mode_is_rejected(sandbox):
    r = _run(sandbox, "--onboard=ui", "--mode=bogus")
    assert r.returncode != 0
    assert "invalid --mode" in (r.stdout + r.stderr)


def test_ui_wizard_prints_detected_addresses_not_placeholder(sandbox):
    # UI path must reuse the SAME detection the CLI picker uses and print real URLs.
    r = _run(sandbox, "--onboard=ui")
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "<this node's reachable address>" not in out, "placeholder must be gone"
    assert "http://100.70.18.9:8081" in out and "tailscale" in out       # detected top pick
    assert "http://localhost:8081" in out and "this host only" in out


# ----------------------- web/API port auto-pick (default, both modes) -------

def test_default_taken_noninteractive_auto_advances_and_binds(sandbox):
    # Default 8081 taken, NO override, non-interactive: auto-advance + bind, no abort/prompt.
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "STUB_INUSE_PORTS": "8081",
                                 "STUB_FREE_PORT": "8091", "INFRADOCS_ONBOARD": "ui"})
    assert r.returncode == 0, r.stdout + r.stderr
    env_txt = (sandbox["deploy_dir"] / "deploy" / "docker" / ".env").read_text()
    assert "WEB_PORT=8091" in env_txt and "DOMAIN=:8091" in env_txt   # bound the auto-chosen port
    assert "auto-selected 8091" in (r.stdout + r.stderr)


def test_auto_chosen_port_propagates_to_advertised_enroll_address(sandbox):
    # The auto-chosen 8091 must reach the enroll/advertise threading (complete --web-port /
    # detect-addresses --web-port), so the advertised address matches the bound port.
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "STUB_INUSE_PORTS": "8081",
                                 "STUB_FREE_PORT": "8091", "INFRADOCS_ONBOARD": "cli",
                                 "INFRADOCS_ROLE": "standalone"})
    assert r.returncode == 0, r.stdout + r.stderr
    calls = _calls(sandbox)
    assert "complete " in calls and "--web-port 8091" in calls


def test_explicit_web_port_taken_fails_with_named_reason(sandbox):
    # Explicit request for a taken port FAILS loudly — auto-pick must NOT override it.
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "INFRADOCS_WEB_PORT": "8081",
                                 "STUB_INUSE_PORTS": "8081"})
    assert r.returncode != 0
    both = r.stdout + r.stderr
    assert "explicitly requested" in both and "8081" in both
    assert "auto-selected" not in both              # did NOT silently pick another
    assert "up -d" not in _calls(sandbox)


def test_no_free_port_in_range_fails_with_named_reason(sandbox):
    # Default taken and nothing free in range -> named failure, not an infinite loop.
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "STUB_INUSE_PORTS": "8081",
                                 "STUB_NO_FREE": "1"})
    assert r.returncode != 0
    assert "no free port was found in range" in (r.stdout + r.stderr)
    assert "up -d" not in _calls(sandbox)


def test_auto_selected_log_line_is_emitted(sandbox):
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "STUB_INUSE_PORTS": "8081",
                                 "STUB_FREE_PORT": "8091", "INFRADOCS_ONBOARD": "ui"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "web port 8081 in use" in r.stdout
    assert "auto-selected 8091 (serving on :8091)" in r.stdout


def test_post_bringup_health_fails_when_web_container_not_serving(sandbox):
    # Ports pass preflight, but the web container is Restarting (bind race / masquerade):
    # the post-bring-up check must fail loudly, not report the stack "up".
    r = _run(sandbox, extra_env={"INFRADOCS_NONINTERACTIVE": "1", "INFRADOCS_ONBOARD": "ui",
                                 "STUB_WEB_STATE": "Restarting"})
    assert r.returncode != 0
    both = (r.stdout + r.stderr).lower()
    assert "web container is not running" in both and "restarting" in both
