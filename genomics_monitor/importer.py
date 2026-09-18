from __future__ import annotations

import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .db import connect


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def import_vcf(path_text: str, genome_build: str) -> dict:
    path = Path(path_text).resolve()
    if not path.is_file():
        raise ValueError("VCF file does not exist")
    digest_builder = hashlib.sha256()
    with path.open("rb") as raw:
        for chunk in iter(lambda: raw.read(8 * 1024 * 1024), b""):
            digest_builder.update(chunk)
    digest = digest_builder.hexdigest()
    sample_column = None
    sample_count = 0
    contig_lengths: dict[str, int] = {}
    variant_count = 0
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        existing = db.execute("SELECT id, variant_count FROM imports WHERE source_sha256=?", (digest,)).fetchone()
        if existing:
            return {"import_id": existing["id"], "variant_count": existing["variant_count"], "already_imported": True}
        cur = db.execute(
            "INSERT INTO imports(source_name,source_sha256,genome_build,imported_at,variant_count) VALUES(?,?,?,?,0)",
            (path.name, digest, genome_build, now),
        )
        import_id = cur.lastrowid
        batch: list[tuple] = []
        with _open(path) as handle:
            for line in handle:
                if line.startswith("##contig=<"):
                    content = line.strip()[10:-1]
                    values = dict(item.split("=", 1) for item in content.split(",") if "=" in item)
                    if "ID" in values and "length" in values:
                        contig_lengths[values["ID"].removeprefix("chr")] = int(values["length"])
                    continue
                if line.startswith("#CHROM"):
                    header = line.rstrip().split("\t")
                    sample_count = max(0, len(header) - 9)
                    if sample_count != 1:
                        raise ValueError(f"Expected one VCF sample, found {sample_count}")
                    sample_column = 9
                    continue
                if line.startswith("#"):
                    continue
                fields = line.rstrip().split("\t")
                if len(fields) < 8:
                    continue
                chrom, pos, rsid, ref, alts, qual, filter_status = fields[:7]
                genotype = None
                phased = 0
                if sample_column is not None and len(fields) > sample_column:
                    keys = fields[8].split(":")
                    values = fields[sample_column].split(":")
                    if "GT" in keys and keys.index("GT") < len(values):
                        genotype = values[keys.index("GT")]
                        phased = int("|" in genotype)
                for alt_index, alt in enumerate(alts.split(","), start=1):
                    if alt in {".", "<*>", "<NON_REF>"}:
                        continue
                    batch.append((chrom.removeprefix("chr"), int(pos), ref, alt, alt_index, None if rsid == "." else rsid, genotype, phased, filter_status, None if qual == "." else float(qual), import_id))
                    variant_count += 1
                    if len(batch) >= 10_000:
                        db.executemany("INSERT INTO variants(chrom,pos,ref,alt,alt_index,rsid,genotype,phased,filter_status,quality,source_import_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
                        batch.clear()
        if batch:
            db.executemany("INSERT INTO variants(chrom,pos,ref,alt,alt_index,rsid,genotype,phased,filter_status,quality,source_import_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
        if genome_build == "GRCh38" and contig_lengths.get("1") not in {None, 248956422}:
            raise ValueError("VCF contig lengths do not match GRCh38")
        if genome_build == "GRCh37" and contig_lengths.get("1") not in {None, 249250621}:
            raise ValueError("VCF contig lengths do not match GRCh37")
        db.execute("UPDATE imports SET variant_count=? WHERE id=?", (variant_count, import_id))
    return {"import_id": import_id, "variant_count": variant_count, "already_imported": False, "sha256": digest, "sample_count": sample_count}
