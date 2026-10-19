#!/usr/bin/env python3
"""Check software source hygiene and documentation without opening site settings."""
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

SOFTWARE = Path(__file__).resolve().parents[1]
REPOSITORY = SOFTWARE.parent
names = subprocess.check_output([
    'git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'
], cwd=REPOSITORY).decode().split('\0')
errors = []
patterns = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
            r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{40,}', r'AKIA[0-9A-Z]{16}']
for name in sorted(set(filter(None, names))):
    path = REPOSITORY / name
    if not name.startswith('software/') or not path.is_file():
        continue
    parts = path.relative_to(SOFTWARE).parts
    if (path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'
            or any(part.startswith('.venv') or part in {'node_modules', '__pycache__', 'secrets', 'backups', '.runtime', 'dist', 'build'} for part in parts)
            or path.suffix in {'.deb', '.so', '.sqlite', '.sqlite3', '.dump', '.key', '.pem'}):
        errors.append('Private/runtime file included: ' + name)
    if path.stat().st_size > 20 * 1024 * 1024:
        errors.append('Large generated software file included: ' + name)
    try:
        body = path.read_text()
    except (UnicodeError, OSError):
        continue
    if any(re.search(pattern, body) for pattern in patterns):
        errors.append('Secret signature found: ' + name)
    if ('/home/' + 'shn/') in body:
        errors.append('Personal absolute path: ' + name)
for relative in ('README.md', 'service/README.md', 'database/README.md', 'frontend/README.md',
                 'host/README.md', 'docs/operations.md', 'docs/data-workflow.md',
                 'host/docs/build.md', 'host/docs/plc-integration.md'):
    path = SOFTWARE / relative
    if not path.exists():
        errors.append('Missing document: ' + relative)
        continue
for name in sorted(set(filter(None, names))):
    path = REPOSITORY / name
    if path.suffix != '.md' or not path.is_file():
        continue
    for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
        if target.startswith('<') and '>' in target:
            target = target[1:target.index('>')]
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        local_path = unquote(parsed.path)
        linked = REPOSITORY / local_path.lstrip('/') if local_path.startswith('/') else path.parent / local_path
        if not linked.exists():
            errors.append(f'Broken link in {name}: {target}')
if errors:
    raise SystemExit('\n'.join(errors))
print('PASS software file hygiene and repository documentation links')
