"""Copy check for feature H (ADR-H, BR-BOOQY-057 style): the personalliggare
is never described as compliant, approved or certified anywhere a restaurant
or a guest can read it, until the legal review signs it off. (Developer
docstrings in this repo say exactly that rule, so they are not scanned.)

Reads the sibling repos when they are checked out beside this one (the
reviewer's setup, RULES.md); CI of this repo only has its own files, so a
missing sibling is skipped, like tests/test_contracts.py."""

import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent
SOURCES = {
    "admin-front-end": [APP.parent / "admin-front-end" / "src"],
    "booqy-console": [APP.parent / "booqy-console" / "web" / "src"],
    "site-kit": [APP.parent / "customer-front-end" / "packages" / "site-kit" / "src"],
    "booqy-website": [APP.parent / "booqy-website" / "site"],
}
SUFFIXES = {".ts", ".tsx", ".py", ".md", ".html", ".json", ".js"}
FORBIDDEN = re.compile(r"uppfyller|godkänd|godkand|certifierad|compliant|certified|approved by skatteverket",
                       re.IGNORECASE)
TOPIC = re.compile(r"personalliggare|staff ledger", re.IGNORECASE)
WINDOW = 400  # characters on either side of a mention


def _files(roots):
    for root in roots:
        if root.is_file():
            yield root
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.suffix in SUFFIXES and path.is_file() and "node_modules" not in path.parts \
                        and not path.name.endswith((".test.ts", ".test.tsx")):
                    yield path


def offences(text):
    found = []
    for mention in TOPIC.finditer(text):
        start, end = max(0, mention.start() - WINDOW), mention.end() + WINDOW
        found += [m.group(0) for m in FORBIDDEN.finditer(text, start, end)]
    return found


def test_the_checker_catches_and_passes():
    assert offences("Personalliggare som uppfyller kraven") == ["uppfyller"]
    assert offences("Vår personalliggare är Skatteverket-godkänd") == ["godkänd"]
    assert offences("Personalliggare (under granskning)") == []
    assert offences("Kassan är certifierad. " + "x" * 500 + " Personalliggare") == []


@pytest.mark.parametrize("repo", sorted(SOURCES))
def test_personalliggare_is_never_called_compliant(repo):
    roots = [r for r in SOURCES[repo] if r.exists()]
    if not roots:
        pytest.skip(f"{repo} not checked out beside application")
    bad = []
    for path in _files(roots):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not TOPIC.search(text):
            continue
        for word in offences(text):
            bad.append(f"{path}: {word}")
    assert bad == [], "personalliggare described as compliant/approved/certified:\n" + "\n".join(bad)
