import os
import gzip
import json
import zipfile

from fastapi.testclient import TestClient

from genomics_monitor.app import app
from genomics_monitor.clinvar import sync_clinvar
from genomics_monitor.gwas import sync_gwas
from genomics_monitor.importer import import_vcf


def test_import_and_match(tmp_path):
    os.environ["GENOMICS_DB"] = str(tmp_path / "test.sqlite")
    os.environ["GENOMICS_API_TOKEN"] = "test-token"
    vcf = tmp_path / "sample.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n1\t101\trs123\tA\tG\t99\tPASS\t.\tGT\t0/1\n")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    result = client.post("/imports/vcf", json={"path": str(vcf), "genome_build": "GRCh38"}, headers=headers)
    assert result.status_code == 200
    assert result.json()["variant_count"] == 1
    evidence = {"source":"example","source_record_id":"paper-1","source_version":"1","title":"Eye pigmentation association","summary":"Example only","category":"trait","evidence_level":"replicated","rsid":"rs123","effect_allele":"G","trait_label":"eye pigmentation","url":"https://example.test/paper"}
    assert client.post("/evidence", json={"records":[evidence]}, headers=headers).json()["inserted"] == 1
    items = client.get("/findings", headers=headers).json()["items"]
    assert len(items) == 1
    assert items[0]["genotype"] == "0/1"
    assert items[0]["category"] == "trait"
    assert items[0]["effect_allele_status"] == "present"
    assert items[0]["match_basis"] == "rsid"


def test_token_required(tmp_path):
    os.environ["GENOMICS_DB"] = str(tmp_path / "test.sqlite")
    os.environ["GENOMICS_API_TOKEN"] = "secret"
    response = TestClient(app).get("/status")
    assert response.status_code == 401
    assert "secret" not in response.text


def test_multiallelic_effect_allele(tmp_path):
    os.environ["GENOMICS_DB"] = str(tmp_path / "multi.sqlite")
    os.environ["GENOMICS_API_TOKEN"] = "test-token"
    vcf = tmp_path / "multi.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\nchr1\t101\trs456\tA\tG,T\t99\tPASS\t.\tGT\t0/2\n")
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/imports/vcf", json={"path": str(vcf), "genome_build": "GRCh38"}, headers=headers).status_code == 200
    evidence = {"source":"example","source_record_id":"paper-2","title":"Example","summary":"Example","category":"trait","evidence_level":"single_study","rsid":"rs456","effect_allele":"T","url":"https://example.test/two"}
    client.post("/evidence", json={"records":[evidence]}, headers=headers)
    statuses = {item["called_alt"]: item["effect_allele_status"] for item in client.get("/findings", headers=headers).json()["items"]}
    assert statuses == {"G": "not_present", "T": "present"}


def test_streamed_vcf_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("GENOMICS_DB", str(tmp_path / "upload.sqlite"))
    monkeypatch.setenv("GENOMICS_INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("GENOMICS_API_TOKEN", "test-token")
    content = b"##fileformat=VCFv4.2\n##contig=<ID=chr1,length=248956422>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\nchr1\t101\trs789\tA\tG\t99\tPASS\t.\tGT\t0/1\n"
    response = TestClient(app).post(
        "/imports/vcf-file",
        headers={"Authorization": "Bearer test-token"},
        data={"genome_build": "GRCh38"},
        files={"file": ("genome.vcf", content, "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["variant_count"] == 1
    assert (tmp_path / "input" / "genome.vcf").exists()


def test_clinvar_sync_keeps_only_called_alt(tmp_path, monkeypatch):
    monkeypatch.setenv("GENOMICS_DB", str(tmp_path / "clinvar.sqlite"))
    monkeypatch.setenv("GENOMICS_API_TOKEN", "test-token")
    sample = tmp_path / "sample.vcf"
    sample.write_text(
        "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
        "1\t101\trs1\tA\tG\t99\tPASS\t.\tGT\t0/1\n"
        "1\t202\trs2\tC\tT\t99\tRefCall\t.\tGT\t0/0\n"
    )
    headers = {"Authorization": "Bearer test-token"}
    client = TestClient(app)
    assert client.post("/imports/vcf", json={"path": str(sample), "genome_build": "GRCh38"}, headers=headers).status_code == 200
    clinvar = tmp_path / "clinvar.vcf.gz"
    with gzip.open(clinvar, "wt") as handle:
        handle.write("##fileformat=VCFv4.2\n##fileDate=20260915\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
        handle.write("1\t101\trs1\tA\tG\t.\t.\tALLELEID=10;CLNSIG=Pathogenic;CLNREVSTAT=reviewed_by_expert_panel;CLNDN=Example_condition\n")
        handle.write("1\t202\trs2\tC\tT\t.\t.\tALLELEID=20;CLNSIG=Pathogenic;CLNREVSTAT=criteria_provided,_single_submitter;CLNDN=Reference_call\n")
    result = sync_clinvar(clinvar.as_uri())
    assert result["records_scanned"] == 2
    assert result["matched_records"] == 1
    assert result["inserted_records"] == 1
    finding = client.get("/findings", headers=headers).json()["items"][0]
    assert finding["source"] == "ClinVar"
    assert finding["evidence_level"] == "expert_panel"
    assert finding["effect_allele_status"] == "present"


def test_gwas_sync_keeps_only_present_effect_allele(tmp_path, monkeypatch):
    vcf = tmp_path / "genome.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n1\t100\trs123\tA\tG\t50\tPASS\t.\tGT\t0/1\n", encoding="utf-8")
    assert import_vcf(str(vcf), "GRCh38")["variant_count"] == 1
    catalog = tmp_path / "gwas.tsv"
    catalog.write_text(
        "PUBMEDID\tSTUDY ACCESSION\tSTRONGEST SNP-RISK ALLELE\tMAPPED_TRAIT\tMAPPED_TRAIT_URI\tP-VALUE\tLINK\n"
        "123\tGCST1\trs123-G\texample trait\tEFO_1\t1e-9\thttps://example.test/paper\n"
        "123\tGCST1\trs123-T\texample trait\tEFO_1\t1e-9\thttps://example.test/paper\n",
        encoding="utf-8",
    )
    result = sync_gwas(catalog.as_uri())
    assert result["matched_records"] == 1
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    report = client.get("/reports/initial", headers=headers).json()
    assert report["by_source"]["GWAS Catalog"] == 1


def test_gwas_sync_reads_official_zip_shape(tmp_path):
    vcf = tmp_path / "genome-zip.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n1\t101\trs456\tC\tT\t50\tPASS\t.\tGT\t0/1\n", encoding="utf-8")
    assert import_vcf(str(vcf), "GRCh38")["variant_count"] == 1
    archive_path = tmp_path / "gwas.zip"
    payload = (
        "PUBMEDID\tSTUDY ACCESSION\tSTRONGEST SNP-RISK ALLELE\tMAPPED_TRAIT\tMAPPED_TRAIT_URI\tP-VALUE\tLINK\n"
        "456\tGCST2\trs456-T\tzip trait\tEFO_2\t2e-9\thttps://example.test/zip-paper\n"
    )
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("gwas_catalog_v1.0-associations_e110_r2026-09-15.tsv", payload)
    result = sync_gwas(archive_path.as_uri())
    assert result["records_scanned"] == 1
    assert result["matched_records"] == 1
