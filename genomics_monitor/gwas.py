from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from .db import connect
from .evidence import ingest

DEFAULT_URL = "https://www.ebi.ac.uk/gwas/rest/api/v2/associations?size=250"


def _present_effect_alleles(rsids: list[str]) -> dict[str, set[str]]:
    present: dict[str, set[str]] = {}
    if not rsids:
        return present
    with connect() as db:
        for start in range(0, len(rsids), 500):
            chunk = rsids[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in db.execute(f"SELECT rsid,ref,alt,alt_index,genotype FROM variants WHERE rsid IN ({marks})", chunk):
                genotype = row["genotype"] or ""
                called = {part for part in genotype.replace("|", "/").split("/") if part != "."}
                alleles = present.setdefault(row["rsid"], set())
                if "0" in called:
                    alleles.add(row["ref"])
                if str(row["alt_index"]) in called:
                    alleles.add(row["alt"])
    return present


def sync_gwas(url: str | None = None, max_pages: int | None = None) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        sync_id = db.execute("INSERT INTO evidence_sync(source,started_at,status) VALUES('GWAS Catalog',?,'running')", (started,)).lastrowid
    scanned = matched = inserted = pages = 0
    next_url = url or os.getenv("GWAS_API_URL", DEFAULT_URL)
    release = datetime.now(timezone.utc).date().isoformat()
    try:
        while next_url and (max_pages is None or pages < max_pages):
            request = urllib.request.Request(next_url, headers={"Accept": "application/json", "User-Agent": "genomics-monitor/0.3"})
            with urllib.request.urlopen(request, timeout=180) as response:
                page = json.load(response)
            associations = page.get("_embedded", {}).get("associations", [])
            scanned += len(associations)
            rsids = sorted({item.get("rs_id") for association in associations for item in association.get("snp_allele", []) if item.get("rs_id")})
            present = _present_effect_alleles(rsids)
            records = []
            for association in associations:
                traits = association.get("efo_traits") or []
                trait_label = "; ".join(item.get("efo_trait", "") for item in traits if item.get("efo_trait")) or "; ".join(association.get("reported_trait") or []) or "Unlabelled trait"
                trait_id = ";".join(item.get("efo_id", "") for item in traits if item.get("efo_id")) or None
                for allele in association.get("snp_allele") or []:
                    rsid, effect = allele.get("rs_id"), allele.get("effect_allele")
                    if not rsid or not effect or effect not in present.get(rsid, set()):
                        continue
                    record_id = f"{association.get('association_id')}:{rsid}:{effect}"
                    pvalue = association.get("p_value")
                    records.append({
                        "source": "GWAS Catalog", "source_record_id": record_id, "source_version": release,
                        "title": f"{trait_label} association", "summary": f"Literature-curated GWAS association for {rsid}-{effect}; not a clinical classification.",
                        "category": "research", "evidence_level": "single_study", "rsid": rsid,
                        "effect_allele": effect, "trait_id": trait_id, "trait_label": trait_label,
                        "effect_size": association.get("beta") or association.get("or_per_copy_num"),
                        "p_value": str(pvalue) if pvalue is not None else None,
                        "url": f"https://www.ebi.ac.uk/gwas/associations/{association.get('association_id')}",
                    })
            matched += len(records)
            inserted += ingest(records)["inserted"] if records else 0
            pages += 1
            next_url = page.get("_links", {}).get("next", {}).get("href")
            with connect() as db:
                db.execute("UPDATE evidence_sync SET source_version=?,records_scanned=?,matched_records=?,inserted_records=? WHERE id=?", (release, scanned, matched, inserted, sync_id))
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
