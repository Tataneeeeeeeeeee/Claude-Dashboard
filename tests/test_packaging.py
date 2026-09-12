"""Checks on the installers, launchers and the PyInstaller spec.

These do not run pip, which needs the network. They verify the things that
break silently: a script referring to a file that no longer exists, or a
spec that forgets to bundle the web assets.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    """Read a file from the project root."""
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [
    "install.sh", "install.ps1", "start.sh", "start.bat",
    "build.sh", "build.bat", "run_tests.sh",
    "claude-dashboard.spec", "run_app.py", "config.json",
    "README.md", "SCHEMA.md", "requirements.txt",
])
def test_every_shipped_script_exists_and_is_not_empty(name):
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    assert path.stat().st_size > 0


@pytest.mark.parametrize("name", ["install.sh", "start.sh", "build.sh", "run_tests.sh"])
def test_shell_scripts_parse(name):
    """A syntax error here only shows up when a user runs the script."""
    result = subprocess.run(["bash", "-n", str(ROOT / name)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["install.sh", "start.sh", "build.sh", "run_tests.sh"])
def test_shell_scripts_are_executable(name):
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("no execute bit on Windows")
    assert os.access(ROOT / name, os.X_OK), f"{name} is not executable"


def test_installers_copy_everything_the_app_needs_at_runtime():
    """The copy list must cover every file run_app.py depends on."""
    required = {"claude_dashboard", "assets", "requirements.txt", "run_app.py"}
    for script in ("install.sh", "install.ps1"):
        text = read(script)
        for item in required:
            assert item in text, f"{script} never mentions {item}"


def test_the_launcher_template_execs_the_entry_point_and_forwards_arguments():
    r"""The launcher must run run_app.py, not `-m`, and pass flags through.

    The argument list is escaped in the heredoc that writes it, so the
    template reads `"\$@"` in the source and `"$@"` in the generated file.
    """
    text = read("install.sh")
    assert 'exec "$PREFIX/venv/bin/python" "$PREFIX/app/run_app.py" "\\$@"' in text


def test_the_generated_launcher_is_valid_and_forwards_arguments(tmp_path):
    """Render the heredoc the way the installer does and check the result."""
    import re
    import subprocess

    text = read("install.sh")
    body = re.search(r"cat > \"\$LAUNCHER\" <<LAUNCHEREOF\n(.*?)LAUNCHEREOF",
                     text, re.S)
    assert body, "the launcher heredoc moved"
    rendered = (body.group(1)
                .replace("$PREFIX", str(tmp_path))
                .replace("\\$@", "$@"))
    launcher = tmp_path / "claude-dashboard"
    launcher.write_text(rendered, encoding="utf-8")

    assert subprocess.run(["bash", "-n", str(launcher)]).returncode == 0
    assert 'exec "' + str(tmp_path) + '/venv/bin/python"' in rendered
    assert rendered.rstrip().endswith('"$@"'), "arguments must be forwarded"


def test_the_windows_launcher_avoids_a_console_window():
    text = read("install.ps1")
    assert "pythonw.exe" in text, "a desktop app must not open a console"


def test_uninstall_never_removes_user_data():
    """Neither installer may delete ~/.claude or ~/.claude-dashboard."""
    for script in ("install.sh", "install.ps1"):
        text = read(script)
        body = text.split("uninstall")[1] if "uninstall" in text else text
        assert "rm -rf \"$HOME/.claude\"" not in body
        assert ".claude-dashboard\" -Recurse" not in body
        # And it should say so.
        assert ".claude-dashboard" in text


def test_uninstall_refreshes_rather_than_deletes_the_shared_desktop_cache():
    text = read("install.sh")
    assert "mimeinfo.cache" in text, "the shared cache must be handled deliberately"
    assert "rm -f \"$DESKTOP_DIR/mimeinfo.cache\"" not in text


def test_the_desktop_entry_matches_the_real_window_class():
    """`StartupWMClass` must match what the toolkit reports, or the window
    is not associated with its launcher in the taskbar."""
    text = read("install.sh")
    assert "StartupWMClass=run_app.py" in text


def test_the_spec_bundles_the_web_assets_and_the_icons():
    text = read("claude-dashboard.spec")
    assert '"claude_dashboard/web"' in text
    for icon in ("icon.png", "icon.ico", "icon.icns"):
        assert icon in text
    assert "console=False" in text, "the build must be windowed"


def test_the_spec_constrains_the_gtk_theme_collection():
    """Without this the Linux build balloons past 300 MB of icon themes."""
    text = read("claude-dashboard.spec")
    assert "hooksconfig" in text
    assert '"icons": ["Adwaita"]' in text


def test_the_spec_declares_the_imports_pyinstaller_cannot_see():
    text = read("claude-dashboard.spec")
    for name in ("uvicorn.loops.auto", "uvicorn.lifespan.on", "watchdog.observers"):
        assert name in text, f"{name} is loaded by name and must be declared"


def test_requirements_pin_no_exact_versions_that_would_break_new_pythons():
    """Lower bounds keep the install working on whatever Python is present."""
    for line in read("requirements.txt").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" not in line, f"{line} pins an exact version"
        assert re.match(r"^[A-Za-z0-9_.-]+>=", line), f"{line} has no lower bound"


def test_the_default_config_matches_the_code():
    """config.json in the repository is the documented default."""
    import json

    from claude_dashboard.config import DEFAULT_CONFIG

    shipped = json.loads(read("config.json"))
    assert shipped == DEFAULT_CONFIG, "config.json has drifted from DEFAULT_CONFIG"


def test_the_readme_documents_the_one_command_install():
    text = read("README.md")
    assert "curl -fsSL" in text
    assert "install.ps1 | iex" in text
    assert "claude-dashboard" in text
    assert "--uninstall" in text


def test_the_readme_install_command_needs_no_environment_variable():
    """The published one-liner must work as written, with nothing to set."""
    import re

    text = read("README.md")
    install = text[text.index("## Install"):text.index("## Building")]
    curl = re.search(r"curl -fsSL (\S+/install\.sh) \| bash$", install, re.M)
    assert curl, "the README has no bare `curl ... | install.sh | bash` line"
    assert "CLAUDE_DASHBOARD_SRC=" not in curl.group(0)


def test_the_installers_default_to_the_published_source():
    """Piped into a shell with no checkout, they must know where to fetch."""
    shell = read("install.sh")
    assert "DEFAULT_SRC=" in shell
    assert 'SRC="${CLAUDE_DASHBOARD_SRC:-$DEFAULT_SRC}"' in shell

    powershell = read("install.ps1")
    assert "archive/refs/heads/main.zip" in powershell


def test_the_readme_url_matches_the_installer_default():
    """A README pointing at one repository and a script at another is the
    kind of drift nobody notices until an install fails."""
    import re

    readme = read("README.md")
    shell = read("install.sh")
    raw = re.search(r"raw\.githubusercontent\.com/([^/]+/[^/]+)/", readme)
    archive = re.search(r"github\.com/([^/]+/[^/]+)/archive/", shell)
    assert raw and archive, "could not find both repository references"
    assert raw.group(1) == archive.group(1), (
        f"README points at {raw.group(1)} but install.sh fetches {archive.group(1)}"
    )


def _run_installer(tmp_path, src, extra_path=""):
    """Pipe install.sh into bash the way `curl | bash` does."""
    return subprocess.run(
        ["bash", "-s"],
        input=read("install.sh"),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin" + extra_path,
            "CLAUDE_DASHBOARD_PREFIX": str(tmp_path / "prefix"),
            "CLAUDE_DASHBOARD_BIN": str(tmp_path / "bin"),
            "CLAUDE_DASHBOARD_SRC": src,
        },
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_an_archive_url_is_downloaded_not_cloned(tmp_path):
    """A .tar.gz on a git host must take the download path.

    The host pattern also matches an archive URL, so if the cases are
    ordered wrongly the published one-liner hands a tarball to `git
    clone` and every install fails.
    """
    result = _run_installer(
        tmp_path, "https://github.com/owner/repo/archive/refs/heads/main.tar.gz"
    )
    combined = result.stdout + result.stderr
    assert "fatal: repository" not in combined, "it tried to clone an archive"


def test_a_non_archive_download_is_reported_clearly(tmp_path):
    """A host can answer 200 with an HTML page instead of an archive.

    GitHub does exactly that for a branch with no commits, so `curl -f`
    cannot catch it and the bytes have to be inspected. Without this the
    user sees "gzip: stdin: not in gzip format".
    """
    text = read("install.sh")
    assert "not_an_archive" in text
    assert "1f8b" in text, "the gzip magic number is never checked"
    assert '"PK"' in text, "the zip magic number is never checked"
    assert "no commits pushed yet" in text, "the likely cause is not explained"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_served_error_page_does_not_look_like_a_tar_failure(tmp_path):
    """Serve an HTML page from a file:// URL named .tar.gz and check the
    message names the real problem."""
    fake = tmp_path / "main.tar.gz"
    fake.write_text("<!DOCTYPE html><html>not found</html>", encoding="utf-8")
    result = _run_installer(tmp_path, f"file://{fake}")
    assert result.returncode != 0
    assert "did not return an archive" in result.stderr
    assert "gzip" not in result.stderr.lower(), "the raw tar error leaked through"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_a_missing_local_directory_is_reported(tmp_path):
    result = _run_installer(tmp_path, str(tmp_path / "nowhere"))
    assert result.returncode != 0
    assert "no such directory" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_an_unrecognisable_source_is_reported(tmp_path):
    result = _run_installer(tmp_path, "wat://nonsense")
    assert result.returncode != 0
    assert "cannot tell how to fetch" in result.stderr
