"""Probe public database protocols without sending any credentials."""
import errno
import socket
from infra_config import require

try:
    probe_host = require('ECR_DB_PUBLIC_IP')
except (ValueError, OSError):
    print('UNVERIFIED public probe configuration')
    raise SystemExit(2)

failed = False
unverified = False
for port, payload in [(5432, bytes.fromhex('0000000804d2162f')), (6379, b'PING\r\n')]:
    try:
        with socket.create_connection((probe_host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(payload)
            response = sock.recv(128)
        if response:
            print(f'FAIL public port {port} responded')
            failed = True
        else:
            print(f'PASS public protocol {port} closed without response')
    except (TimeoutError, ConnectionRefusedError, ConnectionResetError):
        print(f'PASS public protocol {port} unavailable from this probe source')
    except OSError as exc:
        if exc.errno in (errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT):
            print(f'PASS public protocol {port} unavailable from this probe source')
        else:
            print(f'UNVERIFIED public protocol {port}: local network error')
            unverified = True
print('Cloud security-group configuration was not read by this probe.')
raise SystemExit(1 if failed else 2 if unverified else 0)
