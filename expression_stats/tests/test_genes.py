import hashlib

import pytest

from gene_expression_relevance import genes
from gene_expression_relevance.genes import gene_catalog, hgnc, hgnc_path


def test_hgnc_alias_ambiguity_and_previous_symbols(tmp_path, monkeypatch):
    content = (
        "symbol\tstatus\tentrez_id\talias_symbol\tprev_symbol\n"
        "ERBB2\tApproved\t2064\tHER2|SHARED|TP53\tNEU\n"
        "TP53\tApproved\t7157\tP53|SHARED\t\n"
    )
    pin = dict(
        url="https://hgnc.example/snapshot", sha256=hashlib.sha256(content.encode()).hexdigest()
    )
    monkeypatch.setattr(genes, "hgnc_pin", lambda: pin)
    path = hgnc_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content)
    symbols, aliases = hgnc(tmp_path)
    assert symbols[2064] == "ERBB2"
    assert aliases == {"HER2": "ERBB2", "NEU": "ERBB2", "P53": "TP53"}


def test_entrez_join_and_symbol_collision(tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("Hugo_Symbol\tEntrez_Gene_Id\nOLD\t2064\nDUP\t1\nDUP\t2\nX\t3\n")
    second.write_text("Hugo_Symbol\tEntrez_Gene_Id\nERBB2\t2064\nY\t3\n")
    assert gene_catalog([first, second], {2064: "ERBB2"}) == {2064: "ERBB2"}


def test_hgnc_download_verifies_pin_and_rejects_corrupt_cache(tmp_path, monkeypatch):
    pin = dict(url="https://hgnc.example/snapshot", sha256="a" * 64)
    monkeypatch.setattr(genes, "hgnc_pin", lambda: pin)

    def download(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("corrupt")

    monkeypatch.setattr(genes.http, "download", download)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        hgnc(tmp_path)
    assert not hgnc_path(tmp_path).exists()
