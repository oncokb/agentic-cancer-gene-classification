"""Synthetic datahub alteration inputs (no network)."""

import numpy as np

from gene_expression_relevance import alterations


def empty_mutation():
    return dict(
        sample=np.zeros(0, dtype=np.int32),
        sample_table=np.zeros(0, dtype=str),
        symbol=np.zeros(0, dtype=np.int32),
        symbol_table=np.zeros(0, dtype=str),
        entrez=np.zeros(0, dtype=np.int64),
        cls=np.zeros(0, dtype=np.int8),
        transcript=np.zeros(0, dtype=np.int32),
        transcript_table=np.zeros(0, dtype=str),
        position=np.zeros(0, dtype=np.int32),
        hgvsp=np.zeros(0, dtype=np.int32),
        hgvsp_table=np.zeros(0, dtype=str),
    )


def empty_inputs(commit, cache, studies, **kwargs):
    """Every study present with no profiled samples: all pairs become not_testable."""
    inputs = {
        study: dict(
            mutation=dict(data=empty_mutation(), cases=set()),
            cna=dict(
                data=dict(
                    samples=np.zeros(0, dtype=str),
                    symbol=np.zeros(0, dtype=str),
                    entrez=np.zeros(0, dtype=np.int64),
                    calls=np.zeros((0, 0), dtype=np.int8),
                ),
                cases=set(),
            ),
            sv=dict(
                data=dict(
                    sample=np.zeros(0, dtype=str),
                    site1=np.zeros(0, dtype=str),
                    site2=np.zeros(0, dtype=str),
                ),
                cases=set(),
            ),
        )
        for study in studies
    }
    provenance = {study: {} for study in studies}
    return inputs, provenance, "0" * 64


def maf(rows):
    """rows: (symbol, entrez, classification, sample, transcript, hgvsp)."""
    header = "\t".join(alterations.MUTATION_COLUMNS + ["Extra"])
    lines = ["#version 2.4", header]
    lines += ["\t".join([*map(str, row), "x"]) for row in rows]
    return "\n".join(lines) + "\n"
