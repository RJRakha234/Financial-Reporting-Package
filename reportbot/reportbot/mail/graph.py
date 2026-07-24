"""Microsoft Graph mail backend — reads an Outlook.com / Microsoft 365 mailbox
over the Graph REST API using the app-only client-credentials flow.

Requires an Azure AD app registration with the ``Mail.Read`` *application*
permission (admin-consented) and the third-party libraries ``msal`` and
``requests`` (imported lazily so the rest of the package works without them).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .base import Message

_GRAPH = "https://graph.microsoft.com/v1.0"
_SCOPE = ["https://graph.microsoft.com/.default"]
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


class GraphMailClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        user: str,
    ):
        missing = [
            name
            for name, val in (
                ("tenant_id", tenant_id),
                ("client_id", client_id),
                ("client_secret", client_secret),
                ("user", user),
            )
            if not val
        ]
        if missing:
            raise ValueError(
                "Graph backend is missing required settings: "
                + ", ".join(missing)
                + " (set them in config / environment)."
            )
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.user = user
        self._token: str | None = None

    def _get_token(self) -> str:
        try:
            import msal
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "The Graph backend needs the 'msal' package. "
                "Install it with: pip install msal requests"
            ) from exc
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(scopes=_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                "Graph auth failed: "
                + result.get("error_description", str(result))
            )
        return result["access_token"]

    def fetch_recent(self, folder: str, lookback_days: int) -> list[Message]:
        try:
            import requests
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "The Graph backend needs the 'requests' package. "
                "Install it with: pip install msal requests"
            ) from exc

        token = self._get_token()
        since = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 0))
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        url = (
            f"{_GRAPH}/users/{self.user}/mailFolders/"
            f"{folder}/messages"
        )
        params = {
            "$filter": f"receivedDateTime ge {since_iso}",
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
            "$top": "50",
        }
        headers = {"Authorization": f"Bearer {token}"}

        messages: list[Message] = []
        while url:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            for item in payload.get("value", []):
                messages.append(self._to_message(item))
            url = payload.get("@odata.nextLink")
            params = None  # nextLink already carries the query
        return messages

    def _to_message(self, item: dict) -> Message:
        sender = ""
        frm = item.get("from") or {}
        addr = (frm.get("emailAddress") or {}) if isinstance(frm, dict) else {}
        if addr:
            name = addr.get("name", "")
            email_addr = addr.get("address", "")
            sender = f"{name} <{email_addr}>".strip()

        body = ""
        body_obj = item.get("body") or {}
        if isinstance(body_obj, dict):
            content = body_obj.get("content", "") or ""
            if body_obj.get("contentType") == "html":
                content = re.sub(r"<[^>]+>", " ", content)
            body = content
        if not body:
            body = item.get("bodyPreview", "") or ""

        received: datetime | None = None
        if item.get("receivedDateTime"):
            try:
                received = datetime.fromisoformat(
                    item["receivedDateTime"].replace("Z", "+00:00")
                )
            except ValueError:
                received = None

        return Message(
            id=item.get("id", ""),
            sender=sender,
            subject=item.get("subject", "") or "",
            body=body,
            received=received,
            links=_URL_RE.findall(body),
        )
