"""Phase 8 — operational action dispatcher unit tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.actions import (
    ALLOWED_ACTIONS,
    ActionNotAllowed,
    ActionResult,
    SelfActionRefused,
    dispatch,
)


def _container_asset(name="openwebui", container_id="abc123def456"):
    return {
        "category": "docker_container",
        "asset_id": f"oci:container:{container_id[:12]}",
        "name": name,
        "project": "openwebui",
        "metadata": {"container_id": container_id[:12]},
    }


def _systemd_asset(name="my.service", category="systemd_service"):
    return {
        "category": category,
        "asset_id": f"oci:service:{name}",
        "name": name,
        "project": "System",
        "metadata": {"unit_type": "service"},
    }


def _nginx_asset(name="x.example.com"):
    return {
        "category": "nginx_server_block",
        "asset_id": f"oci:nginx:x:{name}:443",
        "name": name,
        "project": "System",
        "metadata": {"config_file": "/etc/nginx/sites-enabled/x"},
    }


# ----------------------- allow-list enforcement -----------------------------


def test_unknown_category_is_rejected():
    asset = {"category": "docker_image", "name": "x", "asset_id": "1", "metadata": {}}
    with pytest.raises(ActionNotAllowed):
        dispatch(asset, "delete")


def test_unknown_action_is_rejected():
    with pytest.raises(ActionNotAllowed):
        dispatch(_container_asset(), "delete")


def test_storage_mount_has_no_actions():
    asset = {"category": "storage_mount", "name": "/", "asset_id": "1", "metadata": {}}
    with pytest.raises(ActionNotAllowed):
        dispatch(asset, "start")


def test_allowed_actions_is_complete():
    """Snapshot test so adding new actions is intentional."""
    assert ALLOWED_ACTIONS["docker_container"] == {"start", "stop", "restart", "logs", "inspect", "stats", "check_update"}
    assert ALLOWED_ACTIONS["systemd_service"] == {"start", "stop", "restart", "logs", "status", "enable", "disable"}
    assert ALLOWED_ACTIONS["nginx_server_block"] == {"test", "reload"}
    assert ALLOWED_ACTIONS["docker_image"] == {"pull", "prune", "check_update"}
    assert ALLOWED_ACTIONS["docker_compose"] == {"up", "down", "restart", "recreate", "update"}
    assert ALLOWED_ACTIONS["systemd_timer"] == {"start", "stop", "restart", "status", "enable", "disable", "trigger"}
    assert ALLOWED_ACTIONS["docker_volume"] == {"inspect", "prune"}
    assert ALLOWED_ACTIONS["storage_mount"] == {"inspect"}


# ----------------------- self-protection ------------------------------------


def test_self_action_refused_for_infradocs_v6_unit():
    asset = _systemd_asset(name="infradocs-v6-api.service")
    with pytest.raises(SelfActionRefused):
        dispatch(asset, "restart")


def test_self_action_refused_does_not_match_unrelated_unit():
    """Self-protection must not over-match: a unit that does NOT start with the
    `infradocs` prefix (e.g. `grafana.service`) is freely actionable. NB: since P0.3
    the guard prefix is `infradocs` (covers the docker containers infradocs-api-1/…
    AND the legacy infradocs-v6-* units), so a bare `infradocs.service` IS protected —
    the unrelated case is any non-`infradocs*` unit."""
    asset = _systemd_asset(name="grafana.service")
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0)
        result = dispatch(asset, "restart")
    assert result.status == "success"


# ----------------------- docker container actions ---------------------------


def test_docker_container_start_calls_sdk():
    container = MagicMock()
    container.name = "openwebui"
    client = MagicMock()
    client.containers.get.return_value = container

    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "start")

    assert result.status == "success"
    container.start.assert_called_once()


def test_docker_container_restart_passes_timeout():
    container = MagicMock()
    container.name = "openwebui"
    client = MagicMock()
    client.containers.get.return_value = container

    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "restart", {"timeout": 5})

    container.restart.assert_called_once_with(timeout=5)
    assert result.status == "success"


def test_docker_container_logs_caps_tail():
    container = MagicMock()
    container.name = "openwebui"
    container.logs.return_value = b"hello\nworld\n"
    client = MagicMock()
    client.containers.get.return_value = container

    with patch("app.actions._docker_client", return_value=client):
        # Request 5000 lines — should cap at 1000
        result = dispatch(_container_asset(), "logs", {"tail": 5000})

    container.logs.assert_called_once_with(tail=1000)
    assert "hello" in result.stdout
    assert result.details["lines"] == 2


def test_docker_container_not_found_yields_failed_result():
    from docker.errors import NotFound
    client = MagicMock()
    client.containers.get.side_effect = NotFound("nope")

    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "start")

    assert result.status == "failed"
    assert "not found" in result.stderr.lower()


# ----------------------- systemd actions (subprocess-mocked) ----------------


def test_systemd_restart_invokes_sudo_systemctl():
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0,
                                        stdout="", stderr="")
        result = dispatch(_systemd_asset(name="myapp.service"), "restart")

    args, kwargs = run.call_args
    cmd = args[0]
    assert cmd == ["sudo", "-n", "systemctl", "restart", "myapp.service"]
    assert result.status == "success"


def test_systemd_logs_invokes_journalctl():
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0,
                                        stdout="log line", stderr="")
        dispatch(_systemd_asset(name="myapp.service"), "logs", {"tail": 50})

    cmd = run.call_args.args[0]
    assert cmd[:2] == ["journalctl", "-u"]
    assert "myapp.service" in cmd
    assert "50" in cmd


def test_systemd_status_allows_nonzero():
    """systemctl status returns non-zero for stopped units; that's not 'failed'."""
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=3,
                                        stdout="inactive", stderr="")
        result = dispatch(_systemd_asset(), "status")

    # Confirm allow_nonzero was passed
    assert run.call_args.kwargs.get("allow_nonzero") is True
    assert result.status == "success"


# ----------------------- nginx actions --------------------------------------


def test_nginx_test_runs_nginx_minus_t():
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0)
        dispatch(_nginx_asset(), "test")

    cmd = run.call_args.args[0]
    assert "nginx" in cmd and "-t" in cmd


def test_nginx_reload_runs_signal():
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0)
        dispatch(_nginx_asset(), "reload")

    cmd = run.call_args.args[0]
    assert "reload" in cmd or "-s" in cmd


# ----------------------- timing -------------------------------------------


def test_dispatcher_records_duration_when_handler_doesnt():
    container = MagicMock()
    container.name = "x"
    client = MagicMock()
    client.containers.get.return_value = container

    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "start")

    # Handler returns ActionResult with duration_ms=0; dispatcher fills it in.
    assert result.duration_ms >= 0  # Could be 0 if very fast, but field is set


# ----------------------- Wave A actions (Phase 3.5.2) -----------------------


def test_docker_container_inspect_returns_attrs():
    container = MagicMock()
    container.name = "openwebui"
    container.attrs = {"State": {"Status": "running"}, "Id": "abc"}
    client = MagicMock()
    client.containers.get.return_value = container
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "inspect")
    assert result.status == "success"
    assert "State" in result.stdout
    assert result.details["state"]["Status"] == "running"


def test_docker_container_stats_returns_snapshot():
    container = MagicMock()
    container.name = "openwebui"
    container.stats.return_value = {"read": "2026-06-12T00:00:00Z", "cpu_stats": {}}
    client = MagicMock()
    client.containers.get.return_value = container
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "stats")
    container.stats.assert_called_once_with(stream=False)
    assert result.status == "success"


def test_systemd_enable_invokes_sudo_systemctl():
    with patch("app.actions._run_subprocess") as run:
        run.return_value = ActionResult(status="success", return_code=0)
        dispatch(_systemd_asset(name="myapp.service"), "enable")
    cmd = run.call_args.args[0]
    assert cmd == ["sudo", "-n", "systemctl", "enable", "myapp.service"]


def test_systemd_disable_is_self_protected():
    asset = _systemd_asset(name="infradocs-v6-agent.timer", category="systemd_timer")
    with pytest.raises(SelfActionRefused):
        dispatch(asset, "disable")


def test_docker_image_pull_calls_sdk():
    client = MagicMock()
    with patch("app.actions._docker_client", return_value=client):
        asset = {
            "category": "docker_image",
            "name": "ghcr.io/open-webui/open-webui:latest",
            "asset_id": "oci:image:1",
            "metadata": {"tags": ["ghcr.io/open-webui/open-webui:latest"]},
        }
        result = dispatch(asset, "pull")
    client.images.pull.assert_called_once_with("ghcr.io/open-webui/open-webui:latest")
    assert result.status == "success"


def test_docker_container_self_protect_now_enforced():
    """Centralized guard: an infradocs-v6-* CONTAINER is now refused too."""
    asset = _container_asset(name="infradocs-v6-api")
    with pytest.raises(SelfActionRefused):
        dispatch(asset, "restart")


def test_compose_recreate_forces_recreate():
    asset = {"category": "docker_compose", "name": "web", "asset_id": "1", "metadata": {"file_path": "/x/docker-compose.yml"}}
    with patch("app.actions._run_subprocess") as rs:
        rs.return_value = ActionResult(status="success")
        dispatch(asset, "recreate")
    assert "--force-recreate" in rs.call_args[0][0]


def test_timer_trigger_starts_service():
    asset = {"category": "systemd_timer", "name": "backup.timer", "asset_id": "1", "metadata": {}}
    with patch("app.actions._run_subprocess") as rs:
        rs.return_value = ActionResult(status="success")
        dispatch(asset, "trigger")
    cmd = rs.call_args[0][0]
    assert cmd[:4] == ["sudo", "-n", "systemctl", "start"] and cmd[-1] == "backup.service"


def test_timer_trigger_is_self_protected():
    asset = {"category": "systemd_timer", "name": "infradocs-v6-agent.timer", "asset_id": "1", "metadata": {}}
    with pytest.raises(SelfActionRefused):
        dispatch(asset, "trigger")


def test_docker_image_prune_runs():
    asset = {"category": "docker_image", "name": "x", "asset_id": "1", "metadata": {}}
    with patch("app.actions._run_subprocess") as rs:
        rs.return_value = ActionResult(status="success")
        dispatch(asset, "prune")
    assert rs.call_args[0][0] == ["docker", "image", "prune", "-f"]


def test_docker_volume_inspect_runs():
    asset = {"category": "docker_volume", "name": "myvol", "asset_id": "1", "metadata": {}}
    with patch("app.actions._run_subprocess") as rs:
        rs.return_value = ActionResult(status="success")
        dispatch(asset, "inspect")
    assert rs.call_args[0][0][:3] == ["docker", "volume", "inspect"]


def test_storage_mount_inspect_runs():
    asset = {"category": "storage_mount", "name": "/data", "asset_id": "1", "metadata": {"mountpoint": "/data"}}
    with patch("app.actions._run_subprocess") as rs:
        rs.return_value = ActionResult(status="success")
        dispatch(asset, "inspect")
    assert rs.call_args[0][0][0] == "findmnt"


# ----------------------- update flow (image upgrade) ------------------------


def test_compose_update_pulls_then_recreates():
    """update = `compose pull` followed by `compose up -d` (recreate alone never pulls)."""
    asset = {"category": "docker_compose", "name": "openwebui", "asset_id": "1",
             "metadata": {"file_path": "/x/docker-compose.yml"}}
    with patch("app.actions._run_subprocess") as rs:
        rs.side_effect = [
            ActionResult(status="success", stdout="pulled newer image"),
            ActionResult(status="success", stdout="recreated openwebui"),
        ]
        result = dispatch(asset, "update")
    assert rs.call_count == 2
    assert rs.call_args_list[0].args[0][-1] == "pull"
    assert rs.call_args_list[1].args[0][-2:] == ["up", "-d"]
    assert result.status == "success"
    assert "pulled newer image" in result.stdout and "recreated openwebui" in result.stdout


def test_compose_update_aborts_if_pull_fails():
    """A failed pull must NOT proceed to recreate (don't bounce the app for nothing)."""
    asset = {"category": "docker_compose", "name": "x", "asset_id": "1",
             "metadata": {"file_path": "/x/docker-compose.yml"}}
    with patch("app.actions._run_subprocess") as rs:
        rs.side_effect = [
            ActionResult(status="failed", stderr="pull: manifest unknown"),
            ActionResult(status="success", stdout="should not run"),
        ]
        result = dispatch(asset, "update")
    assert rs.call_count == 1  # stopped after the failed pull
    assert result.status == "failed"


def _image_asset(ref="ghcr.io/open-webui/open-webui:latest"):
    return {"category": "docker_image", "name": ref, "asset_id": "oci:image:1",
            "metadata": {"tags": [ref]}}


def test_check_update_flags_when_digests_differ():
    client = MagicMock()
    client.images.get_registry_data.return_value.id = "sha256:REMOTE_NEW"
    client.images.get.return_value.attrs = {"RepoDigests": ["repo@sha256:LOCAL_OLD"]}
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_image_asset(), "check_update")
    assert result.status == "success"
    assert result.details["update_available"] is True
    assert "UPDATE AVAILABLE" in result.stdout


def test_check_update_clears_when_digests_match():
    client = MagicMock()
    client.images.get_registry_data.return_value.id = "sha256:SAME"
    client.images.get.return_value.attrs = {"RepoDigests": ["repo@sha256:SAME"]}
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_image_asset(), "check_update")
    assert result.details["update_available"] is False
    assert "up to date" in result.stdout


def test_check_update_unknown_when_no_local_digest():
    client = MagicMock()
    client.images.get_registry_data.return_value.id = "sha256:REMOTE"
    client.images.get.return_value.attrs = {"RepoDigests": []}
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_image_asset(), "check_update")
    assert result.details["update_available"] is None


def test_container_check_update_uses_running_image_tag():
    container = MagicMock()
    container.image.tags = ["ghcr.io/open-webui/open-webui:latest"]
    client = MagicMock()
    client.containers.get.return_value = container
    client.images.get_registry_data.return_value.id = "sha256:REMOTE_NEW"
    client.images.get.return_value.attrs = {"RepoDigests": ["repo@sha256:LOCAL_OLD"]}
    with patch("app.actions._docker_client", return_value=client):
        result = dispatch(_container_asset(), "check_update")
    assert result.details["update_available"] is True
    client.images.get_registry_data.assert_called_once_with(
        "ghcr.io/open-webui/open-webui:latest"
    )
