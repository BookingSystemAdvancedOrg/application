#!/usr/bin/env bash
# Runs the Kassa test suite and prints a Markdown summary to paste into
# docs/kassa/TESTPROTOKOLL.md (section "Körning av automatiska tester").
#
#   bash scripts/kassa-testprotokoll.sh            (from the repository root)
#
# Needs the dev requirements (requirements-dev.txt) in the active Python.
# The run against fiskaly's TEST environment is separate (opt-in, needs AWS
# credentials that can read the fiskaly secret):
#   KASSA_FISKALY_LIVE_TEST=1 python -m pytest -s -q --tb=short -p no:faulthandler \
#     tests/test_control_system_fiskaly.py -k test_environment
set -uo pipefail
[ -d functions/kassa ] || { echo "run from the application repository root" >&2; exit 2; }
PYTHON="${PYTHON:-python}"
REPORT="$(mktemp -t kassa-junit.XXXXXX).xml"
"$PYTHON" -m pytest -q tests/test_kassa.py tests/test_kassa_xml.py tests/test_control_system_fiskaly.py \
  tests/test_card_payments.py \
  -p no:cacheprovider --junitxml="$REPORT" >/dev/null 2>&1
STATUS=$?
"$PYTHON" - "$REPORT" "$STATUS" <<'PY'
import subprocess, sys, xml.etree.ElementTree as ET
from datetime import datetime, timezone
report, status = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, ".")
from shared import kassa
root = ET.parse(report).getroot()
cases = root.iter("testcase")
rows, counts = [], {"passed": 0, "failed": 0, "skipped": 0}
for case in cases:
    outcome = "failed" if case.find("failure") is not None or case.find("error") is not None \
        else "skipped" if case.find("skipped") is not None else "passed"
    counts[outcome] += 1
    rows.append((f"{case.get('classname').split('.')[-1]}.py::{case.get('name')}", outcome))
sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or "?"
dirty = " (med ej incheckade ändringar)" if subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                                            text=True).stdout.strip() else ""
print("### Körning av automatiska tester\n")
print(f"- Datum: {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}")
print(f"- Programversion: Booqy Kassa {kassa.KASSA_VERSION}, commit {sha}{dirty}")
print(f"- Python {sys.version.split()[0]}")
print(f"- Resultat: {counts['passed']} godkända, {counts['failed']} underkända, {counts['skipped']} hoppade över"
      f" ({'GODKÄND' if status == 0 and counts['failed'] == 0 else 'UNDERKÄND'})\n")
print("| Test | Resultat |\n|---|---|")
for name, outcome in rows:
    print(f"| `{name}` | {'Godkänd' if outcome == 'passed' else 'Underkänd' if outcome == 'failed' else 'Överhoppad'} |")
PY
rm -f "$REPORT"
exit "$STATUS"
