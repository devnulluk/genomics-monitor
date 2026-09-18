import os

from fastapi.testclient import TestClient

from genomics_monitor.app import app


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
