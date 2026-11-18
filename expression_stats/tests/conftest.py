import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


@pytest.fixture(autouse=True)
def offline_alteration_inputs(monkeypatch):
    """Builds never download alteration inputs in tests; specific tests override this."""
    from alteration_fixtures import empty_inputs

    from gene_expression_relevance import alterations

    monkeypatch.setattr(alterations, "load_inputs", empty_inputs)


@pytest.fixture(autouse=True)
def offline_oncokb_client(monkeypatch, tmp_path_factory):
    """Default offline OncoKB stub; OncoKB unit tests override this directly."""
    from gene_expression_relevance import oncokb

    class FakeOncoKB:
        def __init__(self):
            self.index = oncokb.build_cancer_gene_index([], "v0-test", "2026-11-01")
            self.api_calls = 0
            self.unique_variants_queried = 0
            self.cache_hits = 0

        def annotate(self, keys):
            return {k: "unknown" for k in keys}

        def provenance(self):
            return {
                "oncokb_data_version": self.index.data_version,
                "oncokb_data_date": self.index.data_date,
                "oncokb_classification_version": oncokb.CLASSIFICATION_VERSION,
                "oncokb_cancer_gene_list_sha256": self.index.sha256,
            }

        def close(self):
            pass

    monkeypatch.setattr(oncokb, "load_client", lambda *a, **kw: FakeOncoKB())


@pytest.fixture(autouse=True)
def offline_purity_inputs(monkeypatch):
    import numpy as np

    from gene_expression_relevance import build

    monkeypatch.setattr(
        build, "load_selected",
        lambda cache, mapping, selection: np.full(
            sum(len(selection[item["study_id"]]) for item in mapping), np.nan
        ),
    )
