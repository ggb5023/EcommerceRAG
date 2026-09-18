"""Create credentials once. Never print secrets or overwrite existing values."""
import os
from pathlib import Path
import secrets
from infra_config import require

root = Path("/etc/ecommerce-rag")
if os.geteuid() != 0 or not root.is_dir():
    raise SystemExit("root and pre-created protected directory required")
try:
    db_ip = require('ECR_DB_PRIVATE_IP')
except (ValueError, OSError):
    raise SystemExit('UNVERIFIED private infrastructure configuration')
values = {}
for name in ("pg_admin_password", "pg_app_password", "redis_password"):
    target = root / name
    if not target.exists():
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    values[name] = target.read_text().strip()
    if len(values[name]) != 64 or any(c not in "0123456789abcdef" for c in values[name]):
        raise SystemExit("Unexpected existing credential format; preserve and inspect manually")
redis = root / "redis.conf"
if not redis.exists():
    fd = os.open(redis, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "w") as stream:
        stream.write("bind 0.0.0.0\nprotected-mode yes\nport 6379\ndir /data\n"
                     "appendonly yes\nappendfsync everysec\nmaxmemory 512mb\n"
                     "maxmemory-policy noeviction\nrequirepass " + values["redis_password"] + "\n")
dev = root / "dev.env"
if not dev.exists():
    fd = os.open(dev, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("PGHOST=" + db_ip + "\nPGPORT=5432\nPGDATABASE=rag\nPGUSER=rag_app\n"
                     "PGPASSWORD=" + values["pg_app_password"] + "\nREDIS_HOST=" + db_ip + "\n"
                     "REDIS_PASSWORD=" + values["redis_password"] + "\n")
print("Credential files ready; existing values preserved")
