# Running with Docker

```bash
docker compose up --build
```

Then open http://localhost:8000. The one container serves both the API (`POST /route`) and the UI.

This needs no API keys and costs nothing. It starts in demo mode, where the answers, costs and quality scores are simulated.

## Environment variables

| Variable | Needed for | Default | Meaning |
|---|---|---|---|
| `AWR_WEB_ALLOW_PAID` | Real calls | unset | Set to `1` to make real, billed model calls. Anything else stays in demo mode, even with keys set. |
| `AWR_WEB_DAILY_BUDGET_USD` | Real calls | `0.50` | Refuse requests once today's spend reaches this. Resets at 00:00 UTC and on restart. |
| `OPENAI_API_KEY` | Real calls | unset | `gpt-4o-mini`, the budget tier of the default roster. |
| `DEEPSEEK_API_KEY` | Real calls | unset | `deepseek-chat`, the mid tier of the default roster. |
| `ANTHROPIC_API_KEY` | Real calls | unset | `claude-opus-4-8`, the frontier tier of the default roster. |
| `GOOGLE_API_KEY` | Other rosters | unset | `gemini-2.0-flash`, used by `cross_vendor_4`. |
| `MOONSHOT_API_KEY` | Other rosters | unset | `kimi-k3`, used by `kimi_mid`. |

Put these in a `.env` file (copy `.env.example`). Compose passes them to the container at start-up. They are never copied into the image: `.dockerignore` only lets in `pyproject.toml` and `src/`. `.env` is gitignored, so keep keys there and nowhere else.

## Limits

- **Local use only.** The app has no login, so the port is published on `127.0.0.1` and the app rejects other `Host` headers. Do not publish it more widely without adding authentication.
- **Nothing is persisted.** The daily spend total lives in memory and the classifier's cache lives in the container, so both are lost when the container is recreated.
