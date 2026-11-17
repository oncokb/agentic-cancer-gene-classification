"""Curator DMT ADC index annotations; never part of pair eligibility."""

import json
from pathlib import Path


_DATA = json.loads(Path(__file__).with_name("dmt_adc_index.json").read_text(encoding="utf-8"))


def index_for_gene(symbol: str) -> list[dict]:
    return _DATA["genes"].get(symbol, [])
