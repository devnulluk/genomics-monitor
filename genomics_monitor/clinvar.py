from __future__ import annotations

import gzip
import json
import os
import urllib.request
from datetime import datetime, timezone

from .db import connect
from .evidence import ingest


DEFAULT_URL = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"


def _info(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, item = part.partition("=")
        if separator:
            result[key] = item
    return result


def _text(value: str | None) -> str:
    return (value or "").replace("_", " ").replace("|", "; ")


def _level(review_status: str) -> str:
    value = review_status.casefold()
    if "practice_guideline" in value:
        return "guideline"
    if "expert_panel" in value:
        return "expert_panel"
    if "multiple_submitters" in value and "conflict" not in value:
        return "replicated"
    if "conflict" in value:
        return "conflicting"
    return "single_study"


def _category(significance: str) -> str:
    value = significance.casefold()
    if "uncertain" in value or "conflict" in value:
        return "uncertain"
    if any(term in value for term in ("pathogenic", "drug_response", "risk_factor")):
        return "clinical"
    return "health"


def _called_alt(genotype: str | None, alt_index: int) -> bool:
    if not genotype or genotype in {"./.", ".|."}:
        return False
    return str(alt_index) in genotype.replace("|", "/").split("/")


def _flush(rows: list[tuple], release: str | None) -> tuple[int, int]:
    if not rows:
        return 0, 0
    with connect() as db:
        db.execute("CREATE TEMP TABLE clinvar_batch(row_id INTEGER, chrom TEXT, pos INTEGER, ref TEXT, alt TEXT, payload TEXT)")
        db.executemany("INSERT INTO clinvar_batch VALUES(?,?,?,?,?,?)", rows)
        matches = db.execute("""
          SELECT b.payload,v.genotype,v.alt_index FROM clinvar_batch b JOIN variants v
            ON b.chrom=v.chrom AND b.pos=v.pos AND b.ref=v.ref AND b.alt=v.alt
        """).fetchall()
    records = []
    for match in matches:
        if not _called_alt(match["genotype"], match["alt_index"]):
            continue
        record = json.loads(match["payload"])
        record["source_version"] = release
        records.append(record)
    result = ingest(records) if records else {"inserted": 0}
    return len(records), int(result["inserted"])


def sync_clinvar(url: str | None = None) -> dict:
    source_url = url or os.getenv("CLINVAR_VCF_URL", DEFAULT_URL)
    started = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        cursor = db.execute("INSERT INTO evidence_sync(source,started_at,status) VALUES('ClinVar',?,'running')", (started,))
        sync_id = cursor.lastrowid
    scanned = matched = inserted = 0
    release: str | None = None
    batch: list[tuple] = []
    try:
        request = urllib.request.Request(source_url, headers={"User-Agent": "genomics-monitor/0.1 (+https://github.com/devnulluk/genomics-monitor)"})
        with urllib.request.urlopen(request, timeout=60) as response, gzip.GzipFile(fileobj=response) as compressed:
            for raw in compressed:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line.startswith("##fileDate="):
                    release = line.split("=", 1)[1]
                if line.startswith("#"):
                    continue
                fields = line.split("\t")
                if len(fields) < 8:
                    continue
                chrom, pos, record_id, ref, alts, _, _, info_text = fields[:8]
                details = _info(info_text)
                significance = _text(details.get("CLNSIG")) or "not provided"
                review = details.get("CLNREVSTAT", "no_assertion_criteria_provided")
                traits = _text(details.get("CLNDN")) or "ClinVar assertion"
                for alt in alts.split(","):
                    scanned += 1
                    record = {
                        "source": "ClinVar",
                        "source_record_id": details.get("ALLELEID") or record_id or f"{chrom}:{pos}:{ref}:{alt}",
                        "title": f"{significance}: {traits}",
                        "summary": f"ClinVar aggregate classification: {significance}. Review status: {_text(review)}.",
                        "category": _category(significance),
                        "evidence_level": _level(review),
                        "clinical_significance": significance,
                        "chrom": chrom.removeprefix("chr"),
                        "pos": int(pos),
                        "ref": ref,
                        "alt": alt,
                        "effect_allele": alt,
                        "trait_id": details.get("CLNDISDB"),
                        "trait_label": traits,
                        "url": f"https://www.ncbi.nlm.nih.gov/clinvar/?term={chrom}-{pos}-{ref}-{alt}%28GRCh38%29",
                    }
                    batch.append((scanned, record["chrom"], record["pos"], ref, alt, json.dumps(record, separators=(",", ":"))))
                if len(batch) >= 5000:
                    new_matches, new_inserted = _flush(batch, release)
                    matched += new_matches
                    inserted += new_inserted
                    batch.clear()
                    if scanned % 100000 == 0:
                        with connect() as db:
                            db.execute("UPDATE evidence_sync SET source_version=?,records_scanned=?,matched_records=?,inserted_records=? WHERE id=?", (release, scanned, matched, inserted, sync_id))
            new_matches, new_inserted = _flush(batch, release)
            matched += new_matches
            inserted += new_inserted
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
        row = db.execute("SELECT source,source_version,started_at,completed_at,status,records_scanned,matched_records,inserted_records,error FROM evidence_sync WHERE source='ClinVar' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None
