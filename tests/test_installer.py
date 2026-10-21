import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import install as i


def test_atomic_write_permissions_and_symlink_refusal(tmp_path):
    path = tmp_path / 'private/config.json'
    i.atomic_write(path, b'private')
    assert path.read_bytes() == b'private'
    assert path.stat().st_mode & 0o777 == 0o600
    target = tmp_path / 'link'
    target.symlink_to(path)
    with pytest.raises(ValueError):
        i.atomic_write(target, b'bad')
    assert path.read_bytes() == b'private'


def test_transaction_restores_original_and_removes_new_files(tmp_path):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.write_text('original')
    old.chmod(0o640)
    tx = i.Transaction(tmp_path / 'backups')
    tx.write(old, b'changed')
    tx.write(new, b'created')
    assert all(Path(r['backup']).stat().st_mode & 0o777 == 0o600 for r in tx.records if r['backup'])
    tx.rollback()
    assert old.read_text() == 'original' and not new.exists()
    assert old.stat().st_mode & 0o777 == 0o640
    symlink = tmp_path / 'link'
    symlink.symlink_to(old)
    with pytest.raises(ValueError):
        tx.write(symlink, b'no')


def test_managed_alias_idempotence_and_preservation():
    original = '# my settings\nexport EDITOR=vim\n'
    once = i.alias_text(original, Path('/home/my name/bin/opencode-zen'))
    twice = i.alias_text(once, Path('/home/my name/bin/opencode-zen'))
    assert once == twice
    assert twice.count('alias opencode=') == 1
    assert i.strip_alias(twice) == original
    with pytest.raises(ValueError):
        i.alias_text('alias opencode=mine\n', Path('/bin/x'))
    with pytest.raises(ValueError):
        i.strip_alias(i.MARKER_START)
    with pytest.raises(ValueError):
        i.strip_alias(i.MARKER_END + i.MARKER_START)


def test_executable_discovery(tmp_path, monkeypatch):
    binary = tmp_path / 'with spaces/opencode'
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    assert i.find_opencode(str(binary)) == binary
    monkeypatch.setattr(i.shutil, 'which', lambda name: str(binary))
    assert i.find_opencode() == binary
    with pytest.raises(ValueError, match='not found'):
        i.find_opencode(str(tmp_path / 'missing'))


def test_generated_launcher_preserves_arguments_and_other_commands(tmp_path):
    binary = tmp_path / 'space dir/opencode'
    binary.parent.mkdir()
    binary.write_text('#!/usr/bin/env python3\nimport json,sys,os\nprint(json.dumps({"args":sys.argv[1:],"no_proxy":os.getenv("NO_PROXY")}))\n')
    binary.chmod(0o755)
    launcher = tmp_path / 'launcher'
    launcher.write_text(i.launcher_text(binary))
    launcher.chmod(0o755)
    subprocess.run(['sh', '-n', str(launcher)], check=True)
    for args, expected in [([], ['--standalone']), (['run', 'hello world'], ['run', 'hello world', '--standalone']), (['auth', 'login'], ['auth', 'login']), (['run', '--standalone', 'x'], ['run', '--standalone', 'x']), (['run', '--server', 'http://local'], ['run', '--server', 'http://local'])]:
        out = json.loads(subprocess.check_output([str(launcher)] + args))
        assert out['args'] == expected
        assert '127.0.0.1' in out['no_proxy']


def test_systemd_quoting_and_port_check():
    text = i.unit_text(Path('/home/a b/$x%y/python'), Path('/home/a/config.json'), 'user')
    assert 'WantedBy=default.target' in text and '$$x%%y' in text
    assert 'multi-user.target' in i.unit_text(Path('/bin/python'), Path('/config'), 'system')
    with pytest.raises(ValueError):
        i.systemd_quote('bad\npath')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        assert not i.free_port(sock.getsockname()[1])
    assert i.free_port(0)


def test_run_failure_does_not_print_command_output(monkeypatch):
    monkeypatch.setattr(i.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='private-token'))
    with pytest.raises(RuntimeError) as exc:
        i.run(['pip', 'install'])
    assert 'private-token' not in str(exc.value)


def test_health_missing_and_valid(monkeypatch):
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"ok":true}'
    monkeypatch.setattr(i.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=lambda *a, **k: Reply()))
    assert i.health(1234) == {'ok': True}
    def fail(*args, **kwargs): raise OSError()
    monkeypatch.setattr(i.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=fail))
    assert i.health(1234) is None


def test_settings_input_without_logging_secrets(monkeypatch):
    monkeypatch.setattr(i, 'ask', lambda *a: True)
    responses = iter(['bad', 'socks5://user:private-password@host:1080', ''])
    monkeypatch.setattr(i.getpass, 'getpass', lambda *a: next(responses))
    settings = i.settings_prompt(18998)
    assert len(settings.routes) == 2 and settings.routes[0] == 'direct'
    old = {'routes': ['direct'], 'token': 't' * 32}
    assert i.settings_prompt(18998, old).token == old['token']


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('SHELL', '/bin/bash')
    binary = home / 'opencode'
    binary.write_text('#!/bin/sh\n')
    binary.chmod(0o755)
    args = SimpleNamespace(install_dir=home / 'install', opencode=str(binary), routes_file=None)
    monkeypatch.setattr(i, 'run', lambda command: 'opencode v2.1.0' if '--version' in command else '')
    monkeypatch.setattr(i, 'health', lambda port: None)
    monkeypatch.setattr(i, 'free_port', lambda port: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(i, 'settings_prompt', lambda port, old=None: i.Settings(('direct',), 't' * 32, port))
    def answer(message, default=True):
        return False  # no service, alias, or live model call
    monkeypatch.setattr(i, 'ask', answer)
    return args, home


def test_install_manual_update_and_uninstall(fake_install, monkeypatch):
    args, home = fake_install
    i.install(args)
    state = json.loads((args.install_dir / 'install-state.json').read_text())
    config = Path(state['config'])
    assert config.stat().st_mode & 0o777 == 0o600
    plugin = home / '.config/opencode/plugins/zen-rotation/index.ts'
    assert 'http://127.0.0.1:18998/v1' in plugin.read_text()
    assert not state['service']
    monkeypatch.setattr(i, 'ask', lambda message, default=True: message.startswith('Установка уже'))
    i.install(args)
    monkeypatch.setattr(i, 'ask', lambda message, default=True: message.startswith('Удалить'))
    i.uninstall(args)
    assert not plugin.exists() and not config.exists()
    assert not (args.install_dir / 'install-state.json').exists()


def test_install_does_not_overwrite_foreign_plugin(fake_install):
    args, home = fake_install
    plugin = home / '.config/opencode/plugins/zen-rotation/index.ts'
    plugin.parent.mkdir(parents=True)
    plugin.write_text('user work')
    with pytest.raises(ValueError, match='not owned'):
        i.install(args)
    assert plugin.read_text() == 'user work'


def test_install_rollback_on_dependency_failure(fake_install, monkeypatch):
    args, home = fake_install
    def command(argv):
        if 'pip' in argv: raise RuntimeError('pip failed')
        return 'opencode v2.1.0'
    monkeypatch.setattr(i, 'run', command)
    with pytest.raises(RuntimeError): i.install(args)
    assert not (home / '.config/opencode/plugins/zen-rotation/index.ts').exists()
    assert not (args.install_dir / 'install-state.json').exists()


def test_update_refuses_active_requests(fake_install, monkeypatch):
    args, home = fake_install
    i.install(args)
    monkeypatch.setattr(i, 'ask', lambda *a: True)
    monkeypatch.setattr(i, 'health', lambda port: {'active': 1})
    with pytest.raises(ValueError, match='активные'):
        i.install(args)


def test_doctor_and_missing_install(fake_install, monkeypatch):
    args, home = fake_install
    with pytest.raises(ValueError, match='not found'): i.doctor(args)
    with pytest.raises(ValueError, match='not found'): i.uninstall(args)
    i.install(args)
    monkeypatch.setattr(i, 'health', lambda port: {'ok': True, 'active': 0})
    i.doctor(args)
    (home / '.local/bin/opencode-zen').unlink()
    with pytest.raises(ValueError, match='failed'): i.doctor(args)


def test_uninstall_preserves_user_edits(fake_install, monkeypatch):
    args, home = fake_install
    i.install(args)
    plugin = home / '.config/opencode/plugins/zen-rotation/index.ts'
    plugin.write_text('my changed plugin')
    monkeypatch.setattr(i, 'ask', lambda *a: True)
    i.uninstall(args)
    assert plugin.read_text() == 'my changed plugin'
    state = json.loads((args.install_dir / 'install-state.json').read_text())
    assert [r['path'] for r in state['files']] == [str(plugin)]


def test_alias_executes_launcher_with_spaces(tmp_path):
    launcher = tmp_path / 'my folder/launcher'
    launcher.parent.mkdir()
    launcher.write_text('#!/bin/sh\necho alias-ok\n')
    launcher.chmod(0o755)
    rc = tmp_path / 'bashrc'
    rc.write_text(i.alias_text('# custom\n', launcher))
    result = subprocess.check_output(['bash', '-O', 'expand_aliases', '-c', 'source "$1"\nopencode', 'bash', str(rc)], text=True)
    assert result.strip() == 'alias-ok'


def test_busy_port_foreign_config_and_cancel(fake_install, monkeypatch):
    args, home = fake_install
    monkeypatch.setattr(i, 'free_port', lambda port: False)
    with pytest.raises(ValueError, match='Port is in use'): i.install(args)
    monkeypatch.setattr(i, 'free_port', lambda port: True)
    config = home / '.config/opencode-zen-rotator/config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{"routes":["direct"]}')
    with pytest.raises(ValueError, match='already exists'): i.install(args)
    config.unlink()
    i.install(args)
    state = (args.install_dir / 'install-state.json').read_bytes()
    i.install(args)  # declining an update must not change the state
    assert (args.install_dir / 'install-state.json').read_bytes() == state
    i.uninstall(args)  # declining removal
    assert (args.install_dir / 'install-state.json').exists()


def test_service_install_update_and_remove(fake_install, monkeypatch):
    args, home = fake_install
    calls = []
    started = [False]
    monkeypatch.setattr(i.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(i.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(i, 'ask', lambda message, default=True: not message.startswith('Проверить') and not message.startswith('Оставить список'))
    def ctl(mode, *args):
        calls.append((mode, *args))
        if args[:1] == ('enable',): started[0] = True
    monkeypatch.setattr(i, 'systemctl', ctl)
    monkeypatch.setattr(i, 'health', lambda port: {'ok': True, 'active': 0} if started[0] else None)
    i.install(args)
    unit = home / '.config/systemd/user/zen-rate-gateway.service'
    assert unit.exists() and 'default.target' in unit.read_text()
    rc = home / '.bashrc'
    rc.write_text(rc.read_text() + '# user edit\n')
    i.install(args)
    assert rc.read_text().count('alias opencode=') == 1
    assert any('restart' in c for c in calls)
    i.uninstall(args)
    assert not unit.exists() and '# user edit' in rc.read_text()
    assert 'alias opencode=' not in rc.read_text()


def test_service_start_failure_rolls_back(fake_install, monkeypatch):
    args, home = fake_install
    monkeypatch.setattr(i.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(i.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(i, 'ask', lambda message, default=True: message.startswith('Установить'))
    calls = []
    monkeypatch.setattr(i, 'systemctl', lambda *a: calls.append(a))
    monkeypatch.setattr(i.time, 'sleep', lambda delay: None)
    with pytest.raises(RuntimeError, match='did not start'): i.install(args)
    assert not (home / '.config/opencode/plugins/zen-rotation/index.ts').exists()
    assert any('stop' in c for c in calls)


def test_no_systemd_and_unsupported_environment(fake_install, monkeypatch):
    args, home = fake_install
    monkeypatch.setattr(i, 'ask', lambda *a: True)
    monkeypatch.setattr(i.shutil, 'which', lambda *a: None)
    monkeypatch.setattr(i.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0))
    i.install(args)
    state = json.loads((args.install_dir / 'install-state.json').read_text())
    assert state['service'] is False
    monkeypatch.setattr(i.sys, 'platform', 'darwin')
    with pytest.raises(ValueError, match='Linux'): i.install(args)


def test_main_errors_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['install.py', 'update', '--install-dir', str(tmp_path)])
    assert i.main() == 1
    monkeypatch.setattr(sys, 'argv', ['install.py', 'doctor', '--install-dir', str(tmp_path)])
    assert i.main() == 1
    monkeypatch.setattr(i, 'doctor', lambda args: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert i.main() == 130
    monkeypatch.setattr(i, 'doctor', lambda args: None)
    assert i.main() == 0


def test_prompt_ask_and_command_helpers(monkeypatch):
    for value, expected in [('', True), ('y', True), ('да', True), ('n', False)]:
        monkeypatch.setattr('builtins.input', lambda *a: value)
        assert i.ask('Question') is expected
    calls = []
    monkeypatch.setattr(i, 'run', lambda argv: calls.append(argv))
    i.systemctl('user', 'status', 'example')
    assert calls == [['systemctl', '--user', 'status', 'example']]


def test_late_active_request_update_does_not_stop_service(fake_install, monkeypatch):
    args, home = fake_install
    i.install(args)
    state_path = args.install_dir / 'install-state.json'
    state = json.loads(state_path.read_text())
    state['service'] = True
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(i.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(i.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(i, 'ask', lambda message, default=True: message.startswith(('Установка уже', 'Установить')))
    checks = iter([{'active': 0}, {'active': 1}])
    monkeypatch.setattr(i, 'health', lambda port: next(checks))
    calls = []
    monkeypatch.setattr(i, 'systemctl', lambda *a: calls.append(a))
    with pytest.raises(ValueError, match='active requests appeared'):
        i.install(args)
    assert not any('stop' in c or 'restart' in c for c in calls)
