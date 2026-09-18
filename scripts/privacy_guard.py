"""Check tracked content and reachable history without printing matched values."""
import argparse
import ipaddress
from pathlib import PurePosixPath
import re
import subprocess
import sys

PUBLIC_NAME = 'EcommerceRAG Maintainers'
PUBLIC_EMAIL = 'maintainers@example.invalid'
PATTERNS = (
    rb'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----',
    rb'LTAI[A-Za-z0-9]{16,}',
    rb'sk-[A-Za-z0-9._-]{24,}',
    rb'tvly-[A-Za-z0-9_-]{24,}',
)
IP = re.compile(rb'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])')
UUID = re.compile(rb'\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b')
EXAMPLES = tuple(ipaddress.ip_network(network) for network in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))


def git(*args, data=None):
    return subprocess.check_output(['git', *args], input=data, stderr=subprocess.PIPE)


def blocked_path(name):
    path = PurePosixPath(name)
    lower = name.lower()
    base = path.name.lower()
    private_dirs = {'docs', 'archive', 'backups', '.private', '.local', '.codex', '.workbuddy-ai', '.ssh', '.venv', 'node_modules'}
    if any(part.lower() in private_dirs for part in path.parts):
        return True
    if path.suffix.lower() in {'.md', '.markdown', '.doc', '.docx', '.pem', '.key', '.dump', '.bundle', '.log', '.bak'} and name != 'README.md':
        return True
    if (base.startswith('.env') or base.endswith('.env')) and not base.endswith('.example'):
        return True
    if base.startswith('id_ed25519') or base.startswith('id_rsa'):
        return True
    return any(word in lower for word in ('chat-history', 'chat_history', 'conversation-history', 'conversation_history'))


def private_content(data):
    if any(re.search(pattern, data) for pattern in PATTERNS):
        return True
    if b'\0' in data:
        return False
    if re.search(rb'(?i)(?<![A-Za-z0-9_])[A-Z]:[/\\][A-Za-z0-9_]', data):
        return True
    if any(value != b'00000000-0000-0000-0000-000000000000' for value in UUID.findall(data)):
        return True
    for value in IP.findall(data):
        try:
            address = ipaddress.ip_address(value.decode('ascii'))
        except ValueError:
            continue
        if not (address.is_loopback or address.is_unspecified or any(address in network for network in EXAMPLES)):
            return True
    return False


def scan_entries(entries, seen):
    failures = []
    for name, oid in entries:
        if blocked_path(name):
            failures.append('private path: '+name)
        if oid not in seen:
            seen.add(oid)
            if private_content(git('cat-file', 'blob', oid)):
                failures.append('private content in: '+name)
    return failures


def tree_entries(commit):
    for item in git('ls-tree', '-r', '-z', commit).split(b'\0'):
        if item:
            meta, name = item.split(b'\t', 1)
            _, kind, oid = meta.split()
            if kind == b'blob':
                yield name.decode('utf-8'), oid.decode('ascii')


def index_entries():
    for item in git('ls-files', '--stage', '-z').split(b'\0'):
        if item:
            meta, name = item.split(b'\t', 1)
            _, oid, stage = meta.split()
            if stage != b'0':
                raise ValueError('Unmerged index cannot be checked')
            yield name.decode('utf-8'), oid.decode('ascii')


def check_identity():
    prefix = (PUBLIC_NAME+' <'+PUBLIC_EMAIL+'> ').encode()
    return all(git('var', kind).startswith(prefix) for kind in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'))


def scan_history(extra_refs=()):
    failures = []
    seen = set()
    commits = git('rev-list', '--all', *extra_refs, '--').decode().splitlines()
    for commit in commits:
        metadata = git('show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce%x00%B', commit).split(b'\0', 4)
        expected = [PUBLIC_NAME.encode(), PUBLIC_EMAIL.encode()] * 2
        if metadata[:4] != expected or private_content(metadata[4]):
            failures.append('private commit metadata: '+commit[:12])
        failures.extend(scan_entries(tree_entries(commit), seen))
    for line in git('for-each-ref', '--format=%(objecttype) %(objectname)', 'refs/tags').decode().splitlines():
        kind, oid = line.split()
        if kind == 'tag':
            data = git('cat-file', 'tag', oid)
            tagger = re.search(rb'^tagger (.*?) <(.*?)> ', data, re.M)
            if private_content(data) or (tagger and list(tagger.groups()) != [PUBLIC_NAME.encode(), PUBLIC_EMAIL.encode()]):
                failures.append('private tag metadata: '+oid[:12])
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--pre-push', action='store_true')
    parser.add_argument('--identity', action='store_true')
    args = parser.parse_args()
    try:
        extra = []
        if args.pre_push:
            for line in sys.stdin:
                fields = line.split()
                if len(fields) != 4 or not re.fullmatch('[0-9a-f]{40,64}', fields[1]):
                    raise ValueError('Invalid push input')
                if set(fields[1]) != {'0'}:
                    extra.append(fields[1])
        failures = scan_history(extra) if args.history or args.pre_push else scan_entries(index_entries(), set())
        if args.identity and not check_identity():
            failures.append('repository commit identity must be the public project identity')
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('BLOCKED privacy check could not complete; details withheld')
        return 2
    for failure in sorted(set(failures)):
        print('BLOCKED '+failure)
    if failures:
        return 1
    print('PASS privacy exclusions, credential patterns, environment values and requested metadata')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
