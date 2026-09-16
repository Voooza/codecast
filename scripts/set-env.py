#!/usr/bin/env python3
"""Set KEY=value in .env by whole-line match.

Substring replacement on names is a trap: "OPENAI_API_KEY=" is a suffix of
"AZURE_OPENAI_API_KEY=", which silently corrupts the wrong line.
"""
import sys
from pathlib import Path

if len(sys.argv) < 3:
    sys.exit("usage: set-env.py KEY VALUE [KEY VALUE ...]")

path = Path(__file__).resolve().parents[1] / ".env"
lines = path.read_text().splitlines()
pairs = dict(zip(sys.argv[1::2], sys.argv[2::2]))

for i, line in enumerate(lines):
    name = line.split("=", 1)[0]
    if name in pairs:
        lines[i] = f"{name}={pairs.pop(name)}"
for name, value in pairs.items():
    lines.append(f"{name}={value}")

path.write_text("\n".join(lines) + "\n")
print("set:", " ".join(sorted(set(sys.argv[1::2]))))
