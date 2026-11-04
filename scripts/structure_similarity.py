"""RDKit structure comparison for the two named Evidence cases (NSC 3053, NSC 702294).

Inputs are the CIR SMILES shown in the Evidence tab. Output is written to
outputs/structure_similarity.json and is labeled as a structure calculation:
it is not used for ranking, docking, or any binding claim.
Run: python3 scripts/structure_similarity.py
"""
import json
from pathlib import Path

from rdkit import Chem, DataStructs, rdBase
from rdkit.Chem import Crippen, Descriptors, rdFingerprintGenerator, rdMolDescriptors

CIR_SMILES = {
    "A": ("NSC 3053", "Dactinomycin", 'CC(C)C1NC(=O)C(NC(=O)c2ccc(C)c3OC4=C(C)C(=O)C(=C(C(=O)NC5C(C)OC(=O)C(C(C)C)N(C)C(=O)CN(C)C(=O)C6CCCN6C(=O)C(NC5=O)C(C)C)C4=Nc23)N)C(C)OC(=O)C(C(C)C)N(C)C(=O)CN(C)C(=O)C7CCCN7C1=O'),
    "B": ("NSC 702294", "Estramustine sodium phosphate", '[Na].C[C@]12CC[C@H]3[C@@H](CCc4cc(OC(=O)N(CCCl)CCCl)ccc34)[C@@H]1CC[C@@H]2O[P](O)(O)=O'),
}


def parent(mol):
    frags = Chem.GetMolFrags(mol, asMols=True)
    return max(frags, key=lambda m: m.GetNumHeavyAtoms())


def describe(key):
    nsc, name, smi = CIR_SMILES[key]
    full = Chem.MolFromSmiles(smi)
    if full is None:
        raise SystemExit("RDKit could not parse " + nsc)
    par = parent(full)
    return {
        "nsc": nsc,
        "name": name,
        "parent_smiles": Chem.MolToSmiles(par),
        "mol_weight": round(Descriptors.MolWt(par), 2),
        "logp_crippen": round(Crippen.MolLogP(par), 2),
        "tpsa": round(rdMolDescriptors.CalcTPSA(par), 1),
        "hbd": rdMolDescriptors.CalcNumHBD(par),
        "hba": rdMolDescriptors.CalcNumHBA(par),
        "rotatable_bonds": rdMolDescriptors.CalcNumRotatableBonds(par),
        "heavy_atoms": par.GetNumHeavyAtoms(),
        "ring_count": rdMolDescriptors.CalcNumRings(par),
    }, par


def main():
    a, ma = describe("A")
    b, mb = describe("B")
    gen = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048)
    fp = gen.GetFingerprint
    fa, fb = fp(ma), fp(mb)
    out = {
        "rdkit_version": rdBase.rdkitVersion,
        "method": "Morgan fingerprint, radius 2, 2048 bits, Tanimoto; salts removed (largest fragment kept)",
        "tanimoto": round(DataStructs.TanimotoSimilarity(fa, fb), 3),
        "bits_on": {"A": fa.GetNumOnBits(), "B": fb.GetNumOnBits(), "shared": len(set(fa.GetOnBits()) & set(fb.GetOnBits()))},
        "compounds": {"A": a, "B": b},
        "scope": "Structure calculation only. Not used for ranking. Not docking. Not binding evidence.",
    }
    path = Path(__file__).resolve().parent.parent / "outputs" / "structure_similarity.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
