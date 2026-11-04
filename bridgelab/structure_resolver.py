"""Offline NSC structure resolution and coverage manifesting."""
import csv
import hashlib
import json
import os

from rdkit import Chem


def resolve_structure_file(path):
    rows, invalid = [], []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            raw = row.get("molfile", "")
            mol = Chem.MolFromMolBlock(raw, sanitize=True, removeHs=True)
            if mol is None:
                invalid.append(row.get("nsc"))
                continue
            rows.append({
                "nsc": str(row["nsc"]),
                "smiles": Chem.MolToSmiles(mol, canonical=True),
                "formula": row.get("formula"),
                "name": row.get("name"),
            })
    rows.sort(key=lambda item: int(item["nsc"]))
    return rows, invalid


def build_manifest(manifest, structure_path, candidate_path):
    structures, invalid = resolve_structure_file(structure_path)
    with open(candidate_path, newline="") as handle:
        candidate_nsc = {row["nsc1"] for row in csv.DictReader(handle)}
        handle.seek(0)
        candidate_nsc.update(row["nsc2"] for row in csv.DictReader(handle))
    resolved = {item["nsc"] for item in structures}
    coverage = len(resolved & candidate_nsc) / max(1, len(candidate_nsc))
    with open(structure_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    result = dict(manifest)
    result.update({
        "molecular_structure_available": coverage >= 0.95,
        "structure_field": "smiles",
        "structure_file_hash": digest,
        "structure_source": os.path.relpath(structure_path, os.path.dirname(candidate_path)),
        "structure_coverage": round(coverage, 6),
        "structure_count": len(structures),
        "structure_invalid_count": len(invalid),
        "structure_status": "ready" if coverage >= 0.95 else "structure_needed",
    })
    return result, structures


def write_resolved_map(source_path, output_path):
    rows, invalid = resolve_structure_file(source_path)
    with open(output_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["nsc", "smiles", "formula", "name"])
        writer.writeheader()
        writer.writerows(rows)
    return {"resolved": len(rows), "invalid": invalid}


if __name__ == "__main__":
    import sys
    print(json.dumps(write_resolved_map(sys.argv[1], sys.argv[2]), indent=2))
