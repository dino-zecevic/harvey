"""The Google Sheets client: service-account sign-in, reads, cell writes and a
checked append, against a fake Sheets API."""

import json
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from harvey.integrations.sheets import SheetsClient, SheetsError, a1, column_letter, used

pytest.importorskip("google.auth")
crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
from cryptography.hazmat.primitives import serialization  # noqa: E402


@pytest.fixture
def key_file(tmp_path):
    key = crypto.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({
        "type": "service_account", "client_email": "bot@project.iam.gserviceaccount.test",
        "private_key": pem, "private_key_id": "k1", "token_uri": "https://oauth2.example/token",
    }))
    return str(path)


class FakeApi:
    def __init__(self, values, grid_rows=1000, status=200):
        self.values = values
        self.grid_rows = grid_rows
        self.status = status
        self.calls = []

    def handler(self, request: httpx.Request):
        self.calls.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.example/token"):
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {}})
        path = unquote(request.url.path)
        if request.method == "GET" and "/values/" in path:
            return httpx.Response(200, json={"values": self.values})
        if request.method == "GET":
            return httpx.Response(200, json={"sheets": [{"properties": {
                "sheetId": 7, "title": "leads",
                "gridProperties": {"rowCount": self.grid_rows, "columnCount": 5}}}]})
        body = json.loads(request.content or b"{}")
        if path.endswith(":batchUpdate") and "/values" not in path:
            for req in body["requests"]:
                if req["appendDimension"]["dimension"] == "ROWS":
                    self.grid_rows += req["appendDimension"]["length"]
            return httpx.Response(200, json={})
        if path.endswith("values:batchUpdate"):
            for d in body["data"]:
                self._put(d["range"], d["values"])
            return httpx.Response(200, json={})
        if request.method == "PUT":
            self._put(body["range"], body["values"])
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call {request.method} {url}")

    def _put(self, rng, rows):
        cell = rng.split("!")[1]
        col = ord(cell[0]) - 64
        row = int(cell[1:])
        for i, values in enumerate(rows):
            while len(self.values) < row + i:
                self.values.append([])
            target = self.values[row + i - 1]
            for j, v in enumerate(values):
                while len(target) < col + j:
                    target.append("")
                target[col + j - 1] = v


def client(api, key_file):
    async def no_sleep(_):
        return None
    return SheetsClient("sheet123", key_file, "leads",
                        transport=httpx.MockTransport(api.handler), sleep=no_sleep)


def test_a1_notation():
    assert [column_letter(n) for n in (1, 26, 27, 52)] == ["A", "Z", "AA", "AZ"]
    assert a1("leads", 5, 3) == "'leads'!C5"
    assert a1("Joe's", 1, 1) == "'Joe''s'!A1"
    assert used([["a", ""], ["", ""], []]) == [["a"]]


@pytest.mark.asyncio
async def test_read_signs_in_once_and_reads_the_tab(key_file):
    api = FakeApi([["email", "status"], ["a@x.test", "sent"]])
    c = client(api, key_file)
    assert await c.read() == [["email", "status"], ["a@x.test", "sent"]]
    await c.read()
    assert sum(1 for r in api.calls if "oauth2" in str(r.url)) == 1
    assert "'leads'" in unquote(api.calls[1].url.path)


@pytest.mark.asyncio
async def test_no_access_says_who_to_share_with(key_file):
    with pytest.raises(SheetsError, match="bot@project.iam.gserviceaccount.test"):
        await client(FakeApi([], status=403), key_file).read()


@pytest.mark.asyncio
async def test_missing_settings_are_named(tmp_path):
    assert "GOOGLE_SHEET_ID" in SheetsClient("", "k.json", "leads").problem()
    assert "GOOGLE_SHEETS_CREDENTIALS" in SheetsClient("id", "", "leads").problem()
    assert "doesn't exist" in SheetsClient("id", str(tmp_path / "no.json"), "leads").problem()
    with pytest.raises(SheetsError):
        await SheetsClient("", "", "leads").read()


@pytest.mark.asyncio
async def test_cells_are_written_where_asked(key_file):
    api = FakeApi([["email", "status"], ["a@x.test", "sent"]])
    await client(api, key_file).write_cells([(2, 2, "clicked")])
    assert api.values[1] == ["a@x.test", "clicked"]


@pytest.mark.asyncio
async def test_append_goes_under_the_last_used_row_and_grows_the_grid(key_file):
    api = FakeApi([["email", "status"], ["a@x.test", "sent"], ["", ""]], grid_rows=3)
    n = await client(api, key_file).append_rows([["b@x.test", "sent"], ["c@x.test", "sent"]])
    assert n == 2
    assert [r[0] for r in api.values if r] == ["email", "a@x.test", "b@x.test", "c@x.test"]
    assert api.grid_rows >= 4


@pytest.mark.asyncio
async def test_an_append_that_moved_other_rows_is_reported(key_file):
    api = FakeApi([["email", "status"], ["a@x.test", "sent"]])
    original = api._put

    def clobber(rng, rows):
        original("'leads'!A2", rows)       # lands on top of an existing row
    api._put = clobber
    with pytest.raises(SheetsError, match="didn't check out"):
        await client(api, key_file).append_rows([["b@x.test", "sent"]])


def test_a_relative_key_path_is_relative_to_harvey_not_the_cwd(tmp_path, monkeypatch):
    from harvey.paths import PROJECT_ROOT
    monkeypatch.chdir(tmp_path)
    c = SheetsClient("id", "secrets/sa.json", "leads")
    assert c._key_path() == PROJECT_ROOT / "secrets" / "sa.json"
    assert SheetsClient("id", str(tmp_path / "sa.json"), "leads")._key_path() == tmp_path / "sa.json"
