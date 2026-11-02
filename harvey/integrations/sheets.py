"""Google Sheets over its REST API, signed in as a service account.

Only what the lead-sheet sync needs: read one worksheet, write single
cells, and add rows at the bottom. A service account (a JSON key file from
Google Cloud) is used rather than your Google login, so the only access it
has is to sheets shared with its email address.

``google-auth`` signs the token request; it's optional
(``pip install -e '.[sheets]'``) because only the sheet sync uses it.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.sheets")

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
# Sheets quotas are per minute: a 429 waits and tries again, twice.
RETRY_WAITS = (10, 30)


class SheetsError(Exception):
    """The sheet couldn't be read or written, or a write didn't land as expected."""


def column_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    if n < 1:
        raise ValueError("columns start at 1")
    letters = ""
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def a1(worksheet: str, row: int, col: int) -> str:
    """One cell of a worksheet in A1 notation: 'leads'!C5."""
    name = worksheet.replace("'", "''")
    return f"'{name}'!{column_letter(col)}{row}"


def used(values: list[list]) -> list[list[str]]:
    """Rows with trailing blank cells and trailing blank rows dropped."""
    rows = []
    for row in values:
        row = ["" if v is None else str(v) for v in row]
        while row and row[-1] == "":
            row.pop()
        rows.append(row)
    while rows and not rows[-1]:
        rows.pop()
    return rows


class SheetsClient:
    def __init__(self, sheet_id: str, credentials_file: str, worksheet: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep, timeout: float = 30.0):
        self.sheet_id = (sheet_id or "").strip()
        self.credentials_file = (credentials_file or "").strip()
        self.worksheet = worksheet
        self._transport = transport
        self._sleep = sleep
        self._timeout = timeout
        self._token = ""
        self._token_expires = 0.0
        self._grid: dict | None = None

    # ── auth ──

    def _key_path(self) -> Path:
        """The key file; a relative path is relative to Harvey's folder, not the cwd."""
        path = Path(self.credentials_file).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    def problem(self) -> str:
        """Why this client can't work at all, or ""."""
        if not self.sheet_id:
            return "GOOGLE_SHEET_ID isn't set in .env"
        if not self.credentials_file:
            return "GOOGLE_SHEETS_CREDENTIALS isn't set in .env (the service-account key file)"
        if not self._key_path().is_file():
            return f"the service-account key file {self.credentials_file} doesn't exist"
        return ""

    def _key(self) -> dict:
        try:
            info = json.loads(self._key_path().read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SheetsError(f"the service-account key file can't be read ({e})") from None
        if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
            raise SheetsError("the key file isn't a service-account key (no client_email/private_key)")
        return info

    def service_account_email(self) -> str:
        return str(self._key().get("client_email", ""))

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        try:
            from google.auth import crypt, jwt
        except ImportError:
            raise SheetsError(
                "the sheet sync needs google-auth: run  pip install -e '.[sheets]'") from None
        info = self._key()
        token_uri = info.get("token_uri") or DEFAULT_TOKEN_URI
        try:
            signer = crypt.RSASigner.from_service_account_info(info)
        except Exception as e:
            raise SheetsError(f"the service-account key can't be used ({e})") from None
        now = int(time.time())
        assertion = jwt.encode(signer, {
            "iss": info["client_email"], "scope": SCOPE, "aud": token_uri,
            "iat": now, "exp": now + 3600,
        })
        if isinstance(assertion, bytes):
            assertion = assertion.decode("ascii")
        resp = await client.post(token_uri, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        })
        if resp.status_code != 200:
            raise SheetsError(f"Google refused the service-account sign-in ({resp.status_code})")
        data = resp.json()
        self._token = str(data.get("access_token", ""))
        self._token_expires = time.time() + float(data.get("expires_in", 3600))
        if not self._token:
            raise SheetsError("Google's sign-in answer had no access token")
        return self._token

    async def _call(self, method: str, url: str, **kwargs) -> dict:
        problem = self.problem()
        if problem:
            raise SheetsError(problem)
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            for attempt in range(len(RETRY_WAITS) + 1):
                token = await self._access_token(client)
                resp = await client.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
                if resp.status_code == 429 and attempt < len(RETRY_WAITS):
                    await self._sleep(RETRY_WAITS[attempt])
                    continue
                if resp.status_code == 401 and attempt == 0:
                    self._token = ""   # expired early: sign in again once
                    continue
                if resp.status_code in (403, 404):
                    raise SheetsError(
                        f"no access to the sheet ({resp.status_code}). Share it with "
                        f"{self.service_account_email()} as an editor, and check GOOGLE_SHEET_ID.")
                if resp.status_code >= 400:
                    raise SheetsError(f"Sheets answered {resp.status_code}: {resp.text[:200]}")
                return resp.json() if resp.content else {}
        raise SheetsError("Sheets kept answering 429 (quota); trying again next cycle")

    # ── reads and writes ──

    async def read(self) -> list[list[str]]:
        """Every row of the worksheet as text, the header first."""
        name = "'" + self.worksheet.replace("'", "''") + "'"
        data = await self._call("GET", f"{API}/{self.sheet_id}/values/{quote(name, safe='')}")
        return [["" if v is None else str(v) for v in row] for row in data.get("values", [])]

    async def write_cells(self, cells: list[tuple[int, int, str]]) -> int:
        """Write single cells as (row, column, text), 1-based, in one call."""
        if not cells:
            return 0
        await self._call("POST", f"{API}/{self.sheet_id}/values:batchUpdate", json={
            "valueInputOption": "RAW",
            "data": [{"range": a1(self.worksheet, r, c), "values": [[v]]} for r, c, v in cells],
        })
        return len(cells)

    async def _sheet_grid(self) -> dict:
        data = await self._call(
            "GET", f"{API}/{self.sheet_id}",
            params={"fields": "sheets.properties(sheetId,title,gridProperties)"})
        for sheet in data.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == self.worksheet:
                grid = props.get("gridProperties", {})
                return {"id": props.get("sheetId"), "rows": int(grid.get("rowCount", 0)),
                        "cols": int(grid.get("columnCount", 0))}
        raise SheetsError(f"the sheet has no tab called '{self.worksheet}'")

    async def append_rows(self, rows: list[list[str]]) -> int:
        """Add rows under the last used row and check nothing else moved.

        The position comes from reading the sheet, not from Sheets' own
        append call, which writes after the end of a Table object and can
        land on rows below it. Read before and after: the sheet must have
        grown by exactly these rows, every earlier row unchanged.
        """
        if not rows:
            return 0
        before = used(await self.read())
        first = len(before) + 1
        last = first + len(rows) - 1
        width = max(len(r) for r in rows)
        grid = await self._sheet_grid()
        grow = []
        if grid["rows"] < last:
            grow.append({"appendDimension": {"sheetId": grid["id"], "dimension": "ROWS",
                                             "length": last - grid["rows"]}})
        if grid["cols"] < width:
            grow.append({"appendDimension": {"sheetId": grid["id"], "dimension": "COLUMNS",
                                             "length": width - grid["cols"]}})
        if grow:
            await self._call("POST", f"{API}/{self.sheet_id}:batchUpdate", json={"requests": grow})
        start = a1(self.worksheet, first, 1)
        await self._call(
            "PUT", f"{API}/{self.sheet_id}/values/{quote(start, safe='')}",
            params={"valueInputOption": "RAW"},
            json={"range": start, "majorDimension": "ROWS",
                  "values": [["" if v is None else str(v) for v in r] for r in rows]})
        after = used(await self.read())
        problems = []
        if len(after) != len(before) + len(rows):
            problems.append(f"row count went from {len(before)} to {len(after)}, "
                            f"expected {len(before) + len(rows)}")
        changed = [i + 1 for i, row in enumerate(before) if i >= len(after) or after[i] != row]
        if changed:
            problems.append(f"{len(changed)} existing row(s) changed, first at row {changed[0]}")
        if problems:
            raise SheetsError("adding rows didn't check out: " + "; ".join(problems)
                              + ". Look at the sheet (File > Version history).")
        return len(rows)
