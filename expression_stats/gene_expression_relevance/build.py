"""Build static artifacts in gene batches using a disk-backed expression matrix."""

import json
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import METHOD_VERSION, PARAMS
from .fetch import digest, fetch_all
from .genes import gene_catalog, hgnc, hgnc_path
from .mapping import cancer_mapping
from .precheck import Precheck
from .provenance import (
    build_fingerprint,
    code_fingerprint,
    mapping_fingerprint,
    overrides_fingerprint,
)
from .stats import LINEAGE, compute, primary_indices


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")


def matrix(paths, mapping, catalog, target):
    """Join by Entrez, retain missing values, choose samples once across studies."""
    selection, labels, seen = {}, [], set()
    for item in mapping:
        study, code = item["study_id"], item["oncotree_code"]
        header = pd.read_csv(paths[study], sep="\t", nrows=0).columns.tolist()[2:]
        indices = primary_indices(header, code, seen)
        selection[study] = [header[i] for i in indices]
        labels.extend([code] * len(indices))
    ids = sorted(catalog)
    index = {entrez: i for i, entrez in enumerate(ids)}
    x = np.memmap(target, mode="w+", dtype="float64", shape=(len(ids), len(labels)))
    x[:] = np.nan
    offset = 0
    for item in mapping:
        study = item["study_id"]
        samples = selection[study]
        frame = pd.read_csv(paths[study], sep="\t", usecols=["Entrez_Gene_Id", *samples])
        entrez = pd.to_numeric(frame.pop("Entrez_Gene_Id"), errors="coerce")
        frame.index = entrez
        frame = frame.loc[frame.index.isin(index), samples]
        # Multiple source rows can map to one Entrez gene. Average linear RSEM,
        # omitting missing measurements, before the log transform.
        frame = frame.groupby(level=0, sort=False).mean().reindex(ids)
        x[:, offset : offset + len(samples)] = frame.to_numpy(dtype=np.float64)
        del frame
        offset += len(samples)
    x.flush()
    return x, np.asarray(labels), ids


def build(commit, out, cache, genes=None, skip_precheck=False, batch_size=256):
    started = time.monotonic()
    out, cache = Path(out), Path(cache)
    paths = fetch_all(commit, cache)
    mapping = cancer_mapping(paths)
    canonical, aliases = hgnc(cache)
    catalog = gene_catalog(paths.values(), canonical)
    if isinstance(genes, str):
        genes = genes.split(",")
    requested_genes = (
        sorted({g.strip().upper() for g in genes if g.strip()}) or None if genes else None
    )
    if requested_genes:
        known_symbols = set(catalog.values())
        requested = {g if g in known_symbols else aliases.get(g, g) for g in requested_genes}
        catalog = {entrez: symbol for entrez, symbol in catalog.items() if symbol in requested}
        missing = requested - set(catalog.values())
        if missing:
            raise ValueError(f"Genes absent or ambiguous: {sorted(missing)}")
    if not catalog:
        raise ValueError("No genes to build")
    now = datetime.now(UTC)
    hgnc_sha = digest(hgnc_path(cache))
    code_sha = code_fingerprint()
    mapping_sha = mapping_fingerprint(mapping)
    build_sha = build_fingerprint(
        PARAMS, requested_genes, skip_precheck, hgnc_sha, code_sha, mapping_sha
    )
    release = f"datahub-{commit[:12]}-m{METHOD_VERSION}-{build_sha[:12]}"
    manifest = dict(
        schema_version="1",
        release_id=release,
        method_version=METHOD_VERSION,
        params=PARAMS,
        source=dict(datahub_commit=commit, generated_at=now.isoformat()),
        cancer_types=mapping,
        build=dict(genes=requested_genes, skip_precheck=skip_precheck),
    )
    # Record the HGNC snapshot hash to make symbol resolution auditable.
    manifest["source"].update(
        hgnc_sha256=hgnc_sha,
        code_sha256=code_sha,
        overrides_sha256=overrides_fingerprint(),
        build_sha256=build_sha,
        cohort_mapping_sha256=mapping_sha,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    precheck = Precheck(cache)
    codes = [item["oncotree_code"] for item in mapping]
    passed = {}
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".build-") as temporary:
        staging = Path(temporary) / "site"
        data, labels, ids = matrix(paths, mapping, catalog, Path(temporary) / "expression.bin")
        for start in range(0, len(ids), batch_size):
            batch = ids[start : start + batch_size]
            arrays = compute(data[start : start + batch_size], labels, codes)
            for i, entrez in enumerate(batch):
                symbol = catalog[entrez]
                records = []
                for j, code in enumerate(codes):
                    record = {}
                    for key, array in arrays.items():
                        value = array[i, j]
                        record[key] = (
                            bool(value)
                            if key == "stats_pass"
                            else int(value)
                            if key in ("n", "n_rest")
                            else float(value)
                            if np.isfinite(value)
                            else None
                        )
                    count = None
                    if record["stats_pass"]:
                        passed.setdefault(symbol, []).append(code)
                    record.update(
                        oncotree_code=code,
                        pubmed_count=count,
                        literature_precheck_pass=(
                            count >= PARAMS["precheck_min_pmids"] if count is not None else None
                        ),
                        flags=(["small_n"] if record["n"] < PARAMS["min_n"] else [])
                        + (["tissue_of_origin_risk"] if symbol in LINEAGE.get(code, set()) else []),
                    )
                    records.append(record)
                write_json(
                    staging / "v1" / "genes" / f"{symbol}.json",
                    dict(
                        schema_version="1",
                        release_id=release,
                        gene=dict(hugo_symbol=symbol, entrez_gene_id=entrez),
                        cancer_types=records,
                    ),
                )
            print(f"Computed {min(start + batch_size, len(ids))}/{len(ids)} genes", flush=True)
        del data
        if not skip_precheck:
            queries = {item["oncotree_code"]: item["tumor_type_query"] for item in mapping}

            def check_gene(path):
                artifact = json.loads(path.read_text())
                for record in artifact["cancer_types"]:
                    if record["stats_pass"]:
                        count = precheck.count(
                            artifact["gene"]["hugo_symbol"],
                            record["oncotree_code"],
                            queries[record["oncotree_code"]],
                        )
                        record["pubmed_count"] = count
                        record["literature_precheck_pass"] = count >= PARAMS["precheck_min_pmids"]
                write_json(path, artifact)

            with ThreadPoolExecutor(max_workers=8) as pool:
                for _ in pool.map(check_gene, sorted((staging / "v1" / "genes").glob("*.json"))):
                    pass
        if not skip_precheck:
            timestamps = [
                precheck.cache[f"{symbol}|{code}"]["fetched_at"]
                for symbol, types in passed.items()
                for code in types
                if f"{symbol}|{code}" in precheck.cache
            ]
            expires = (
                min(timestamps, default=now.timestamp()) + PARAMS["precheck_cache_ttl_days"] * 86400
            )
            manifest["source"]["precheck_expires_at"] = datetime.fromtimestamp(
                expires, UTC
            ).isoformat()
        write_json(staging / "v1" / "manifest.json", manifest)
        published = set(catalog.values())
        write_json(
            staging / "v1" / "aliases.json",
            {
                alias: symbol
                for alias, symbol in aliases.items()
                if symbol in published and alias not in published
            },
        )
        if out.exists():
            shutil.rmtree(out)
        shutil.move(staging, out)
    print(
        json.dumps(
            dict(
                stats_passing=passed,
                genes=len(ids),
                samples=len(labels),
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
        ),
        flush=True,
    )
    return manifest
