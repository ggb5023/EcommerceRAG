"""Read private deployment values without sourcing shell code."""
import argparse
import ipaddress
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid

KEYS = {'ECR_DEV_PRIVATE_IP', 'ECR_DB_PRIVATE_IP', 'ECR_DB_PUBLIC_IP', 'ECR_DATA_UUID'}


def require(name):
    if name not in KEYS:
        raise ValueError('Unknown infrastructure setting')
    values = {}
    path = Path(os.environ.get('ECR_INFRA_ENV', '/etc/ecommerce-rag/infra.env'))
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ValueError('Infrastructure configuration must be a regular file')
        if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
            raise ValueError('Infrastructure configuration requires restricted ownership and mode 600')
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, raw = line.partition('=')
            if not sep or key.strip() not in KEYS or key.strip() in values:
                raise ValueError('Invalid or duplicate infrastructure setting')
            tokens = shlex.split(raw, comments=True)
            if len(tokens) != 1:
                raise ValueError('Infrastructure values must be single literals')
            values[key.strip()] = tokens[0]
    value = os.environ.get(name, values.get(name, ''))
    if not value:
        raise ValueError('Missing required setting: '+name)
    if name == 'ECR_DATA_UUID':
        parsed = uuid.UUID(value)
        if not parsed.int or str(parsed) != value.lower():
            raise ValueError('Invalid data-volume UUID')
    else:
        address = ipaddress.IPv4Address(value)
        if address.is_unspecified or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved:
            raise ValueError('Invalid infrastructure address')
        if name.endswith('PRIVATE_IP'):
            private_ranges = (ipaddress.IPv4Network((0x0A000000, 8)), ipaddress.IPv4Network((0xAC100000, 12)), ipaddress.IPv4Network((0xC0A80000, 16)))
            if not any(address in network for network in private_ranges):
                raise ValueError('Private infrastructure address must be RFC1918')
        elif not address.is_global:
            raise ValueError('Public probe requires a public address')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('names', nargs='+', choices=sorted(KEYS))
    args = parser.parse_args()
    try:
        values = [require(name) for name in args.names]
    except (ValueError, OSError) as exc:
        # Only exception type is shown: parse errors may contain a private value.
        print('UNVERIFIED infrastructure configuration: '+type(exc).__name__, file=sys.stderr)
        return 2
    if not args.check:
        if len(values) != 1:
            parser.error('Request one value or use --check')
        print(values[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
