"""PDF → section-aware chunks. Builds a per-plan chunk index."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from pypdf import PdfReader


@dataclass
class Chunk:
    plan: str
    section: str
    page: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


# Map filename → plan label
PLAN_FILES = {
    "blueshield_ppo.pdf": "Blue Shield PPO",
    "blueshield_epo.pdf": "Blue Shield EPO",
    "blueshield_hmo.pdf": "Blue Shield HMO",
}

# Heuristic section-heading patterns derived from Blue Shield doc structure.
# First pattern that matches a line is used as the current section.
HEADING_PATTERNS = [
    re.compile(r"^(Table of contents)$", re.I),
    re.compile(r"^(Notice)$", re.I),
    re.compile(r"^(General disclosures)$", re.I),
    re.compile(r"^(Principal Benefits(?: and coverages)?)$", re.I),
    re.compile(r"^(Principal exclusions and limitations on Benefits)$", re.I),
    re.compile(r"^(General exclusions and limitations)$", re.I),
    re.compile(r"^(Prepayments? fees?)$", re.I),
    re.compile(r"^(Other charges)$", re.I),
    re.compile(r"^(Calendar Year Deductible)$", re.I),
    re.compile(r"^(Copayment and Coinsurance)$", re.I),
    re.compile(r"^(Calendar Year Out-of-Pocket Maximum)$", re.I),
    re.compile(r"^(Choice of Physicians and providers)$", re.I),
    re.compile(r"^(Second medical opinion)$", re.I),
    re.compile(r"^(Continuity of care)$", re.I),
    re.compile(r"^(Care outside of California)$", re.I),
    re.compile(r"^(Emergency Services)$", re.I),
    re.compile(r"^(Reimbursement provisions)$", re.I),
    re.compile(r"^(Facilities)$", re.I),
    re.compile(r"^(Renewal provisions)$", re.I),
    re.compile(r"^(Individual continuation of Benef[ti]ts)$", re.I),
    re.compile(r"^(Termination of Benefits)$", re.I),
    # SBC-specific
    re.compile(r"^(Common Medical Event)$", re.I),
    re.compile(r"^(Important Questions)$", re.I),
    re.compile(r"^(Excluded Services)$", re.I),
    re.compile(r"^(Your Rights to Continue Coverage)$", re.I),
    re.compile(r"^(Your Grievance and Appeals Rights)$", re.I),
    # Numbered exclusion items — treat as named sub-headings within the parent
    re.compile(r"^(Clinical Trials\.)", re.I),
    re.compile(r"^(Cosmetic Services[^.]*\.)", re.I),
    re.compile(r"^(Custodial or Domiciliary Care\.)", re.I),
    re.compile(r"^(Dental Services\.)", re.I),
    re.compile(r"^(Dietary or Nutritional Supplements\.)", re.I),
    re.compile(r"^(Disposable Supplies for Home Use\.)", re.I),
    re.compile(r"^(Experimental or Investigational services\.)", re.I),
    re.compile(r"^(Vision Care\.)", re.I),
    re.compile(r"^(Hearing Aids\.)", re.I),
    re.compile(r"^(Immunizations\.)", re.I),
    re.compile(r"^(Non-licensed or Non-certified Providers\.)", re.I),
    re.compile(r"^(Private Duty Nursing\.)", re.I),
    re.compile(r"^(Personal or Comfort Items\.)", re.I),
    re.compile(r"^(Reversal of Voluntary Sterilization\.)", re.I),
    re.compile(r"^(Surrogate Pregnancy\.)", re.I),
    re.compile(r"^(Therapies\.)", re.I),
    re.compile(r"^(Routine Physical Examination\.)", re.I),
    re.compile(r"^(Travel and Lodging\.)", re.I),
    re.compile(r"^(Weight Control Programs and Exercise Programs\.)", re.I),
]


def detect_heading(line: str) -> str | None:
    s = line.strip()
    if len(s) < 3 or len(s) > 120:
        return None
    for pat in HEADING_PATTERNS:
        m = pat.match(s)
        if m:
            return m.group(1).rstrip(".")
    return None


def extract_page_blocks(pdf_path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, full_text)] per page."""
    reader = PdfReader(str(pdf_path))
    pages = []
    for i, p in enumerate(reader.pages, start=1):
        try:
            txt = p.extract_text() or ""
        except Exception:
            txt = ""
        pages.append((i, txt))
    return pages


def chunk_pdf(pdf_path: Path, plan: str, window: int = 900, overlap: int = 150) -> list[Chunk]:
    """
    Produce section-tagged chunks from a PDF.
    Algorithm:
      1. Per page, scan lines for section headings; carry "current section" forward.
      2. Accumulate text into fixed-size windows (char-based for simplicity)
         and emit a Chunk tagged with the section current at window start.
    """
    chunks: list[Chunk] = []
    current_section = "Document Start"
    buf = ""
    buf_page = 1
    buf_section = current_section

    pages = extract_page_blocks(pdf_path)
    for page_num, page_text in pages:
        for line in page_text.splitlines():
            heading = detect_heading(line)
            if heading:
                # Flush current buffer before switching section
                if buf.strip():
                    chunks.append(
                        Chunk(plan=plan, section=buf_section, page=buf_page, text=buf.strip())
                    )
                    buf = ""
                current_section = heading
                buf_section = heading
                buf_page = page_num
                continue
            if not buf:
                buf_page = page_num
                buf_section = current_section
            buf += line + " "
            # Emit when window is full
            if len(buf) >= window:
                chunks.append(
                    Chunk(plan=plan, section=buf_section, page=buf_page, text=buf.strip())
                )
                # Keep overlap tail
                buf = buf[-overlap:] if overlap else ""
                buf_page = page_num
                buf_section = current_section
    # Final flush
    if buf.strip():
        chunks.append(Chunk(plan=plan, section=buf_section, page=buf_page, text=buf.strip()))
    return chunks


def build_index(policies_dir: Path, out_path: Path) -> dict:
    """Build { plan_name: [chunk_dict, ...] } and write JSON."""
    index: dict[str, list[dict]] = {}
    for fname, plan in PLAN_FILES.items():
        pdf = policies_dir / fname
        if not pdf.exists():
            print(f"[ingest] skipping missing file: {pdf}")
            continue
        chunks = chunk_pdf(pdf, plan)
        index[plan] = [c.to_dict() for c in chunks]
        print(f"[ingest] {plan}: {len(chunks)} chunks from {fname}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(index, indent=2))
    print(f"[ingest] wrote {out_path}")
    return index


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    build_index(root / "policies", root / "data" / "chunks.json")
