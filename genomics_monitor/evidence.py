from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .db import connect


ALLOWED_CATEGORIES = {"clinical", "health", "trait", "ancestry", "research", "uncertain"}
ALLOWED_LEVELS = {"guideline", "expert_panel", "replicated", "single_study", "preprint", "conflicting"}


def ingest(records: list[dict]) -> dict:
    inserted = 0
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        for record in records:
            category = record.get("category", "research")
            level = record.get("evidence_level", "single_study")
            if category not in ALLOWED_CATEGORIES or level not in ALLOWED_LEVELS:
                raise ValueError("Unsupported category or evidence level")
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            columns = (
                "source", "source_record_id", "source_version", "title", "summary", "category",
                "evidence_level", "clinical_significance", "chrom", "pos", "ref", "alt", "rsid",
                "effect_allele", "trait_id", "trait_label", "population", "effect_size", "p_value", "url"
            )
            values = [record.get(name) for name in columns]
            cur = db.execute(
                f"INSERT OR IGNORE INTO evidence({','.join(columns)},observed_at,payload_sha256) VALUES({','.join('?' for _ in range(len(columns)+2))})",
                values + [now, digest],
            )
            inserted += cur.rowcount
    return {"received": len(records), "inserted": inserted}


def findings(limit: int = 100) -> list[dict]:
    query = """
    SELECT e.*, v.chrom AS called_chrom, v.pos AS called_pos, v.ref AS called_ref,
      v.alt AS called_alt, v.alt_index, v.genotype, v.phased, v.filter_status, v.quality,
      CASE e.category WHEN 'clinical' THEN 1 WHEN 'health' THEN 2 WHEN 'trait' THEN 3
           WHEN 'ancestry' THEN 4 WHEN 'research' THEN 5 ELSE 6 END AS tier_rank
    FROM evidence e JOIN variants v ON
      (e.rsid IS NOT NULL AND e.rsid=v.rsid) OR
      (e.chrom IS NOT NULL AND e.chrom=v.chrom AND e.pos=v.pos AND e.ref=v.ref AND e.alt=v.alt) OR
      (e.source='GWAS Catalog' AND e.chrom=v.chrom AND e.pos=v.pos)
    ORDER BY tier_rank, e.observed_at DESC LIMIT ?
    """
    with connect() as db:
        rows = [dict(row) for row in db.execute(query, (limit,)).fetchall()]
    for row in rows:
        row["match_basis"] = "verified_import_match" if row.get("source") == "GWAS Catalog" else ("rsid" if row.get("rsid") else "exact_locus_and_alleles")
        genotype = row.get("genotype")
        effect = row.get("effect_allele")
        if not genotype or genotype in {"./.", ".|."}:
            row["effect_allele_status"] = "unknown_no_call"
        elif not effect:
            row["effect_allele_status"] = "not_specified"
        else:
            called_indexes = {part for part in genotype.replace("|", "/").split("/") if part != "."}
            present = (effect == row["called_ref"] and "0" in called_indexes) or (
                effect == row["called_alt"] and str(row["alt_index"]) in called_indexes
            )
            row["effect_allele_status"] = "present" if present else "not_present"
    return rows
