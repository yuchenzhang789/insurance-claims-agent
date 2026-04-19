"""Hand-curated ICD-10 / procedure-code consistency table.

In production this would call CMS NCCI edits or a medical-coding service.
For the demo we encode the sample-claim pairs plus a minimal set of rules.
"""
from __future__ import annotations

# Diagnosis family → procedure codes that are clinically reasonable.
# A procedure is flagged inconsistent if the dx root is NOT in this map
# OR the procedure is not listed for that dx.
CONSISTENT_PAIRS: dict[str, set[str]] = {
    # M54.5 — low back pain
    "M54": {"MRI-LS-SPINE", "PHYSICAL-THERAPY", "EPIDURAL-INJECTION", "OFFICE-VISIT"},
    # E11 — type-2 diabetes
    "E11": {"OFFICE-VISIT", "LAB-HBA1C", "LIPID-PANEL"},
    # J45 — asthma
    "J45": {"BIOLOGIC-INJECTION", "PULMONARY-FUNCTION-TEST", "OFFICE-VISIT", "ORAL-STEROID"},
    # L03 — cellulitis
    "L03": {"ORAL-ANTIBIOTIC", "IV-ANTIBIOTIC", "OFFICE-VISIT", "INCISION-DRAINAGE"},
    # F32 — major depressive episode
    "F32": {"PSYCH-THERAPY", "OFFICE-VISIT", "PSYCH-EVAL"},
    # S72 — femur / hip fracture
    "S72": {"HIP-REPLACEMENT", "ORIF-FEMUR", "X-RAY-HIP", "OFFICE-VISIT"},
    # E78 — lipid disorders
    "E78": {"LIPID-PANEL", "OFFICE-VISIT"},
    # K35 — appendicitis
    "K35": {"APPENDECTOMY", "CT-ABDOMEN", "OFFICE-VISIT"},
    # Z00 — general examination (preventive only)
    "Z00": {"OFFICE-VISIT", "LIPID-PANEL", "LAB-CBC"},
}


def dx_root(icd_code: str) -> str:
    """Get the 3-char ICD root, e.g. 'M54.5' → 'M54'."""
    return icd_code.split(".")[0][:3].upper()


def codes_consistent(diagnosis_code: str, procedure_code: str) -> tuple[bool, str]:
    """
    Returns (is_consistent, explanation).
    Treat unknown dx roots as "not verifiable" → consistent=True (don't block on
    missing knowledge), but flag in the explanation so the agent can consider it.
    """
    root = dx_root(diagnosis_code)
    proc = procedure_code.upper()
    if root not in CONSISTENT_PAIRS:
        return True, f"Diagnosis root {root} not in coding table; mismatch not verified."
    allowed = CONSISTENT_PAIRS[root]
    if proc in allowed:
        return True, f"Diagnosis {diagnosis_code} and procedure {procedure_code} are consistent per coding table."
    return (
        False,
        f"Diagnosis {diagnosis_code} (root {root}) is NOT consistent with procedure {procedure_code}. "
        f"Expected procedures for this dx root: {sorted(allowed)}.",
    )
