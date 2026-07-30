from pathlib import Path
from unittest.mock import patch


def test_service_path_skips_nonexistent_node_modules(tmp_path):
    """Service PATH should not include node_modules/.bin if it doesn't exist."""
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    node_modules_bin = str(tmp_path / "node_modules" / ".bin")
    assert node_modules_bin not in dirs


def test_service_path_includes_node_modules_when_present(tmp_path):
    """Service PATH should include node_modules/.bin when it exists."""
    nm_bin = tmp_path / "node_modules" / ".bin"
    nm_bin.mkdir(parents=True)
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(nm_bin) in dirs


def test_service_path_includes_hermes_home_node_modules(tmp_path):
    """Service PATH should include ~/.hermes/node_modules/.bin when it exists."""
    hermes_nm = tmp_path / ".hermes" / "node_modules" / ".bin"
    hermes_nm.mkdir(parents=True)
    from hermes_cli.gateway import _build_service_path_dirs
    with patch("hermes_cli.gateway.get_hermes_home", return_value=tmp_path / ".hermes"):
        dirs = _build_service_path_dirs(project_root=tmp_path)
    assert str(hermes_nm) in dirs


def test_service_path_skips_unreadable_hermes_home(tmp_path):
    """A system-service probe must tolerate another user's protected home."""
    from hermes_cli.gateway import _build_service_path_dirs

    hermes_home = tmp_path / "protected" / ".hermes"
    original_is_dir = Path.is_dir

    def guarded_is_dir(path):
        if hermes_home in (path, *path.parents):
            raise PermissionError("simulated protected home")
        return original_is_dir(path)

    with (
        patch("hermes_cli.gateway.get_hermes_home", return_value=hermes_home),
        patch.object(Path, "is_dir", guarded_is_dir),
    ):
        dirs = _build_service_path_dirs(project_root=tmp_path)

    assert all(str(hermes_home) not in entry for entry in dirs)
