"""Check active local markdown links and canonical schema table count."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
failures = []
for path in [root / "README.md", *sorted((root / "docs").glob("*.md"))]:
    for dest in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
        if "://" in dest or dest.startswith("#"):
            continue
        target = path.parent / dest.split("#")[0]
        if not target.exists():
            failures.append(f"{path.name}: missing {dest}")
schema = (root / "sql/migrations/0001_init.sql").read_text(encoding="utf-8")
tables = re.findall(r"CREATE TABLE (\w+)\s*\(", schema)
if len(tables) != 18:
    failures.append(f"Expected 18 domain tables, got {len(tables)}")
for failure in failures:
    print(failure)
if failures:
    raise SystemExit(1)
print("PASS active documentation links and 18-table schema")
