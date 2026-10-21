import json

import pytest

from zen_rotator.config import Settings, config_home, default_config, validate_route

TOKEN = 'x' * 32


@pytest.mark.parametrize('route', ['direct', 'socks5://user:pass@proxy.example:1080', 'socks5h://proxy.example:1080', 'http://proxy.example:8080', 'https://proxy.example:443'])
def test_valid_routes(route):
    assert validate_route(route) == route


@pytest.mark.parametrize('route', [None, 1, '', 'file:///etc/passwd', 'socks5://host', 'socks5://host:99999', 'http://host:80/path', 'http://host:80?q=a', 'http://host:80#part'])
def test_invalid_routes_hide_input(route):
    with pytest.raises(ValueError, match='Invalid route'):
        validate_route(route)


@pytest.mark.parametrize('kwargs', [{'routes': ()}, {'routes': ('direct', 'direct')}, {'token': ''}, {'port': True}, {'port': 1}, {'max_body_bytes': 0}])
def test_invalid_settings(kwargs):
    values = {'routes': ('direct',), 'token': TOKEN}
    values.update(kwargs)
    with pytest.raises(ValueError):
        Settings(**values)


def test_load_and_default_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    assert config_home() == tmp_path
    assert default_config() == tmp_path / 'opencode-zen-rotator/config.json'
    path = tmp_path / 'test.json'
    path.write_text(json.dumps({'routes': ['direct'], 'token': TOKEN, 'port': 19000}))
    assert Settings.load(path).port == 19000
    for content in ['[]', '{}', 'bad json', '{"routes": ["http://private-secret@host"]}']:
        path.write_text(content)
        with pytest.raises(ValueError, match='Invalid configuration') as exc:
            Settings.load(path)
        assert 'private-secret' not in str(exc.value)
    with pytest.raises(ValueError, match='Cannot read'):
        Settings.load(tmp_path / 'missing')
