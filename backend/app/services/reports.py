"""Forensic report generation with a verifiable chain-of-custody hash.

The hash is of the *finished PDF bytes*, computed once and stored. Anyone
holding the file can run `sha256sum` and compare against the digest printed
inside the report and returned by the API - if the file were altered after
export, the recomputed digest would not match the stored one.

fpdf2 is used because it is pure Python: no system libraries to install, which
matters for a container that has to build reproducibly.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Case, CaseNote, CaseWallet, Evidence, Report, TraceRun, User, Wallet

logger = logging.getLogger(__name__)
settings = get_settings()

STORAGE = Path("/app/storage")
REPORT_DIR = STORAGE / "reports"

SYNTHETIC_NOTICE = (
    "DEMONSTRATION DOCUMENT. This report was produced by a prototype built for "
    "Smart India Hackathon 2026 (SIH26183). The complaint and blockchain data it "
    "describes are synthetic or drawn from public datasets. It contains no real "
    "NCRP complaint data and no exchange KYC data, and it is not a law-enforcement "
    "record."
)

RECOMMENDATION_NOTICE = (
    "Findings are automated recommendations. No freeze, disclosure or enforcement "
    "action is taken by this system. Any such action requires explicit approval by "
    "an authorised officer."
)


def _ascii(text: str) -> str:
    """fpdf2's core fonts are Latin-1; drop anything they cannot encode.

    Wallet addresses and case numbers are ASCII, so this only ever affects free
    text an investigator typed.
    """
    return (text or "").encode("latin-1", "replace").decode("latin-1")


class ForensicPDF(FPDF):
    def __init__(self, case_number: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.case_number = case_number
        self.set_auto_page_break(auto=True, margin=18)

    def header(self):
        self.set_font("Helvetica", "B", 13)
        self.cell(
            0, 8, "Cryptocurrency Fraud Trace - Forensic Report",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.set_font("Helvetica", "", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 4, f"SIH26183 prototype | Case {self.case_number}",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        self.cell(0, 4, "SYNTHETIC / PUBLIC-DATASET DEMONSTRATION - not a law-enforcement record",
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)

    # -- helpers --------------------------------------------------------
    def h2(self, text: str):
        self.ln(2)
        self.set_font("Helvetica", "B", 10.5)
        self.cell(0, 6, _ascii(text), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)

    def kv(self, key: str, value: str):
        self.set_font("Helvetica", "B", 9)
        self.cell(45, 5, _ascii(key), new_x="RIGHT", new_y="TOP")
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, _ascii(str(value)), new_x="LMARGIN", new_y="NEXT")

    def para(self, text: str, size: float = 9):
        self.set_font("Helvetica", "", size)
        self.multi_cell(0, 4.6, _ascii(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def notice(self, text: str):
        self.set_fill_color(250, 243, 219)
        self.set_font("Helvetica", "I", 8)
        self.multi_cell(0, 4.2, _ascii(text), border=1, fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)


def _gather(db: Session, case: Case) -> dict:
    wallets = list(
        db.execute(
            select(Wallet, CaseWallet.role)
            .join(CaseWallet, CaseWallet.wallet_id == Wallet.id)
            .where(CaseWallet.case_id == case.id)
        ).all()
    )
    traces = list(
        db.execute(
            select(TraceRun).where(TraceRun.case_id == case.id).order_by(TraceRun.started_at)
        ).scalars().all()
    )
    notes = list(
        db.execute(
            select(CaseNote).where(CaseNote.case_id == case.id).order_by(CaseNote.created_at)
        ).scalars().all()
    )
    exhibits = list(
        db.execute(
            select(Evidence).where(Evidence.case_id == case.id).order_by(Evidence.uploaded_at)
        ).scalars().all()
    )
    return {"wallets": wallets, "traces": traces, "notes": notes, "exhibits": exhibits}


def generate_case_report(
    db: Session, case: Case, generated_by: User | None = None, analysis: dict | None = None
) -> Report:
    """Render the case to PDF, hash the bytes, and record the artifact."""
    data = _gather(db, case)
    pdf = ForensicPDF(case.case_number)
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.notice(SYNTHETIC_NOTICE)

    pdf.h2("1. Case")
    pdf.kv("Case number", case.case_number)
    pdf.kv("Status", case.status)
    pdf.kv("Source", case.source)
    pdf.kv("NCRP reference", case.ncrp_ref or "not supplied")
    pdf.kv("Reported at", case.reported_at.isoformat() if case.reported_at else "-")
    pdf.kv("Victim reference", case.victim_ref or "not supplied (pseudonymous only)")
    pdf.kv("Amount (INR)", f"{case.amount_inr}" if case.amount_inr is not None else "not stated")
    if case.narrative:
        pdf.ln(1)
        pdf.para(f"Narrative: {case.narrative}")

    pdf.h2("2. Reported addresses")
    if data["wallets"]:
        for wallet, role in data["wallets"]:
            pdf.kv(f"{wallet.chain} ({role})", wallet.address)
    else:
        pdf.para("None recorded.")

    pdf.h2("3. Trace runs")
    if data["traces"]:
        for t in data["traces"]:
            pdf.kv(
                "Trace",
                f"depth {t.max_depth} | status {t.status} | hops {t.hops_discovered} | "
                f"addresses {t.addresses_touched} | mixer contact: "
                f"{'yes' if t.mixer_interaction else 'no'} | data source: {t.data_source}",
            )
    else:
        pdf.para("No trace has been run for this case.")

    if analysis:
        pdf.h2("4. Attribution and risk")
        attribution = analysis.get("attribution") or {}
        pdf.kv("Attribution method", attribution.get("method", "none"))
        pdf.kv("Entity", attribution.get("entity_name") or "not named")
        pdf.kv("Entity type", attribution.get("entity_type") or "-")
        pdf.kv("Tag source", attribution.get("source") or "-")
        pdf.kv("Confidence", str(attribution.get("confidence", "-")))
        pdf.kv("Risk label", str(analysis.get("risk_label", "-")).upper())
        pdf.kv("Risk score", f"{analysis.get('risk_score', 0)} / 100")

        if attribution.get("method") == "classifier":
            pdf.para(
                "Attribution came from the behavioural classifier, not a curated tag. The service "
                "category is a suggestion; no entity is named because behaviour alone cannot "
                "identify one."
            )

        contributions = analysis.get("contributions") or []
        if contributions:
            pdf.h2("5. Cases contributing to the risk score")
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(42, 5, "Case", border=1)
            pdf.cell(26, 5, "Reported", border=1)
            pdf.cell(16, 5, "Age", border=1, align="R")
            pdf.cell(24, 5, "Decay wt", border=1, align="R")
            pdf.cell(20, 5, "Points", border=1, align="R")
            pdf.ln()
            pdf.set_font("Helvetica", "", 8)
            for c in contributions[:30]:
                pdf.cell(42, 5, _ascii(str(c.get("case_number", ""))), border=1)
                pdf.cell(26, 5, str(c.get("reported_at", ""))[:10], border=1)
                pdf.cell(16, 5, f"{c.get('age_days', 0)}d", border=1, align="R")
                pdf.cell(24, 5, f"{c.get('decay_weight', 0):.4f}", border=1, align="R")
                pdf.cell(20, 5, f"{c.get('points', 0):.2f}", border=1, align="R")
                pdf.ln()
            pdf.ln(1)
            pdf.para(
                "Score = sum of points, plus aggravating factors, mapped onto 0-100 by a "
                "saturating curve. The figures above are sufficient to recompute it."
            )

    pdf.h2("6. Case notes")
    if data["notes"]:
        for n in data["notes"]:
            stamp = n.created_at.isoformat() if n.created_at else ""
            pdf.para(f"[{stamp}] {n.body}")
    else:
        pdf.para("No notes recorded.")

    pdf.h2("7. Evidence exhibits (chain of custody)")
    if data["exhibits"]:
        for e in data["exhibits"]:
            pdf.kv(e.filename, f"sha256 {e.sha256} | {e.size_bytes} bytes | {e.uploaded_at}")
    else:
        pdf.para("No exhibits attached.")

    pdf.h2("8. Integrity and limitations")
    generated_at = datetime.now(UTC)
    pdf.kv("Generated at", generated_at.isoformat())
    pdf.kv("Generated by", generated_by.username if generated_by else "system")
    pdf.para(
        "The SHA-256 digest of this document is returned by the export API and stored against "
        "the case record. To verify this file has not been altered, recompute the digest of the "
        "PDF and compare it with the stored value."
    )
    pdf.notice(RECOMMENDATION_NOTICE)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = bytes(pdf.output())
    digest = hashlib.sha256(payload).hexdigest()

    filename = f"{case.case_number}_{generated_at.strftime('%Y%m%dT%H%M%SZ')}_{digest[:12]}.pdf"
    path = REPORT_DIR / filename
    path.write_bytes(payload)

    report = Report(
        case_id=case.id,
        kind="forensic_pdf",
        sha256=digest,
        storage_path=str(path),
        generated_by=generated_by.id if generated_by else None,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    logger.info("forensic report %s written for %s (sha256 %s)", filename, case.case_number, digest)
    return report


def verify_report(report: Report) -> dict:
    """Recompute the digest of the stored file and compare with the record."""
    path = Path(report.storage_path)
    if not path.exists():
        return {"verified": False, "reason": "report file is missing from storage"}
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "verified": actual == report.sha256,
        "stored_sha256": report.sha256,
        "recomputed_sha256": actual,
        "size_bytes": path.stat().st_size,
    }
