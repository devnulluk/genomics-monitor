from __future__ import annotations

import csv
import hashlib
import io
import os
import urllib.request
from datetime import datetime, timezone

from .db import connect
from .evidence import ingest

DEFAULT_URL = "https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/gwas-catalog-associations_ontology-annotated.tsv"


def _present_effect_alleles(rsids: list[str]) -> dict[str, set[str]]:
    present: dict[str, set[str]] = {}
    with connect() as db:
        for start in range(0, len(rsids), 500):
            chunk = rsids[start:start + 500]
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            for row in db.execute(f"SELECT rsid,ref,alt,alt_index,genotype FROM variants WHERE rsid IN ({marks})", chunk):
                called = {part for part in (row["genotype"] or "").replace("|", "/").split("/") if part != "."}
                alleles = present.setdefault(row["rsid"], set())
                if "0" in called:
                    alleles.add(row["ref"])
                if str(row["alt_index"]) in called:
                    alleles.add(row["alt"])
    return present


def _flush(rows: list[dict], release: str) -> tuple[int, int]:
    parsed = []
    for row in rows:
        strongest = row.get("STRONGEST SNP-RISK ALLELE", "")
        if "-" not in strongest:
            continue
        rsid, effect = strongest.rsplit("-", 1)
        if not rsid.startswith("rs") or not effect or effect == "?":
            continue
        parsed.append((row, rsid, effect.upper()))
    present = _present_effect_alleles(sorted({rsid for _, rsid, _ in parsed}))
    records = []
    for row, rsid, effect in parsed:
        if effect not in present.get(rsid, set()):
            continue
        trait = row.get("MAPPED_TRAIT") or row.get("DISEASE/TRAIT") or "Unlabelled trait"
        identity = "|".join((row.get("PUBMEDID", ""), row.get("STUDY ACCESSION", ""), rsid, effect, trait))
        record_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        records.append({
            "source": "GWAS Catalog", "source_record_id": record_id, "source_version": release,
            "title": f"{trait} association", "summary": f"Literature-curated GWAS association for {rsid}-{effect}; not a clinical classification.",
            "category": "research", "evidence_level": "single_study", "rsid": rsid,
            "effect_allele": effect, "trait_id": row.get("MAPPED_TRAIT_URI") or None,
            "trait_label": trait, "population": row.get("INITIAL SAMPLE SIZE") or None,
            "effect_size": row.get("OR or BETA") or None, "p_value": row.get("P-VALUE") or None,
            "url": row.get("LINK") or f"https://www.ebi.ac.uk/gwas/search?query={rsid}",
        })
    return len(records), ingest(records)["inserted"] if records else 0


def sync_gwas(url: str | None = None, max_pages: int | None = None) -> dict:
    del max_pages
    started = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        sync_id = db.execute("INSERT INTO evidence_sync(source,started_at,status) VALUES('GWAS Catalog',?,'running')", (started,)).lastrowid
    scanned = matched = inserted = 0
    release = datetime.now(timezone.utc).date().isoformat()
    try:
        request = urllib.request.Request(url or os.getenv("GWAS_TSV_URL", DEFAULT_URL), headers={"User-Agent": "genomics-monitor/0.3"})
        with urllib.request.urlopen(request, timeout=180) as response:
            reader = csv.DictReader(io.TextIOWrapper(response, encoding="utf-8"), delimiter="\t")
            batch = []
            for row in reader:
                batch.append(row); scanned += 1
                if len(batch) >= 5000:
                    found, added = _flush(batch, release)
                    matched += found; inserted += added; batch.clear()
                    with connect() as db:
                        db.execute("UPDATE evidence_sync SET source_version=?,records_scanned=?,matched_records=?,inserted_records=? WHERE id=?", (release, scanned, matched, inserted, sync_id))
            if batch:
                found, added = _flush(batch, release)
                matched += found; inserted += added
        completed = datetime.now(timezone.utc).isoformat()
        with connect() as db:
            db.execute("UPDATE evidence_sync SET source_version=?,completed_at=?,status='complete',records_scanned=?,matched_records=?,inserted_records=? WHERE id=?", (release, completed, scanned, matched, inserted, sync_id))
        return {"sync_id": sync_id, "status": "complete", "source_version": release, "records_scanned": scanned, "matched_records": matched, "inserted_records": inserted}
    except Exception as exc:
        with connect() as db:
            db.execute("UPDATE evidence_sync SET completed_at=?,status='failed',records_scanned=?,matched_records=?,inserted_records=?,error=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), scanned, matched, inserted, type(exc).__name__, sync_id))
        raise


def latest_sync() -> dict | None:
    with connect() as db:
        row = db.execute("SELECT source,source_version,started_at,completed_at,status,records_scanned,matched_records,inserted_records,error FROM evidence_sync WHERE source='GWAS Catalog' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
