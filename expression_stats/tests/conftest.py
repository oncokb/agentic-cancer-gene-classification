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
def offline_purity_inputs(monkeypatch):
    import numpy as np

    from gene_expression_relevance import build

    monkeypatch.setattr(
        build, "load_selected",
        lambda cache, mapping, selection: np.full(
            sum(len(selection[item["study_id"]]) for item in mapping), np.nan
        ),
    )
