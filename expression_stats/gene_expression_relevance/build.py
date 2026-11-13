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

from . import (
    ALTERATION_METHOD_VERSION,
    ALTERATION_PARAMS,
    CONTRACT_VERSION,
    METHOD_VERSION,
    PARAMS,
    drivers,
)
from . import alterations as alteration_stats
from .controls import evaluate as evaluate_controls
from .controls import require_passed
from .fetch import digest, fetch_all
from .genes import gene_catalog, hgnc, hgnc_path
from .mapping import cancer_mapping
from .precheck import Precheck, PrecheckBudgetExceeded
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


def select_samples(paths, mapping):
    """One primary sample per patient, chosen once across studies in mapping order.

    The single source of the patient population: expression and alteration statistics
    both use exactly these samples.
    """
    selection, seen = {}, set()
    for item in mapping:
        study, code = item["study_id"], item["oncotree_code"]
        header = pd.read_csv(paths[study], sep="\t", nrows=0).columns.tolist()[2:]
        selection[study] = [header[i] for i in primary_indices(header, code, seen)]
    return selection


def matrix(paths, mapping, catalog, target, selection=None):
    """Join by Entrez, retain missing values, choose samples once across studies."""
    selection = select_samples(paths, mapping) if selection is None else selection
    labels = []
    for item in mapping:
        labels.extend([item["oncotree_code"]] * len(selection[item["study_id"]]))
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


def load_alterations(commit, cache, mapping, selection, ids, catalog, aliases, delete_downloads):
    """Alteration statistics for the build's gene rows (detail only; see alterations.py)."""
    driver = drivers.load(ALTERATION_PARAMS["mutation_driver_source"])
    cna_driver = drivers.load_cna(ALTERATION_PARAMS["cna_driver_source"])
    inputs, provenance, inputs_sha = alteration_stats.load_inputs(
        commit, cache, [item["study_id"] for item in mapping], delete_downloads=delete_downloads
    )
    result = alteration_stats.compute(
        inputs, mapping, selection, ids, catalog, aliases, driver, cna_driver=cna_driver
    )
    pins = dict(**driver.pin(), **cna_driver.pin())
    return result, pins, provenance, inputs_sha


def build(
    commit,
    out,
    cache,
    genes=None,
    skip_precheck=False,
    batch_size=256,
    *,
    skip_alterations=False,
    delete_downloads=False,
):
    started = time.monotonic()
    out, cache = Path(out), Path(cache)
    paths = fetch_all(commit, cache)
    mapping = cancer_mapping(paths)
    canonical, aliases = hgnc(cache)
    catalog = gene_catalog(paths.values(), canonical)
    full_catalog = dict(catalog)
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
    ids = sorted(catalog)
    selection = select_samples(paths, mapping)
    alteration = None
    alteration_identity = dict(skipped=True)
    if not skip_alterations:
        alteration, driver_pins, alteration_inputs, inputs_sha = load_alterations(
            commit, cache, mapping, selection, ids, full_catalog, aliases, delete_downloads
        )
        alteration_identity = dict(
            method_version=ALTERATION_METHOD_VERSION,
            params=ALTERATION_PARAMS,
            inputs_sha256=inputs_sha,
            **driver_pins,
        )
    now = datetime.now(UTC)
    hgnc_sha = digest(hgnc_path(cache))
    code_sha = code_fingerprint()
    mapping_sha = mapping_fingerprint(mapping)
    build_sha = build_fingerprint(
        PARAMS,
        requested_genes,
        skip_precheck,
        hgnc_sha,
        code_sha,
        mapping_sha,
        alterations=alteration_identity,
    )
    release = f"datahub-{commit[:12]}-m{METHOD_VERSION}-{build_sha[:12]}"
    manifest = dict(
        schema_version="1",
        contract_version=CONTRACT_VERSION,
        release_id=release,
        method_version=METHOD_VERSION,
        params=PARAMS,
        alteration_method_version=ALTERATION_METHOD_VERSION,
        alteration_params=ALTERATION_PARAMS,
        source=dict(datahub_commit=commit, generated_at=now.isoformat()),
        cancer_types=mapping,
        build=dict(
            genes=requested_genes, skip_precheck=skip_precheck, skip_alterations=skip_alterations
        ),
    )
    if alteration is not None:
        manifest["source"].update(
            alteration_inputs_sha256=alteration_identity["inputs_sha256"], **driver_pins
        )
        manifest["alterations"] = dict(alteration.summary, inputs=alteration_inputs)
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
    # Throughput/budgets are operational metadata, never hashed method params.
    manifest["source"].update(
        precheck_requests_per_second=precheck.requests_per_second,
        precheck_time_budget_seconds=precheck.time_budget_seconds,
        build_time_budget_seconds=precheck.build_time_budget_seconds,
    )
    codes = [item["oncotree_code"] for item in mapping]
    passed = {}
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".build-") as temporary:
        staging = Path(temporary) / "site"
        data, labels, ids = matrix(
            paths, mapping, catalog, Path(temporary) / "expression.bin", selection
        )
        for start in range(0, len(ids), batch_size):
            batch = ids[start : start + batch_size]
            arrays = compute(data[start : start + batch_size], labels, codes)
            for i, entrez in enumerate(batch):
                symbol = catalog[entrez]
                records = []
                for j, code in enumerate(codes):
                    record = {}
                    for key, array in arrays.items():
                        if key == "broadly_expressed":
                            continue
                        value = array[i, j]
                        if key in ("pass_mode", "confidence_tier"):
                            record[key] = value
                            continue
                        record[key] = (
                            bool(value)
                            if key in ("stats_pass", "lineage_blocked_by_broad")
                            else int(value)
                            if key in ("n", "n_rest", "subgroup_high_count") and np.isfinite(value)
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
                        # Detail only: added after every expression field is final.
                        alterations=(
                            alteration.record(start + i, j) if alteration is not None else None
                        ),
                    )
                    records.append(record)
                write_json(
                    staging / "v1" / "genes" / f"{symbol}.json",
                    dict(
                        schema_version="1",
                        release_id=release,
                        gene=dict(hugo_symbol=symbol, entrez_gene_id=entrez),
                        broadly_expressed=bool(arrays["broadly_expressed"][i]),
                        cancer_types=records,
                    ),
                )
            print(f"Computed {min(start + batch_size, len(ids))}/{len(ids)} genes", flush=True)
        del data
        report = evaluate_controls(
            staging / "v1" / "genes",
            subset=requested_genes is not None,
            codes=codes,
            alterations=alteration is not None,
        )
        manifest["controls"] = {key: value for key, value in report.items() if key != "controls"}
        if "alterations" in report:
            manifest["controls"]["alterations"] = {
                key: value for key, value in report["alterations"].items() if key != "controls"
            }
        write_json(staging / "v1" / "controls-report.json", report)
        if requested_genes is None and report["status"] != "passed":
            failed = out.with_name(out.name + "-failed")
            write_json(failed / "controls-report.json", report)
            write_json(failed / "manifest.json", manifest)
            require_passed(report)
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

            pool = None
            try:
                precheck.start_budget(build_deadline=started + precheck.build_time_budget_seconds)
                pool = ThreadPoolExecutor(max_workers=8)
                for _ in pool.map(check_gene, sorted((staging / "v1" / "genes").glob("*.json"))):
                    pass
                precheck.remaining()
            except PrecheckBudgetExceeded as exc:
                precheck.stop()
                manifest["source"]["precheck_incomplete"] = True
                incomplete = out.with_name(out.name + "-incomplete")
                write_json(incomplete / "manifest.json", manifest)
                write_json(incomplete / "controls-report.json", report)
                write_json(
                    incomplete / "precheck-report.json",
                    dict(
                        status="incomplete",
                        reason=str(exc),
                        cache=str(precheck.path),
                    ),
                )
                raise
            finally:
                if pool is not None:
                    pool.shutdown(wait=True, cancel_futures=True)
                # Counts commit individually; flush once more before Actions caches them.
                precheck.db.commit()
        if not skip_precheck:
            expiries = [
                precheck.expires_at(
                    f"{symbol}|{code}", precheck.cache[f"{symbol}|{code}"]["fetched_at"]
                )
                for symbol, types in passed.items()
                for code in types
                if f"{symbol}|{code}" in precheck.cache
            ]
            expires = min(expiries, default=now.timestamp() + precheck.ttl_seconds)
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
