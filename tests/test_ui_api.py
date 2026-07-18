"""Tests for the browser UI support endpoints."""

from __future__ import annotations

import json
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from src.pipeline import db_lookups, google_sheets_export, literature
from src.main import app
from src.models.schema import (
    AnnotationResult,
    GeneAnnotation,
    GoogleSheetExportResponse,
    LocalBackendStatus,
    OncoKBConfigStatus,
)


client = TestClient(app)


def test_ui_root_serves_static_app():
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Gene Fusion Annotation" in response.text


def test_static_ui_softens_class_ii_tier_for_review_badges():
    app_js = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert "Functional cancer evidence" in app_js
    assert "Equivalent raw tier: Class II - Likely Driver." in app_js
    assert "Moderate priority" in app_js
    assert ".review-badge.medium" in styles


def test_local_backend_status_reports_installed_codex(monkeypatch):
    def fake_find_command(command):
        return "/usr/local/bin/codex" if command == "codex" else None

    def fake_run(command, **kwargs):
        assert command == ["/usr/local/bin/codex", "--version"]
        return CompletedProcess(command, 0, stdout="codex 1.2.3\n", stderr="")

    monkeypatch.setattr("src.main._find_command", fake_find_command)
    monkeypatch.setattr("src.main.run", fake_run)
    monkeypatch.setattr("src.main.settings.anthropic_api_key", "")
    monkeypatch.setattr(
        "src.main.oncokb_config_status",
        lambda: OncoKBConfigStatus(configured=True, source="local_upload"),
    )

    response = client.get("/v1/local-backends/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["setup_required"] is False
    assert payload["minimum_setup_complete"] is True
    assert payload["local_backend_configured"] is True
    assert payload["anthropic_sdk_configured"] is False
    assert payload["oncokb_configured"] is True
    assert payload["operating_system"]
    codex = next(item for item in payload["backends"] if item["backend"] == "codex")
    assert codex["installed"] is True
    assert codex["version"] == "codex 1.2.3"
    claude = next(item for item in payload["backends"] if item["backend"] == "claude-code")
    assert claude["installed"] is False
    copilot = next(item for item in payload["backends"] if item["backend"] == "copilot")
    assert copilot["installed"] is False
    antigravity = next(item for item in payload["backends"] if item["backend"] == "antigravity")
    assert antigravity["installed"] is False


def test_local_backend_status_requires_oncokb_token(monkeypatch):
    def fake_find_command(command):
        return "/usr/local/bin/codex" if command == "codex" else None

    def fake_run(command, **kwargs):
        return CompletedProcess(command, 0, stdout="codex 1.2.3\n", stderr="")

    monkeypatch.setattr("src.main._find_command", fake_find_command)
    monkeypatch.setattr("src.main.run", fake_run)
    monkeypatch.setattr("src.main.settings.anthropic_api_key", "")
    monkeypatch.setattr(
        "src.main.oncokb_config_status",
        lambda: OncoKBConfigStatus(configured=False),
    )

    response = client.get("/v1/local-backends/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["setup_required"] is True
    assert payload["minimum_setup_complete"] is False
    assert payload["local_backend_configured"] is True
    assert payload["oncokb_configured"] is False
    assert payload["setup_messages"] == [
        "Paste an OncoKB API token from OncoKB account settings."
    ]


def test_local_backend_installers_return_os_specific_commands(monkeypatch):
    monkeypatch.setattr("src.main.platform.system", lambda: "Darwin")

    response = client.get("/v1/local-backends/installers")

    assert response.status_code == 200
    payload = response.json()
    codex = next(item for item in payload if item["backend"] == "codex")
    assert codex["supported"] is True
    assert "chatgpt.com/codex/install.sh" in codex["display_command"]
    claude = next(item for item in payload if item["backend"] == "claude-code")
    assert claude["supported"] is True
    assert "claude.ai/install.sh" in claude["display_command"]
    copilot = next(item for item in payload if item["backend"] == "copilot")
    assert copilot["supported"] is True
    assert "gh.io/copilot-install" in copilot["display_command"]
    antigravity = next(item for item in payload if item["backend"] == "antigravity")
    assert antigravity["supported"] is False
    assert antigravity["setup_url"] == "https://antigravity.google/"


def test_install_local_backend_runs_allowlisted_installer(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        return CompletedProcess(command, 0, stdout="installed\n", stderr="")

    def fake_check_backend(backend):
        return LocalBackendStatus(
            backend=backend,
            command="codex",
            installed=True,
            version="codex 1.2.3",
            path="/Users/person/.local/bin/codex",
        )

    monkeypatch.setattr("src.main.platform.system", lambda: "Darwin")
    monkeypatch.setattr("src.main.run", fake_run)
    monkeypatch.setattr("src.main._check_backend", fake_check_backend)

    response = client.post("/v1/local-backends/install", json={"backend": "codex"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["installed"] is True
    assert payload["return_code"] == 0
    assert payload["stdout"] == "installed\n"
    assert "chatgpt.com/codex/install.sh" in payload["command"]
    assert seen["command"] == [
        "sh",
        "-lc",
        "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh",
    ]
    assert seen["timeout"] == 600


def test_install_copilot_runs_allowlisted_installer(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        return CompletedProcess(command, 0, stdout="installed\n", stderr="")

    def fake_check_backend(backend):
        return LocalBackendStatus(
            backend=backend,
            command="copilot",
            installed=True,
            version="copilot 1.2.3",
            path="/Users/person/.local/bin/copilot",
        )

    monkeypatch.setattr("src.main.platform.system", lambda: "Darwin")
    monkeypatch.setattr("src.main.run", fake_run)
    monkeypatch.setattr("src.main._check_backend", fake_check_backend)

    response = client.post("/v1/local-backends/install", json={"backend": "copilot"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["installed"] is True
    assert payload["return_code"] == 0
    assert "gh.io/copilot-install" in payload["command"]
    assert seen["command"] == [
        "sh",
        "-lc",
        "curl -fsSL https://gh.io/copilot-install | bash",
    ]
    assert seen["timeout"] == 600


def test_login_copilot_runs_cli_login(monkeypatch):
    seen = {}

    def fake_find_command(command):
        return "/usr/local/bin/copilot" if command == "copilot" else None

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs["timeout"]
        return CompletedProcess(command, 0, stdout="logged in\n", stderr="")

    monkeypatch.setattr("src.main._find_command", fake_find_command)
    monkeypatch.setattr("src.main.run", fake_run)

    response = client.post("/v1/local-backends/login", json={"backend": "copilot"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["return_code"] == 0
    assert payload["command"] == "copilot login"
    assert payload["stdout"] == "logged in\n"
    assert seen["command"] == ["/usr/local/bin/copilot", "login"]
    assert seen["timeout"] == 600


def test_install_local_backend_rejects_non_local_clients():
    remote_client = TestClient(app, client=("192.0.2.1", 5000))

    response = remote_client.post(
        "/v1/local-backends/install",
        json={"backend": "codex"},
    )

    assert response.status_code == 403


def test_prepare_local_backend_paths_persists_detected_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.main.prepare_local_agent_paths",
        lambda: (
            {"codex": "/Users/person/.local/bin/codex", "claude": "/Users/person/.local/bin/claude"},
            tmp_path / "local-agent-paths.json",
        ),
    )

    response = client.post("/v1/local-backends/prepare")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured_count"] == 2
    assert payload["configured_paths"] == {
        "codex": "/Users/person/.local/bin/codex",
        "claude-code": "/Users/person/.local/bin/claude",
    }
    assert payload["config_path"] == str(tmp_path / "local-agent-paths.json")


def test_prepare_local_backend_paths_rejects_non_local_clients():
    remote_client = TestClient(app, client=("192.0.2.1", 5000))

    response = remote_client.post("/v1/local-backends/prepare")

    assert response.status_code == 403


def test_export_annotation_results_csv_endpoint_returns_download():
    result = AnnotationResult(
        run_id="run-1",
        timestamp="2026-07-13T00:00:00Z",
        fusions_processed=1,
        genes_annotated=1,
        annotations=[
            GeneAnnotation(
                gene="BRAF",
                fusions=["TP53::BRAF"],
                cancer_associated=True,
                citations=["12345"],
                date_annotated="7/13/26",
            )
        ],
    )

    response = client.post(
        "/v1/export/annotation-results.csv",
        json=result.model_dump(),
    )

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert response.headers["content-disposition"] == (
        'attachment; filename="annotation_results.csv"'
    )
    assert "BRAF" in response.text
    assert "https://pubmed.ncbi.nlm.nih.gov/12345/" in response.text


def test_annotate_stream_returns_progress_events(monkeypatch):
    async def fake_iter_pipeline_events(fusions, local_backend=None):
        assert fusions == ["TP53::BRAF"]
        assert local_backend == "codex"
        annotation = GeneAnnotation(
            gene="BRAF",
            fusions=["TP53::BRAF"],
            cancer_associated=True,
            citations=["12345"],
            date_annotated="7/15/26",
        )
        result = AnnotationResult(
            run_id="run-1",
            timestamp="2026-07-15T00:00:00Z",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[annotation],
            run_error="insufficient tokens for document retrieval",
        )
        yield {
            "type": "start",
            "run_id": "run-1",
            "timestamp": "2026-07-15T00:00:00Z",
            "fusions_processed": 1,
            "genes_total": 2,
        }
        yield {
            "type": "annotation",
            "annotation": annotation,
            "completed_count": 1,
            "genes_total": 2,
        }
        yield {
            "type": "error",
            "message": result.run_error,
            "gene": "TP53",
            "result": result,
        }

    monkeypatch.setattr("src.main.iter_pipeline_events", fake_iter_pipeline_events)

    response = client.post(
        "/v1/annotate/stream",
        json={"fusions": ["TP53::BRAF"], "local_backend": "codex"},
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["type"] for event in events] == ["start", "annotation", "error"]
    assert events[1]["annotation"]["gene"] == "BRAF"
    assert events[2]["result"]["run_error"] == "insufficient tokens for document retrieval"


def test_export_google_sheet_endpoint_uses_reviewed_result(monkeypatch):
    seen = {}

    def fake_export(result, *, spreadsheet_id, sheet_name):
        seen["result"] = result
        seen["spreadsheet_id"] = spreadsheet_id
        seen["sheet_name"] = sheet_name
        return GoogleSheetExportResponse(
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            updated_range="'Review'!A1:R2",
            updated_rows=2,
            updated_columns=18,
            spreadsheet_url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        )

    monkeypatch.setattr("src.main.export_annotation_results_to_google_sheet", fake_export)
    result = AnnotationResult(
        run_id="run-1",
        timestamp="2026-07-13T00:00:00Z",
        fusions_processed=1,
        genes_annotated=1,
        annotations=[
            GeneAnnotation(
                gene="BRAF",
                fusions=["TP53::BRAF"],
                cancer_associated=True,
                citations=["12345"],
                date_annotated="7/13/26",
            )
        ],
    )

    response = client.post(
        "/v1/export/google-sheet",
        json={
            "spreadsheet_id": "sheet-123",
            "sheet_name": "Review",
            "result": result.model_dump(),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["updated_rows"] == 2
    assert payload["spreadsheet_url"] == (
        "https://docs.google.com/spreadsheets/d/sheet-123/edit"
    )
    assert seen["spreadsheet_id"] == "sheet-123"
    assert seen["sheet_name"] == "Review"
    assert seen["result"].annotations[0].gene == "BRAF"


def test_google_sheets_configure_service_account_from_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(google_sheets_export.settings, "google_service_account_json", "")
    service_account = {
        "type": "service_account",
        "client_email": "sheet-writer@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    }

    response = client.post(
        "/v1/google-sheets/service-account",
        json={"service_account_json": json.dumps(service_account)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["service_account_email"] == service_account["client_email"]
    status = client.get("/v1/google-sheets/config").json()
    assert status["configured"] is True
    assert status["source"] == "local_upload"
    assert status["service_account_email"] == service_account["client_email"]


def test_google_sheets_configure_rejects_non_local_clients():
    remote_client = TestClient(app, client=("192.0.2.1", 5000))

    response = remote_client.post(
        "/v1/google-sheets/service-account",
        json={"service_account_json": "{}"},
    )

    assert response.status_code == 403


def test_oncokb_configure_token_from_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(db_lookups.settings, "oncokb_api_token", "")

    response = client.post(
        "/v1/oncokb/token",
        json={"api_token": "oncokb-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["source"] == "local_upload"
    status = client.get("/v1/oncokb/config").json()
    assert status["configured"] is True
    assert status["source"] == "local_upload"


def test_oncokb_configure_rejects_non_local_clients():
    remote_client = TestClient(app, client=("192.0.2.1", 5000))

    response = remote_client.post(
        "/v1/oncokb/token",
        json={"api_token": "oncokb-token"},
    )

    assert response.status_code == 403


def test_ncbi_configure_api_key_from_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(literature.settings, "ncbi_api_key", "")

    response = client.post(
        "/v1/ncbi/api-key",
        json={"api_key": "ncbi-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True
    assert payload["source"] == "local_upload"
    status = client.get("/v1/ncbi/config").json()
    assert status["configured"] is True
    assert status["source"] == "local_upload"


def test_ncbi_configure_rejects_non_local_clients():
    remote_client = TestClient(app, client=("192.0.2.1", 5000))

    response = remote_client.post(
        "/v1/ncbi/api-key",
        json={"api_key": "ncbi-key"},
    )

    assert response.status_code == 403


def test_benchmark_endpoint_uses_benchmark_helpers(monkeypatch):
    async def fake_run_pipeline(fusions, local_backend=None):
        assert fusions == ["TP53::BRAF"]
        assert local_backend == "codex"
        return {
            "run_id": "run-1",
            "timestamp": "2026-07-13T00:00:00Z",
            "fusions_processed": 1,
            "genes_annotated": 1,
            "annotations": [
                {
                    "gene": "BRAF",
                    "fusions": ["TP53::BRAF"],
                    "in_oncokb": True,
                    "cancer_associated": True,
                    "cancer_associated_gene_tier": "Class I - Driver",
                    "og_or_tsg": "OG",
                    "citations": ["12345"],
                    "date_annotated": "7/13/26",
                    "retrieval_count": 1,
                    "insufficient_evidence": False,
                    "confidence": 0.9,
                    "error": None,
                }
            ],
        }

    holdout = [
        {
            "gene": "BRAF",
            "fusions": ["TP53::BRAF"],
            "cancer_associated": True,
            "cancer_associated_gene_tier": "Class I - Driver",
            "og_or_tsg": "OG",
            "citations": ["12345"],
        }
    ]

    monkeypatch.setattr("benchmarks.run_benchmark.load_holdout", lambda path: holdout)
    monkeypatch.setattr(
        "benchmarks.run_benchmark._get_fusions_from_holdout",
        lambda records: ["TP53::BRAF"],
    )
    monkeypatch.setattr("benchmarks.run_benchmark._run_pipeline", fake_run_pipeline)

    response = client.post(
        "/v1/benchmark",
        json={"local_backend": "codex", "no_judge": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["n_genes"] == 1
    assert payload["judge"] is None
    assert payload["pipeline_result"]["annotations"][0]["gene"] == "BRAF"
