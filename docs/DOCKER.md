# Running with Docker

```bash
docker compose up --build
```

That starts two services from one image:

- `app` on http://localhost:8000: the web page and its API (`POST /route`).
- `api` on http://localhost:8787/v1: the OpenAI-compatible endpoint. It needs a bearer token. Set `AWR_SERVER_TOKEN` before starting, or leave it unset and read the generated one, shown once, with `docker compose logs api`.

This needs no API keys and costs nothing. Both start in demo mode, where the answers, costs and quality scores are simulated.

## Environment variables

| Variable | Needed for | Default | Meaning |
|---|---|---|---|
| `AWR_WEB_ALLOW_PAID` | Real calls | unset | Set to `1` to make real, billed model calls. Anything else stays in demo mode, even with keys set. |
| `AWR_WEB_DAILY_BUDGET_USD` | Real calls | `0.50` | Refuse requests once today's spend reaches this. Resets at 00:00 UTC. The total is kept in `data/spend.db` on the shared volume (see `AWR_SPEND_STORE` in the README). |
| `AWR_SERVER_TOKEN` | The `api` service | generated | Bearer token for port 8787. Never put it in a file that is committed. |
| `AWR_API_FORCE_MOCK` | Real calls on `api` | `1` | `1` keeps the `api` answers simulated. Set to `0`, with keys, for real, billed calls. |
| `AWR_API_DAILY_BUDGET_USD` | Real calls on `api` | `1.00` | Daily cap for the `api` service, per UTC day. |
| `AWR_HOME` | Both | `/var/lib/awr` in the image | Folder that holds `data/` and `.cache/`. |
| `OPENAI_API_KEY` | Real calls | unset | `gpt-4o-mini`, the budget tier of the default roster. |
| `DEEPSEEK_API_KEY` | Real calls | unset | `deepseek-chat`, the mid tier of the default roster. |
| `ANTHROPIC_API_KEY` | Real calls | unset | `claude-opus-4-8`, the frontier tier of the default roster. |
| `GOOGLE_API_KEY` | Other rosters | unset | `gemini-2.0-flash`, used by `cross_vendor_4`. |
| `MOONSHOT_API_KEY` | Other rosters | unset | `kimi-k3`, used by `kimi_mid`. |

Put these in a `.env` file (copy `.env.example`). Compose passes them to the container at start-up. They are never copied into the image: `.dockerignore` only lets in `pyproject.toml` and `src/`. `.env` is gitignored, so keep keys there and nowhere else.

## Limits

- **Local use only.** The web page has no login, so both ports are published on `127.0.0.1` and both services reject other `Host` headers. Do not publish it more widely without adding authentication.
- **State lives in one Docker volume.** The daily spend total (`data/spend.db`) and the classifier's cache are kept in the `awr-data` volume, shared by both services. They survive a restart and are removed by `docker compose down -v`.
- **The real-call path of the `api` service has not been run in Docker.** Only simulated answers were checked.
