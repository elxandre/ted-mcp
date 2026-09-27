import json

import httpx
import pytest
from mcp.client.client import Client

import ted_mcp.server as srv

SAMPLE = {
    "notices": [
        {
            "publication-number": "612345-2026",
            "publication-date": "2026-09-20+02:00",
            "notice-type": "cn-standard",
            "notice-title": {"ron": "Servicii de securitate cibernetica NIS2", "eng": "Cybersecurity services NIS2"},
            "buyer-name": {"ron": ["Primaria Botosani"]},
            "buyer-country": ["ROU"],
            "classification-cpv": ["72000000", "79417000"],
            "total-value": 250000,
            "total-value-cur": "RON",
            "links": {"html": {"ENG": "https://ted.europa.eu/en/notice/-/detail/612345-2026"}},
        }
    ],
    "totalNoticeCount": 42,
    "iterationNextToken": None,
    "timedOut": False,
}


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        status, payload = self.responses.pop(0)
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


@pytest.fixture
def ted(monkeypatch):
    def install(*responses):
        rec = Recorder(responses)
        monkeypatch.setattr(srv, "_transport", httpx.MockTransport(rec))
        monkeypatch.setattr(srv, "_last_call", 0.0)
        return rec
    return install


def structured(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


# ---------------- query builder ---------------- #
def test_build_query_full():
    q = srv.build_query(["NIS2", "cyber security"], ["ro", "DEU"], ["72000000"], "2026-09-01", "2026-09-24",
                        ["cn-standard"])
    assert q == ('(FT ~ NIS2 OR FT ~ "cyber security") AND buyer-country IN (ROU DEU) AND '
                 'classification-cpv IN (72000000) AND publication-date>=20260901 AND publication-date<=20260924 '
                 'AND notice-type IN (cn-standard) SORT BY publication-date DESC')


@pytest.mark.parametrize("kwargs", [
    {},
    {"countries": ["Romania"]},
    {"cpv_codes": ["7200"]},
    {"published_since": "01/09/2026"},
    {"notice_types": ["cn standard; DROP"]},
    {"keywords": ['"']},
])
def test_build_query_rejects_bad_input(kwargs):
    with pytest.raises(ValueError):
        srv.build_query(**kwargs)


def test_keyword_injection_is_neutralised():
    q = srv.build_query(keywords=['x") OR buyer-country=USA OR ("y'])
    # quotes, brackets and '=' are stripped, so the payload stays one quoted phrase
    assert q.startswith('(FT ~ "x OR buyer-country USA OR y")')
    assert "buyer-country=" not in q and q.count('"') == 2


# ---------------- tools via in-memory MCP client ---------------- #
async def test_tools_listed_and_read_only():
    async with Client(srv.mcp) as c:
        tools = (await c.list_tools()).tools
    names = {t.name for t in tools}
    assert names == {"search_tenders", "count_tenders", "search_notices", "check_query"}
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


async def test_search_tenders_normalises(ted):
    rec = ted((200, SAMPLE))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_tenders", {"keywords": ["NIS2"], "countries": ["RO"], "limit": 5}))
    body = rec.bodies[0]
    assert body["scope"] == "ACTIVE" and body["limit"] == 5 and body["paginationMode"] == "PAGE_NUMBER"
    assert "buyer-country IN (ROU)" in body["query"]
    assert res["total"] == 42 and res["returned"] == 1
    n = res["notices"][0]
    assert n["title"] == "Cybersecurity services NIS2"        # English picked from multilingual dict
    assert n["buyer"] == "Primaria Botosani"                  # falls back to first language
    assert n["cpv"] == "72000000; 79417000"
    assert n["url"].startswith("https://ted.europa.eu/")
    assert "third-party data" in res["note"]


async def test_count_tenders_uses_limit_1_and_all_scope(ted):
    rec = ted((200, {"notices": [], "totalNoticeCount": 1234, "timedOut": False}))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("count_tenders", {"keywords": ["SBOM"], "published_since": "2026-01-01"}))
    assert rec.bodies[0]["limit"] == 1 and rec.bodies[0]["scope"] == "ALL"
    assert res["total"] == 1234


async def test_field_error_falls_back_to_minimal_fields(ted):
    rec = ted((400, "Unknown field: total-value-cur"), (200, SAMPLE))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_notices", {"query": "FT ~ kubernetes", "limit": 3}))
    assert rec.bodies[1]["fields"] == srv.FALLBACK_FIELDS
    assert "warning" in res


async def test_retries_on_429_then_succeeds(ted, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    rec = ted((429, "slow down"), (503, "busy"), (200, SAMPLE))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_tenders", {"cpv_codes": ["72000000"]}))
    assert len(rec.bodies) == 3 and res["returned"] == 1


async def test_bad_limit_is_tool_error(ted):
    ted()
    async with Client(srv.mcp) as c:
        r = await c.call_tool("search_tenders", {"keywords": ["cloud"], "limit": 500})
    assert r.is_error


async def test_check_query_reports_invalid(ted):
    ted((400, "Syntax error at position 3"))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("check_query", {"query": "FT~~bad"}))
    assert res["valid"] is False and "Syntax error" in res["error"]


# ---------------- HTTP auth middleware ---------------- #
async def _call_asgi(app, path, headers=None, query=b""):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "path": path, "query_string": query, "headers": headers or [], "method": "POST"}
    await app(scope, receive, send)
    return sent[0]["status"]


async def test_token_auth_middleware():
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    token = "a" * 32
    app = srv.TokenAuth(ok_app, token)
    assert await _call_asgi(app, "/mcp") == 401
    assert await _call_asgi(app, "/mcp", query=b"token=wrong") == 401
    assert await _call_asgi(app, "/mcp", query=f"token={token}".encode()) == 200
    assert await _call_asgi(app, "/mcp", headers=[(b"authorization", f"Bearer {token}".encode())]) == 200
    assert await _call_asgi(app, "/healthz") == 200


async def test_error_messages_reach_the_client(ted):
    ted((500, "internal"), (500, "internal"), (500, "internal"))
    async with Client(srv.mcp) as c:
        bad = await c.call_tool("search_tenders", {"countries": ["Romania"]})
        down = await c.call_tool("count_tenders", {"keywords": ["x1"]})
    assert bad.is_error and "ISO alpha-3" in bad.content[0].text
    assert down.is_error and "HTTP 500" in down.content[0].text


# Trimmed from a real TED response (2026-09-24, buyer-country=ROU): lowercase title keys, uppercase link keys.
REAL = {
    "notices": [{
        "publication-number": "599753-2026",
        "links": {
            "xml": {"MUL": "https://ted.europa.eu/en/notice/599753-2026/xml"},
            "pdf": {"ENG": "https://ted.europa.eu/en/notice/599753-2026/pdf"},
            "html": {"RON": "https://ted.europa.eu/ro/notice/-/detail/599753-2026",
                     "ENG": "https://ted.europa.eu/en/notice/-/detail/599753-2026"},
        },
        "notice-title": {
            "ron": "România – Pachete software pentru copii de siguranţă (backup) sau recuperare – Subscripții DELL VxRail -Lot 1",
            "eng": "Romania – Backup or recovery software package – Subscripții DELL VxRail -Lot 1",
        },
    }],
    "totalNoticeCount": 3260, "iterationNextToken": None, "timedOut": False,
}


async def test_real_ted_response_shape(ted):
    ted((200, REAL))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_tenders", {"countries": ["RO"], "published_since": "2026-09-01"}))
    n = res["notices"][0]
    assert res["total"] == 3260
    assert n["title"].startswith("Romania – Backup or recovery software package")
    assert n["url"] == "https://ted.europa.eu/en/notice/-/detail/599753-2026"
    assert "extra" not in n  # links are consumed, not dumped into output


async def test_romanian_language_preference(ted):
    ted((200, REAL))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_tenders", {"countries": ["RO"], "language": "RON"}))
    n = res["notices"][0]
    assert n["title"].startswith("România") and "/ro/" in n["url"]


async def test_award_winners_deduplicated(ted):
    award = {"notices": [{"publication-number": "1-2026", "notice-type": "can-standard",
                          "winner-name": {"hun": ["p2m Informatika Kft.", "p2m Consulting Kft."] * 30},
                          "buyer-name": {"hun": ["Nemzeti Kommunikációs Hivatal"]}}],
             "totalNoticeCount": 89, "timedOut": False}
    ted((200, award))
    async with Client(srv.mcp) as c:
        res = structured(await c.call_tool("search_notices", {
            "query": "FT ~ NIS2 AND notice-type IN (can-standard)",
            "fields": ["publication-number", "notice-type", "winner-name", "buyer-name"]}))
    n = res["notices"][0]
    assert n["winners"] == "p2m Informatika Kft.; p2m Consulting Kft."
    assert "extra" not in n
