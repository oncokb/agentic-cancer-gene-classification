"""Google Sheets export for reviewed annotation results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from src.config import settings
from src.models.schema import (
    AnnotationResult,
    GoogleSheetExportResponse,
    GoogleSheetsConfigStatus,
    GoogleSheetsServiceAccountConfigResponse,
)
from src.pipeline.local_config import app_config_dir, write_secret_file
from src.pipeline.results_export import (
    ANNOTATION_RESULTS_CSV_HEADERS,
    build_annotation_results_csv_rows,
)


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SERVICE_ACCOUNT_FILENAME = "google-service-account.json"


class GoogleSheetsExportError(RuntimeError):
    """Raised when a Google Sheets export cannot be completed."""


def annotation_result_sheet_values(result: AnnotationResult) -> list[list[str]]:
    """Return header + row values matching the CSV export columns."""
    rows = build_annotation_results_csv_rows(result)
    return [
        ANNOTATION_RESULTS_CSV_HEADERS,
        *[
            [row.get(header, "") for header in ANNOTATION_RESULTS_CSV_HEADERS]
            for row in rows
        ],
    ]


def export_annotation_results_to_google_sheet(
    result: AnnotationResult,
    *,
    spreadsheet_id: str,
    sheet_name: str = "Annotation Results",
    credentials_path: Optional[str] = None,
) -> GoogleSheetExportResponse:
    """Replace a target worksheet's contents with reviewed annotation results."""
    credentials_file = credentials_path or _configured_credentials_path()
    if not credentials_file:
        raise GoogleSheetsExportError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not configured. "
            "Upload a Google service account JSON file in the UI or set the env var."
        )

    session = _authorized_session(credentials_file)
    sheet_title = sheet_name.strip() or "Annotation Results"
    _ensure_sheet(session, spreadsheet_id, sheet_title)
    values = annotation_result_sheet_values(result)
    escaped_title = _quote_sheet_name(sheet_title)
    clear_range = f"{escaped_title}!A:Z"
    update_range = f"{escaped_title}!A1"
    clear_range_path = quote(clear_range, safe="")
    update_range_path = quote(update_range, safe="")

    _request(
        session,
        "post",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{clear_range_path}:clear",
        json={},
    )
    payload = {"majorDimension": "ROWS", "values": values}
    response = _request(
        session,
        "put",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/"
        f"{update_range_path}",
        params={"valueInputOption": "RAW"},
        json=payload,
    )

    return GoogleSheetExportResponse(
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_title,
        updated_range=response.get("updatedRange", update_range),
        updated_rows=int(response.get("updatedRows", len(values))),
        updated_columns=int(
            response.get("updatedColumns", len(ANNOTATION_RESULTS_CSV_HEADERS))
        ),
        spreadsheet_url=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
    )


def google_sheets_config_status() -> GoogleSheetsConfigStatus:
    """Return the currently configured Google Sheets credential source."""
    env_path = settings.google_service_account_json
    if env_path:
        return _status_for_path(env_path, source="environment")

    local_path = default_service_account_credentials_path()
    if local_path.exists():
        return _status_for_path(str(local_path), source="local_upload")

    return GoogleSheetsConfigStatus(configured=False)


def save_service_account_credentials(
    service_account_json: str,
) -> GoogleSheetsServiceAccountConfigResponse:
    """Validate and save service account JSON uploaded through the local UI."""
    payload = _parse_service_account_json(service_account_json)
    target_path = default_service_account_credentials_path()
    write_secret_file(target_path, json.dumps(payload, indent=2) + "\n")

    email = str(payload["client_email"])
    return GoogleSheetsServiceAccountConfigResponse(
        configured=True,
        service_account_email=email,
        credentials_path=str(target_path),
        message=(
            "Google Sheets export is configured. Share the target spreadsheet "
            f"with {email} before exporting."
        ),
    )


def default_service_account_credentials_path() -> Path:
    return app_config_dir() / SERVICE_ACCOUNT_FILENAME


def _authorized_session(credentials_file: str) -> Any:
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise GoogleSheetsExportError(
            "Google Sheets export dependencies are not installed. "
            "Install the package dependencies and try again."
        ) from exc

    credentials = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=[SHEETS_SCOPE],
    )
    return AuthorizedSession(credentials)


def _configured_credentials_path() -> str:
    if settings.google_service_account_json:
        return settings.google_service_account_json
    local_path = default_service_account_credentials_path()
    return str(local_path) if local_path.exists() else ""


def _status_for_path(path: str, *, source: str) -> GoogleSheetsConfigStatus:
    try:
        payload = _parse_service_account_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, GoogleSheetsExportError):
        return GoogleSheetsConfigStatus(
            configured=False,
            source=source,
            credentials_path=path,
        )

    return GoogleSheetsConfigStatus(
        configured=True,
        source=source,
        service_account_email=str(payload["client_email"]),
        credentials_path=path,
    )


def _parse_service_account_json(service_account_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise GoogleSheetsExportError("The uploaded file is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise GoogleSheetsExportError("The uploaded credential must be a JSON object.")
    if payload.get("type") != "service_account":
        raise GoogleSheetsExportError(
            "The uploaded credential is not a service account JSON file."
        )
    if not payload.get("client_email"):
        raise GoogleSheetsExportError("The service account JSON is missing client_email.")
    if not payload.get("private_key"):
        raise GoogleSheetsExportError("The service account JSON is missing private_key.")
    return payload


def _ensure_sheet(session: Any, spreadsheet_id: str, sheet_name: str) -> None:
    metadata = _request(
        session,
        "get",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
        params={"fields": "sheets(properties(title))"},
    )
    sheets = metadata.get("sheets", [])
    if any(sheet.get("properties", {}).get("title") == sheet_name for sheet in sheets):
        return

    _request(
        session,
        "post",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        json={
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": sheet_name,
                        }
                    }
                }
            ]
        },
    )


def _request(session: Any, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = getattr(session, method)(url, **kwargs)
    if response.status_code >= 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = payload.get("error", {}).get("message") or response.text
        raise GoogleSheetsExportError(message or f"Google Sheets request failed: {response.status_code}")
    if response.content:
        return response.json()
    return {}


def _quote_sheet_name(sheet_name: str) -> str:
    return f"'{sheet_name.replace(chr(39), chr(39) + chr(39))}'"
