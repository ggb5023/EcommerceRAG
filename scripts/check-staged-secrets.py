"""Reject credential files and common secret signatures in the actual Git index."""
import re
import subprocess
from pathlib import PurePosixPath

names = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM', '-z']).split(b'\0')
patterns = [rb'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----', rb'LTAI[A-Za-z0-9]{16,}', rb'sk-[A-Za-z0-9]{24,}', rb'tvly-[A-Za-z0-9-]{24,}']
blocked = []
for raw in filter(None, names):
    name = raw.decode('utf-8')
    path = PurePosixPath(name)
    if path.suffix in ('.pem', '.key', '.dump') or (path.name.startswith('.env') and path.name != '.env.example') or any(x in path.parts for x in ('.local', '.workbuddy-ai', '.venv', 'node_modules')):
        blocked.append(name)
        continue
    data = subprocess.check_output(['git', 'show', ':' + name])
    if any(re.search(pattern, data) for pattern in patterns):
        blocked.append(name)
if blocked:
    print('BLOCKED files (contents withheld):', ', '.join(blocked))
    raise SystemExit(1)
print('PASS staged file exclusions and common credential signatures; manual scope review still required')
