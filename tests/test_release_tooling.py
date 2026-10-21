"""Exercise the real builder against a committed fixture, including offline help."""
import hashlib
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_release_bundle_uses_selected_commit_and_includes_docs(tmp_path):
    pytest.importorskip('mkdocs')
    for name in ('scripts', 'docs', 'meta_coder/static'):
        shutil.copytree(ROOT / name, tmp_path / name)
    for name in ('pyproject.toml', 'pixi.lock', 'mkdocs.yml'):
        shutil.copy2(ROOT / name, tmp_path / name)
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    git('init')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'Fixture')
    (tmp_path / 'scripts/install.sh').write_text('UNCOMMITTED CONTENT')
    subprocess.run(['bash', 'scripts/build_release.sh', 'HEAD'], cwd=tmp_path,
                   env={**os.environ, 'PYTHON': sys.executable}, check=True, capture_output=True)
    dist = tmp_path / 'dist'
    version = (dist / 'latest.txt').read_text().strip()
    with tarfile.open(dist / f'meta-coder-{version}.tar.gz') as archive:
        prefix = f'meta-coder-{version}/'
        assert prefix + 'meta_coder/documentation/index.html' in archive.getnames()
        assert prefix + 'meta_coder/documentation/assets/app.css' in archive.getnames()
        assert prefix + 'pixi.lock' in archive.getnames()
        assert b'UNCOMMITTED' not in archive.extractfile(prefix + 'scripts/install.sh').read()
    assert 'UNCOMMITTED' not in (dist / 'install.sh').read_text()
    for name in ('uninstall.sh', 'uninstall.ps1'):
        assert (dist / name).read_bytes() == (ROOT / 'scripts' / name).read_bytes()
    assert {'install.sh', 'install.ps1', 'uninstall.sh', 'uninstall.ps1', 'latest.txt'} < {
        line.split('  ')[1] for line in (dist / 'SHA256SUMS').read_text().splitlines()}
    for line in (dist / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((dist / name).read_bytes()).hexdigest() == digest


@pytest.mark.skipif(sys.platform == 'win32', reason='Unix installer')
@pytest.mark.parametrize('fail_install', [False, True])
def test_public_installer_downloads_pinned_bundle_and_preserves_data(tmp_path, fail_install):
    import io
    import json

    bundle = tmp_path / 'bundle.tar.gz'
    with tarfile.open(bundle, 'w:gz') as archive:
        body = b'[project]\nname = "fixture"\n'
        info = tarfile.TarInfo('meta-coder-1.2.3/pyproject.toml')
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    bin_dir = tmp_path / 'fake-bin'
    bin_dir.mkdir()
    log = tmp_path / 'downloads.jsonl'
    gh = bin_dir / 'curl'
    gh.write_text(f'''#!{sys.executable}
import json, pathlib, shutil, sys
args = sys.argv[1:]
with open({str(log)!r}, 'a') as f: f.write(json.dumps(args) + '\\n')
url = args[args.index('-fsSL') + 1]
out = args[args.index('-o') + 1]
if url.endswith('/latest.txt'): print('1.2.3')
else: shutil.copyfile({str(bundle)!r}, out)
''')
    gh.chmod(0o755)
    pixi = bin_dir / 'pixi'
    pixi.write_text('#!/bin/sh\nexit ' + ('1' if fail_install else '0') + '\n')
    pixi.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()
    data = home / ('Library/Application Support/Meta-Coder' if sys.platform == 'darwin' else '.local/share/meta-coder')
    old = data / 'app/0.9.0'
    old.mkdir(parents=True)
    project = data / 'projects/keep.txt'
    project.parent.mkdir()
    project.write_text('keep me')
    env = {key: value for key, value in os.environ.items() if not key.startswith('META_CODER_') and key != 'XDG_DATA_HOME'}
    env.update(HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
    result = subprocess.run(['bash', str(ROOT / 'scripts/install.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == (1 if fail_install else 0), result.stderr
    assert project.read_text() == 'keep me'
    assert old.exists() == fail_install
    downloads = [json.loads(line) for line in log.read_text().splitlines()]
    assert downloads[0][1] == 'https://github.com/shaheedazaad/meta-coder/releases/latest/download/latest.txt'
    assert downloads[1][1] == 'https://github.com/shaheedazaad/meta-coder/releases/download/v1.2.3/meta-coder-1.2.3.tar.gz'
    if not fail_install:
        launcher = (home / '.local/bin/meta-coder').read_text()
        assert str(pixi) in launcher
        assert '--locked' in launcher


def _data_root(home):
    return home / ('Library/Application Support/Meta-Coder' if sys.platform == 'darwin' else '.local/share/meta-coder')


def _script_env(home, bin_dir=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('META_CODER_') and key != 'XDG_DATA_HOME'}
    env['HOME'] = str(home)
    if bin_dir:
        env['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']
    return env


def _uninstall(home):
    # Piped on stdin, as with the documented `curl ... | bash`.
    return subprocess.run(['bash'], input=(ROOT / 'scripts/uninstall.sh').read_text(), env=_script_env(home),
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(sys.platform == 'win32', reason='Unix uninstaller')
def test_uninstaller_removes_launcher_written_by_installer(tmp_path):
    bin_dir = tmp_path / 'fake bin'
    bin_dir.mkdir()
    bundle = tmp_path / 'bundle.tar.gz'
    with tarfile.open(bundle, 'w:gz') as archive:
        archive.add(ROOT / 'pyproject.toml', 'meta-coder-1.2.3/pyproject.toml')
    (bin_dir / 'curl').write_text(f'''#!{sys.executable}
import shutil, sys
args = sys.argv[1:]
if args[1].endswith('/latest.txt'): print('1.2.3')
else: shutil.copyfile({str(bundle)!r}, args[args.index('-o') + 1])
''')
    (bin_dir / 'pixi').write_text('#!/bin/sh\nexit 0\n')
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    home = tmp_path / 'home with spaces'
    home.mkdir()
    subprocess.run(['bash', str(ROOT / 'scripts/install.sh')], env=_script_env(home, bin_dir),
                   capture_output=True, text=True, check=True)
    launcher = home / '.local/bin/meta-coder'
    assert '# meta-coder launcher' in launcher.read_text().splitlines()
    project = _data_root(home) / 'projects/keep.txt'
    project.parent.mkdir()
    project.write_text('keep me')

    assert 'Keeping launcher' not in _uninstall(home)
    assert not launcher.exists()
    assert not (_data_root(home) / 'app').exists()
    assert project.read_text() == 'keep me'
    # Re-running after a complete uninstall is a harmless no-op.
    assert 'Keeping launcher' not in _uninstall(home)
    assert project.read_text() == 'keep me'


@pytest.mark.skipif(sys.platform == 'win32', reason='Unix uninstaller')
@pytest.mark.parametrize(('body', 'removed'), [
    # Written by install.sh before the marker line existed.
    ('exec "/opt/pixi bin/pixi" run --locked --manifest-path "{app}/1.2.3/pyproject.toml" start "$@"', True),
    # A marked launcher may change how it runs the app.
    ('# meta-coder launcher\nPIXI=pixi\n"$PIXI" run --manifest-path "{app}/1.2.3/pyproject.toml" start', True),
    ('exec /usr/bin/python3 -m meta_coder "$@"', False),
    ('# meta-coder launcher\nexec /usr/bin/python3 -m meta_coder "$@"', False),
    ('exec pixi run --manifest-path "/elsewhere/meta-coder/app/1.2.3/pyproject.toml" start "$@"', False),
    ('# exec pixi run --manifest-path "{app}/1.2.3/pyproject.toml" start', False),
])
def test_uninstaller_recognises_only_its_own_launchers(tmp_path, body, removed):
    home = tmp_path / 'home with spaces'
    app_root = _data_root(home) / 'app'
    (app_root / '1.2.3').mkdir(parents=True)
    launcher = home / '.local/bin/meta-coder'
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/usr/bin/env bash\n' + body.replace('{app}', str(app_root)) + '\n')

    output = _uninstall(home)
    assert launcher.exists() is not removed
    assert ('Keeping launcher' in output) is not removed
    assert not app_root.exists()


def test_windows_launcher_matches_what_the_uninstaller_recognises():
    # No PowerShell in CI: check statically that the launcher install.ps1 writes
    # carries the marker and the run line shape uninstall.ps1 looks for.
    install = (ROOT / 'scripts/install.ps1').read_text()
    uninstall = (ROOT / 'scripts/uninstall.ps1').read_text()
    written = re.search(r'Set-Content -Path \$Launcher -Value "(.*)"$', install, re.M).group(1)
    lines = written.replace('`"', '"').split('`r`n')
    assert lines[:2] == ['@echo off', 'REM meta-coder launcher']
    assert '-ceq "REM meta-coder launcher"' in uninstall
    assert '--manifest-path "$ManifestPath"' in lines[2]
    assert 'Contains("--manifest-path `"$AppRoot\\")' in uninstall
    run_line = re.search(r"\$_ -match '(.*)'", uninstall).group(1)
    assert re.match(run_line, lines[2].replace('$PixiPath', r'C:\Users\A B\pixi.exe'))
