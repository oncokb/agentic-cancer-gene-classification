from fastapi.testclient import TestClient

from src import main


class FakeRunStore:
    def __init__(self):
        self.saved_feedback = []

    async def save_feedback(self, **kwargs):
        self.saved_feedback.append(kwargs)


def test_feedback_submit_stores_feedback_and_returns_llm_issue_draft(monkeypatch):
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_complete_with_tool(**kwargs):
        assert kwargs["model"] == main.settings.feedback_model
        assert "Export dropdown is confusing" in kwargs["user"]
        return {
            "title": "Clarify export dropdown behavior",
            "problem_summary": "The export dropdown behavior is confusing.",
            "suggested_solution": "Keep the format selector enabled and disable only export.",
            "acceptance_criteria": [
                "Format dropdown remains clickable before results exist.",
                "Export remains disabled until data exists.",
            ],
        }

    monkeypatch.setattr(main, "complete_with_tool", fake_complete_with_tool)
    client = TestClient(main.app)

    response = client.post(
        "/v1/feedback",
        json={
            "category": "bug",
            "message": "Export dropdown is confusing\nPlease keep it clickable.",
            "run_id": "run-123",
            "gene": "TP53",
            "page_url": "https://acgc.oncokb.org/static/index.html?run=run-123",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["feedback_id"]
    assert payload["issue_title"] == "Clarify export dropdown behavior"
    assert "Keep the format selector enabled" in payload["issue_body"]
    assert "Export dropdown is confusing\nPlease keep it clickable." in payload["issue_body"]
    assert "run-123" in payload["issue_body"]
    assert run_store.saved_feedback[0]["message"] == (
        "Export dropdown is confusing\nPlease keep it clickable."
    )


def test_feedback_submit_creates_github_issue_when_token_configured(monkeypatch):
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_complete_with_tool(**kwargs):
        return {
            "title": "Clarify export dropdown behavior",
            "problem_summary": "The export dropdown behavior is confusing.",
            "suggested_solution": "Keep the format selector enabled and disable only export.",
            "acceptance_criteria": ["Format dropdown remains clickable before results exist."],
        }

    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"html_url": "https://github.com/oncokb/agentic-cancer-gene-classification/issues/42"}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(main, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(main.settings, "github_token", "gh-token-123")
    monkeypatch.setattr(main.settings, "github_repo", "oncokb/agentic-cancer-gene-classification")
    monkeypatch.setattr(main, "httpx", type("_httpx", (), {"AsyncClient": FakeAsyncClient}))
    client = TestClient(main.app)

    response = client.post(
        "/v1/feedback",
        json={"category": "bug", "message": "Export dropdown is confusing"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["issue_url"] == "https://github.com/oncokb/agentic-cancer-gene-classification/issues/42"
    assert seen["url"] == "https://api.github.com/repos/oncokb/agentic-cancer-gene-classification/issues"
    assert seen["headers"]["Authorization"] == "Bearer gh-token-123"
    assert seen["json"]["title"] == "Clarify export dropdown behavior"


def test_feedback_submit_omits_issue_url_when_token_not_configured(monkeypatch):
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_complete_with_tool(**kwargs):
        return {
            "title": "Clarify export dropdown behavior",
            "problem_summary": "The export dropdown behavior is confusing.",
            "suggested_solution": "Keep the format selector enabled and disable only export.",
            "acceptance_criteria": ["Format dropdown remains clickable before results exist."],
        }

    monkeypatch.setattr(main, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(main.settings, "github_token", "")
    client = TestClient(main.app)

    response = client.post(
        "/v1/feedback",
        json={"category": "bug", "message": "Export dropdown is confusing"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["issue_url"] is None
    assert payload["issue_title"] == "Clarify export dropdown behavior"


def test_feedback_submit_falls_back_when_github_api_fails(monkeypatch):
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_complete_with_tool(**kwargs):
        return {
            "title": "Clarify export dropdown behavior",
            "problem_summary": "The export dropdown behavior is confusing.",
            "suggested_solution": "Keep the format selector enabled and disable only export.",
            "acceptance_criteria": ["Format dropdown remains clickable before results exist."],
        }

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(main, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(main.settings, "github_token", "gh-token-123")
    monkeypatch.setattr(main, "httpx", type("_httpx", (), {"AsyncClient": FakeAsyncClient}))
    client = TestClient(main.app)

    response = client.post(
        "/v1/feedback",
        json={"category": "bug", "message": "Export dropdown is confusing"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["issue_url"] is None
    assert payload["issue_title"] == "Clarify export dropdown behavior"


def test_feedback_submit_falls_back_when_llm_fails(monkeypatch):
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_complete_with_tool(**kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(main, "complete_with_tool", fake_complete_with_tool)
    client = TestClient(main.app)

    response = client.post(
        "/v1/feedback",
        json={
            "category": "feature_request",
            "message": "Please add PDF export.",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["issue_title"].startswith("Feedback:")
    assert "Please add PDF export." in payload["issue_body"]
    assert run_store.saved_feedback
