"""Print per-file and combined SHA-256 for the files named in design.json["packet_files"].

The combined hash is what the E0004 seal commits to. Run it at sealing time;
any later edit to a listed file changes it. Usage: python3 scripts/prereg_packet_hash.py [design.json]
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(path):
    design = json.loads(Path(path).read_text())
    parts = []
    for name in sorted(design["packet_files"]):
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        parts.append("%s  %s" % (digest, name))
        print(parts[-1])
    print("combined", hashlib.sha256("\n".join(parts).encode()).hexdigest())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ROOT / "comparison-prereg-v5" / "design.json")
