from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import tempfile
import urllib.request
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone

from .db import connect
from .evidence import ingest

DEFAULT_URL = "https://ftp.ebi.ac.uk/pub/databases/gwas/releases/latest/gwas-catalog-associations_ontology-annotated-full.zip"


def _called_alleles(row) -> set[str]:
    called = {part for part in (row["genotype"] or "").replace("|", "/").split("/") if part != "."}
    alleles: set[str] = set()
    if "0" in called:
        alleles.add(row["ref"])
    if str(row["alt_index"]) in called:
        alleles.add(row["alt"])
    return alleles


def _present_effect_alleles(parsed: list[tuple]) -> dict[int, set[str]]:
    present: dict[int, set[str]] = {}
    with connect() as db:
        db.execute("CREATE TEMP TABLE gwas_batch(row_id INTEGER, rsid TEXT, chrom TEXT, pos INTEGER)")
        db.executemany(
            "INSERT INTO gwas_batch VALUES(?,?,?,?)",
            [(index, rsid, chrom, pos) for index, (_, rsid, _, chrom, pos) in enumerate(parsed)],
        )
        matches = db.execute("""
          SELECT b.row_id,v.ref,v.alt,v.alt_index,v.genotype
          FROM gwas_batch b JOIN variants v ON b.chrom=v.chrom AND b.pos=v.pos
          UNION
          SELECT b.row_id,v.ref,v.alt,v.alt_index,v.genotype
          FROM gwas_batch b JOIN variants v ON b.rsid=v.rsid
          WHERE b.rsid IS NOT NULL
        """).fetchall()
        for row in matches:
            present.setdefault(row["row_id"], set()).update(_called_alleles(row))
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
        chrom = (row.get("CHR_ID") or "").removeprefix("chr")
        position = row.get("CHR_POS") or ""
        try:
            pos = int(position)
        except ValueError:
            pos = None
        parsed.append((row, rsid, effect.upper(), chrom or None, pos))
    present = _present_effect_alleles(parsed)
    records = []
    for index, (row, rsid, effect, chrom, pos) in enumerate(parsed):
        if effect not in present.get(index, set()):
            continue
        trait = row.get("MAPPED_TRAIT") or row.get("DISEASE/TRAIT") or "Unlabelled trait"
        identity = "|".join((row.get("PUBMEDID", ""), row.get("STUDY ACCESSION", ""), rsid, effect, trait))
        record_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        records.append({
            "source": "GWAS Catalog", "source_record_id": record_id, "source_version": release,
            "title": f"{trait} association", "summary": f"Literature-curated GWAS association for {rsid}-{effect}; not a clinical classification.",
            "category": "research", "evidence_level": "single_study", "rsid": rsid,
            "chrom": chrom, "pos": pos,
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
        catalog_url = url or os.getenv("GWAS_TSV_URL", DEFAULT_URL)
        request = urllib.request.Request(catalog_url, headers={"User-Agent": "genomics-monitor/0.3"})
        with ExitStack() as stack:
            response = stack.enter_context(urllib.request.urlopen(request, timeout=180))
            if catalog_url.lower().endswith(".zip"):
                archive_file = stack.enter_context(tempfile.TemporaryFile())
                shutil.copyfileobj(response, archive_file)
                archive_file.seek(0)
                archive = stack.enter_context(zipfile.ZipFile(archive_file))
                candidates = [name for name in archive.namelist() if name.lower().endswith((".tsv", ".txt")) and not name.endswith("/")]
                if not candidates:
                    raise ValueError("GWAS archive contains no tabular association file")
                source = stack.enter_context(archive.open(max(candidates, key=lambda name: archive.getinfo(name).file_size)))
            else:
                source = response
            reader = csv.DictReader(io.TextIOWrapper(source, encoding="utf-8-sig"), delimiter="\t")
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
