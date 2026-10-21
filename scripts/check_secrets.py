#!/usr/bin/env python3
"""Small pre-publish check. Prints filenames only, never matched secrets."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP = {'.git', '.venv', '__pycache__', '.pytest_cache', 'node_modules', 'build', 'dist', 'htmlcov'}
RULES = [
    re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{30,}'),
    re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    re.compile(r'(?i)(?:socks5h?|https?)://[^/\s:@]+:([^@/\s]+)@'),
]
FORBIDDEN = {'config.json', 'zen-proxies.json', 'auth.json', '.env', 'install-state.json', 'opencode.json', 'opencode.jsonc'}


def scan():
    tracked = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True).stdout
    paths = [ROOT / p for p in tracked.decode().split('\x00') if p] if tracked else list(ROOT.rglob('*'))
    failed = []
    for path in paths:
        relative = path.relative_to(ROOT)
        if any(part in SKIP or part.endswith('.egg-info') for part in relative.parts) or path.name.startswith('.coverage') or not path.is_file():
            continue
        if path.name in FORBIDDEN or path.suffix in {'.log', '.bak'}:
            failed.append(str(relative))
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            failed.append(str(relative))
            continue
        # Test fixtures and public examples contain deliberately fake proxy data.
        for rule in RULES:
            for match in rule.finditer(text):
                if rule is RULES[-1] and match.group(1) == 'PASSWORD' and '@proxy.example:' in match.group(0) + text[match.end():match.end() + 30]:
                    continue
                if rule is RULES[-1] and match.group(1) in {'pass', 'private-password'} and relative.parts[0] == 'tests':
                    continue
                failed.append(str(relative))
    if failed:
        print('Review these files before publishing:', *sorted(set(failed)), sep='\n')
        return 1
    print('No obvious secrets found. Review the staged diff as well.')
    return 0


if __name__ == '__main__':
    raise SystemExit(scan())
