"""Default-flip contract tests.

Covers the two default changes that reduce latency/token cost for the common
case: annotation `mode` defaults to "core", and `skip_literature_for_oncokb`
defaults to True — while keeping an explicit opt-out (full mode, full
literature for OncoKB genes) working.
"""

from src.cli import parse_args
from src.models.schema import AnnotateRequest, GeneAnnotateRequest


def test_annotate_request_defaults_to_core_mode_and_oncokb_literature_skip():
    request = AnnotateRequest(fusions=["BRAF"])

    assert request.mode == "core"
    assert request.skip_literature_for_oncokb is True


def test_gene_annotate_request_defaults_to_core_mode_and_oncokb_literature_skip():
    request = GeneAnnotateRequest(gene="BRAF")

    assert request.mode == "core"
    assert request.skip_literature_for_oncokb is True


def test_annotate_request_accepts_full_mode_and_full_literature_opt_out():
    request = AnnotateRequest(
        fusions=["BRAF"],
        mode="full",
        skip_literature_for_oncokb=False,
    )

    assert request.mode == "full"
    assert request.skip_literature_for_oncokb is False


def test_gene_annotate_request_accepts_full_mode_and_full_literature_opt_out():
    request = GeneAnnotateRequest(
        gene="BRAF",
        mode="full",
        skip_literature_for_oncokb=False,
    )

    assert request.mode == "full"
    assert request.skip_literature_for_oncokb is False


def test_cli_defaults_to_core_mode_and_oncokb_literature_skip(monkeypatch):
    monkeypatch.setattr("sys.argv", ["prog", "--fusions", "BRAF"])

    args = parse_args()

    assert args.mode == "core"
    assert args.skip_literature_for_oncokb is True


def test_cli_explicit_full_mode_and_oncokb_literature_opt_out(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--fusions",
            "BRAF",
            "--mode",
            "full",
            "--no-skip-literature-for-oncokb",
        ],
    )

    args = parse_args()

    assert args.mode == "full"
    assert args.skip_literature_for_oncokb is False


def test_cli_still_accepts_explicit_skip_literature_flag(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "BRAF", "--skip-literature-for-oncokb"],
    )

    args = parse_args()

    assert args.skip_literature_for_oncokb is True
    assert args.mode == "core"