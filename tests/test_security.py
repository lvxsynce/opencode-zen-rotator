import importlib.util
from pathlib import Path
from types import SimpleNamespace


def test_scanner_reports_filename_not_secret(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('secret_scan', Path(__file__).resolve().parents[1] / 'scripts/check_secrets.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    monkeypatch.setattr(scanner, 'ROOT', tmp_path)
    monkeypatch.setattr(scanner.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=b''))
    secret = 'gh' + 'p_' + 'TESTONLY' * 5
    (tmp_path / 'unsafe.txt').write_text(secret)
    assert scanner.scan() == 1
    out = capsys.readouterr().out
    assert 'unsafe.txt' in out and secret not in out
    (tmp_path / 'unsafe.txt').unlink()
    (tmp_path / 'safe.txt').write_text('public example')
    assert scanner.scan() == 0


def test_scanner_rejects_private_filenames_and_realistic_proxy(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('secret_scan2', Path(__file__).resolve().parents[1] / 'scripts/check_secrets.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    monkeypatch.setattr(scanner, 'ROOT', tmp_path)
    monkeypatch.setattr(scanner.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=b'auth.json\x00proxy.txt\x00'))
    (tmp_path / 'auth.json').write_text('{}')
    (tmp_path / 'proxy.txt').write_text('socks5://' + 'someone:' + 'never-publish-this' + '@host.example:1080')
    assert scanner.scan() == 1
