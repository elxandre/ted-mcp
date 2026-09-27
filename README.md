# ted-mcp: EU public tenders for AI agents

Read-only MCP server for **TED (Tenders Electronic Daily)**, the EU's official public-procurement journal. It uses TED's free, keyless Search API v3; no account or API key is needed.

## Tools

| Tool | What it does |
|---|---|
| `search_tenders` | Search with simple filters: keywords, countries, CPV codes, date range, notice type. Returns title, buyer, country, CPV, value, link |
| `count_tenders` | Count matches only (one tiny request). Compare two date windows to see if demand is rising |
| `search_notices` | Raw TED expert query, for advanced use |
| `check_query` | Validate expert-query syntax without fetching notices |

All tools are marked read-only. Notice text is returned as third-party data, and the server tells the AI to treat it as data, not instructions.

Example prompts once connected:
- *"How many EU tenders mentioning NIS2 were published since 2026-01-01, vs the same period last year?"*
- *"List open Romanian IT tenders (CPV 72000000) published this month."*
- *"Find active tenders in DE, AT and NL mentioning SBOM or 'Cyber Resilience Act'."*

## 1. First run: check it against the real API

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                      # 20 tests, TED is mocked

# Real API smoke test (no key needed):
curl -s -X POST https://api.ted.europa.eu/v3/notices/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"buyer-country=ROU AND publication-date>=20260901","fields":["publication-number","notice-title"],"limit":2}' | head -c 600
```

> **Query syntax** follows TED's official Expert Search help: single words unquoted (`FT ~ kubernetes`), phrases quoted (`FT ~ "cyber security"`), and lists via `IN` (`buyer-country IN (ROU DEU)`). Use `check_query` to validate anything custom before running it.

## 2. Claude Desktop (stdio, local)

Settings → Developer → Edit Config, then add:

```json
{
  "mcpServers": {
    "ted-tenders": {
      "command": "/ABSOLUTE/PATH/ted-mcp/.venv/bin/ted-mcp"
    }
  }
}
```

Windows path example: `C:\\Users\\you\\ted-mcp\\.venv\\Scripts\\ted-mcp.exe`. Restart Claude Desktop.

## 3. Claude Code

```bash
claude mcp add ted-tenders -- /ABSOLUTE/PATH/ted-mcp/.venv/bin/ted-mcp
```

## 4. Remote connector (claude.ai web, mobile)

claude.ai custom connectors need a public **HTTPS** URL. HTTP mode refuses to start without a token.

```bash
export TED_MCP_TOKEN=$(openssl rand -hex 24)     # save this in your password manager
docker build -t ted-mcp .
docker run -d --name ted-mcp --restart unless-stopped \
  -e TED_MCP_TOKEN=$TED_MCP_TOKEN \
  -e TED_MCP_ALLOWED_HOSTS=ted.example.com \
  -p 127.0.0.1:8000:8000 ted-mcp
```

Put TLS in front, for example with Caddy (automatic Let's Encrypt):

```
ted.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then in claude.ai → Settings → Connectors → **Add custom connector**:

```
https://ted.example.com/mcp?token=YOUR_TOKEN
```

Health check (no token needed): `https://ted.example.com/healthz`

### Security notes
- The token is compared in constant time; requests without it get `401`.
- A token in a URL can end up in proxy logs. Keep access logs private and **rotate the token** if exposed. MCP clients that can send headers should use `Authorization: Bearer <token>` instead.
- `TED_MCP_ALLOWED_HOSTS` enables DNS-rebinding protection. Set it to your domain.
- The container runs as a non-root user and exposes only port 8000 on localhost. The server makes outbound calls to TED only.
- Built-in politeness: at most 2 concurrent requests and 0.4 s between calls, with retries on 429/5xx.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TED_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `TED_MCP_HOST` / `TED_MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind address |
| `TED_MCP_TOKEN` | none | Required for HTTP; minimum 24 characters |
| `TED_MCP_ALLOWED_HOSTS` | none | Comma-separated hostnames for DNS-rebinding protection |
| `TED_API_URL` | TED v3 search | Override for testing |

## Data and licence
TED notice metadata is published by the EU Publications Office for free reuse, including commercial use. Keep "Source: TED (ted.europa.eu)" attribution when you republish results. This project is independent and not affiliated with the EU. Code: MIT.
