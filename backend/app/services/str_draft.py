"""FIU-IND-style Suspicious Transaction Report (STR) draft generator.

Produces a **draft for a human to review, edit and file**. This system does not
submit anything to FIU-IND, has no connection to FinNet, and the layout below is
modelled on the publicly documented structure of an STR narrative rather than
copied from any controlled form.

The draft deliberately states what the system does *not* know - it has no KYC,
no account holder, no PAN - because an STR narrative that quietly omits its own
evidential gaps is worse than one that names them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Case, StrDraft, User

logger = logging.getLogger(__name__)

HEADER = "SUSPICIOUS TRANSACTION REPORT - DRAFT (NOT FILED)"

DISCLAIMER = (
    "This is an automatically generated DRAFT produced by a Smart India Hackathon "
    "prototype (SIH26183). It has not been reviewed, approved or filed with "
    "FIU-IND. The underlying complaint and blockchain data are synthetic or drawn "
    "from public datasets; this draft contains no real NCRP complaint data and no "
    "exchange KYC data. A designated officer must review, complete and file any "
    "actual STR through the prescribed channel."
)


def build_str_body(
    case: Case,
    *,
    address: str,
    chain: str,
    entity_name: str | None,
    entity_type: str | None,
    attribution_method: str,
    attribution_source: str | None,
    risk_label: str,
    risk_score: float,
    contributing_cases: list[str],
    mixer_interaction: bool,
    hops: int | None = None,
) -> str:
    now = datetime.now(UTC)
    counterparty = entity_name or "an unidentified virtual asset service provider"
    amount_text = case.amount_inr if case.amount_inr is not None else "not stated"

    lines = [
        HEADER,
        "=" * 72,
        "",
        f"Draft reference        : STR-DRAFT-{case.case_number}",
        f"Generated (UTC)        : {now.isoformat()}",
        f"Originating case       : {case.case_number}",
        f"NCRP reference         : {case.ncrp_ref or 'not supplied'}",
        f"Reported on            : {case.reported_at.isoformat() if case.reported_at else '-'}",
        "",
        "PART A - SUBJECT OF REPORT",
        "-" * 72,
        f"Suspect wallet address : {address}",
        f"Blockchain             : {chain}",
        f"Reported amount (INR)  : {amount_text}",
        f"Complainant reference  : {case.victim_ref or 'pseudonymous reference not supplied'}",
        "",
        "PART B - COUNTERPARTY / DESTINATION",
        "-" * 72,
        f"Attributed entity      : {counterparty}",
        f"Entity category        : {entity_type or 'undetermined'}",
        f"Attribution basis      : {attribution_method}"
        + (f" (source: {attribution_source})" if attribution_source else ""),
        "",
        "PART C - GROUNDS FOR SUSPICION",
        "-" * 72,
    ]

    grounds = []
    if hops is not None:
        grounds.append(
            f"Funds deposited to the reported address were traced across {hops} hop(s) "
            f"to a deposit address associated with {counterparty}."
        )
    grounds.append(
        f"The destination carries a fraud-linkage score of {risk_score:.1f}/100 "
        f"({risk_label.upper()}), computed by time-decayed aggregation of victim-reported "
        f"cases whose traced funds terminate at the same cluster."
    )
    if contributing_cases:
        grounds.append(
            f"{len(contributing_cases)} separate reported case(s) terminate at this destination, "
            "indicating a repeat destination rather than an isolated incident."
        )
    if mixer_interaction:
        grounds.append(
            "The traced flow interacted with an address tagged as a mixing service. This is "
            "recorded as a layering indicator; no attempt was made to defeat the mixing."
        )
    if attribution_method == "classifier":
        grounds.append(
            "NOTE: the destination was NOT matched to a curated tag. Its category was inferred "
            "from transaction behaviour and no entity is named. This ground is weaker than a "
            "sourced attribution and should be corroborated before filing."
        )

    for i, g in enumerate(grounds, 1):
        lines.append(f"{i}. {g}")

    lines += [
        "",
        "PART D - CONTRIBUTING CASE REFERENCES",
        "-" * 72,
    ]
    lines += [f"  - {c}" for c in contributing_cases[:40]] or ["  (none)"]
    if len(contributing_cases) > 40:
        lines.append(f"  ... and {len(contributing_cases) - 40} more")

    lines += [
        "",
        "PART E - LIMITATIONS OF THIS DRAFT",
        "-" * 72,
        "1. No KYC or account-holder information was available to this system. The",
        "   subscriber behind the destination address is NOT established here and must",
        "   be obtained from the service provider through lawful process.",
        "2. Address attribution derives from public tagged-address datasets and",
        "   clustering heuristics. Clustering is probabilistic and can over-merge.",
        "3. No enforcement action has been taken. This draft recommends review only.",
        "",
        "PART F - OFFICER ACTION REQUIRED",
        "-" * 72,
        "  [ ] Reviewed by designated officer",
        "  [ ] Subject details verified against source records",
        "  [ ] Approved for filing with FIU-IND",
        "",
        "Officer name : ______________________   Designation : ______________________",
        "Signature    : ______________________   Date        : ______________________",
        "",
        "=" * 72,
        DISCLAIMER,
    ]
    return "\n".join(lines)


def create_str_draft(
    db: Session,
    case: Case,
    *,
    created_by: User | None,
    entity_id=None,
    **kwargs,
) -> StrDraft:
    body = build_str_body(case, **kwargs)
    draft = StrDraft(
        case_id=case.id,
        entity_id=entity_id,
        body=body,
        status="draft",
        created_by=created_by.id if created_by else None,
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    logger.info("STR draft %s created for case %s", draft.id, case.case_number)
    return draft
