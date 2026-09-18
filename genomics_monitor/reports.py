from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .db import connect


def initial_report() -> dict:
    with connect() as db:
        variants = db.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        rows = db.execute(
            """SELECT e.source,e.category,e.evidence_level,e.clinical_significance
               FROM evidence e WHERE EXISTS (
                 SELECT 1 FROM variants v WHERE
                 (e.rsid IS NOT NULL AND e.rsid=v.rsid) OR
                 (e.chrom IS NOT NULL AND e.chrom=v.chrom AND e.pos=v.pos AND e.ref=v.ref AND e.alt=v.alt)
               )"""
        ).fetchall()
        imports = [dict(row) for row in db.execute(
            "SELECT source,source_version,completed_at,status,records_scanned,matched_records FROM evidence_sync ORDER BY id DESC"
        ).fetchall()]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "indexed_calls": variants,
        "evidence_matches": len(rows),
        "by_source": dict(Counter(row["source"] for row in rows)),
        "by_category": dict(Counter(row["category"] for row in rows)),
        "by_evidence_level": dict(Counter(row["evidence_level"] for row in rows)),
        "by_clinical_significance": dict(Counter((row["clinical_significance"] or "not supplied") for row in rows)),
        "latest_syncs": imports,
        "interpretation": "Evidence inventory only; matches are not diagnoses or personalised medical advice.",
    }
