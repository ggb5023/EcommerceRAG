"""Probe public database protocols without sending any credentials."""
import socket

failed = False
for port, payload in [(5432, bytes.fromhex('0000000804d2162f')), (6379, b'PING\r\n')]:
    try:
        with socket.create_connection(('192.0.2.20', port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(payload)
            response = sock.recv(128)
        if response:
            print(f'FAIL public port {port} responded: {response!r}')
            failed = True
        else:
            print(f'PASS public protocol {port} closed without response')
    except (TimeoutError, ConnectionError, OSError):
        print(f'PASS public protocol {port} unavailable from this probe source')
print('Cloud security-group configuration was not read by this probe.')
raise SystemExit(1 if failed else 0)
