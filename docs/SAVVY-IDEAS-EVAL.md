# Three savvy-flow ideas for the email classifier: evaluation

Date: 2 Nov 2026. Status: **evaluated, built (A–E) and live on the server.** A separate review session found fixes needed before merge (see `~/n8n-cloud/REVIEW-savvy-ideas.md`, local only).

Note: the brief described the setup as it was before 1 Nov. n8n and the weekly review have since moved to the free Oracle server (`box1`). The weekly review now runs from cron there, not launchd on the Mac. All numbers below come from the live server.

## Short answer

| Idea | Recommendation | Why, in one line |
|---|---|---|
| 1. Fresh-eyes spot-check | **Build, with one change** | It is cheap and it is the only way the weekly learning gets independent evidence. Add the gpt-4o-mini fallback decisions to the sample, because that is where mistakes are likely. |
| 2. Alert after two failures in a row | **Mostly already built (1 Nov). Add two small pieces.** | The real outages were jobs that *never ran*, not jobs that ran and failed. The healthchecks.io dead-man switch set up on 1 Nov catches those. Missing: a classifier error check and a fuller digest line. |
| 3. Share, don't duplicate | **Reuse the gate unchanged. Copy about 25 lines of prompt code.** | The weekly review already uses the router's gate. Jarvis's labelling code is about "tiers", not email categories, and `~/agentic-os` is off-limits, so a small adapted copy is the smaller option. |

## The numbers

### Data used
- **n8n execution history** on the server: 227 classifier runs, 24 Oct to 2 Nov. That is all n8n still keeps. Within them were 310 classifier decisions: 126 by Sender Rules, 107 by Jev (all at p ≥ 0.9) and 77 by the gpt-4o-mini fallback.
- **Corrections:** 0 ever, out of about 3,300 Metrics Log rows. The last 3 weekly reviews all ended "no change", and this explains why.
- **Weekly-review outputs:** the 1 Nov run reviewed 200 emails, found 0 corrections, changed nothing, and spent $0.004.

### How many confident decisions a week would be sampled?
- Confident decisions (Sender Rules plus Jev p ≥ 0.9): **about 180 a week**. 10% of that is **about 18 a week**, so the 40 cap is never reached.
- Fallback (gpt-4o-mini) decisions: **about 60 a week**.
- Recommended sample: the ~18 confident ones, plus fallback decisions to fill the rest, up to **40 a week in total**.

### What would the Opus labelling cost on the Pro plan?
I measured it. I ran one real blind labelling call on the 20 pilot emails, using Opus via `claude -p` with no tools and no connectors.
- Usage: **about 11,000 tokens in, 900 out, 11 seconds**. That is one request.
- At list price that call would be $0.10. On the Pro plan it costs **$0 extra**. It uses a small slice of the weekly allowance, about as much as one short chat message with a long paste.
- 40 emails a week would be about 15,000 tokens: still **one request a week**.

### Has Jev or Sender Rules ever been wrong when confident?
- **Pilot hand-check:** I checked 20 recent confident decisions myself (10 Sender Rules, 10 Jev at p 0.96–1.00), seeded at random. **All 20 were correct.** They were mostly job-board alerts, shop and gambling spam, and payment notices.
- **Blind Opus check** of the same 20, without showing it the chain's answer: **20 out of 20 agreed.**
- **History:** in the 24 Oct calibration, Jev was 86 out of 86 at p ≥ 0.9. There have been no corrections ever.
- **Honest caveat:** 0 wrong out of 20 still allows a real error rate of up to about 15%. Confident traffic is mostly easy email. The riskier decisions are the fallback ones: in the October calibration, gpt-4o-mini was wrong on 7 of 113 emails while claiming p ≥ 0.9.
- **What this means for idea 1:** on the *confident* decisions alone, expect 0–2 disagreements a week. The main gain is giving the weekly learning *independent* labels. Without corrections, it compares Jev against Jev's own earlier answers, which always agree, so it can never find anything to fix. (Correction: I first wrote that it was blocked by a 40-email minimum. It isn't; the 1 Nov run had 99 emails. The real blocker is that its labels are circular.)
- **Built and run (see below):** the first real spot-check found that **Opus disagreed with 17 of the 26 fallback decisions** it sampled. The confident decisions were almost clean: 1 disagreement in 14. So the mistakes are concentrated in the gpt-4o-mini fallback.

### How many failures in 90 days would "two in a row" have caught?

Five incidents between 4 Aug and 2 Nov:

| Incident | How long it went unnoticed | Caught by "error twice in a row" in n8n? | Caught by "missed or failed twice" (dead-man)? |
|---|---|---|---|
| Docker off on the Mac (3 Aug – 20 Oct) | ~11 weeks | No: nothing ran, so there were no errors | **Yes**, within about an hour |
| Monday digest not sending (until 27 Oct) | ~12 weeks: only 4 digest runs exist since 31 Jul | No: the missing runs left no errors | **Yes**, on the 2nd missed Monday |
| Anthropic credit ran out (23 Oct, ~10 failed classifier runs) | ~1 day | **Yes** | Yes, with the classifier check below |
| Weekly review's first run failed (25 Oct) | Same evening (Filip was at the keyboard) | No: it isn't an n8n job | **Yes** |
| Mac reboot, Docker down (29–31 Oct) | ~2.5 days | No: nothing ran | **Yes**, within about an hour |

- An alert based only on n8n errors would have caught **1 of 5**. In the stored history, none of the 6 classifier errors came twice in a row. The most recent one, 1 Nov 17:45, was a one-off job-alert scoring hiccup.
- An alert that also treats *a missed slot* as a failure would have caught **5 of 5**. That is what the healthchecks.io setup from 1 Nov does.

## What already exists for idea 2 (checked before adding anything)

**healthchecks.io (live since 1 Nov).** It sends an alert email when a job misses its slot or fails on its last attempt. It works even when the server is completely down.
- `daily` (Applied Sync + Follow-ups): runs at 12:00, retries at 14:00, and alerts only if the 14:00 retry also fails. That is effectively two in a row.
- `digest`: Monday, same retry rule.
- `weekly` (weekly review): Sunday 18:00, retry at 21:00.
- `alive`: n8n answering, checked every 15 minutes.
- `inbox-filer`: hourly.

**The digest's "System health" section** counts *ok* and *failed* runs for the week for four workflows: classifier, category correction, applied sync and follow-ups. Its gaps:
- a job that **never ran** shows "0 ok" and still prints "All systems ran normally";
- the weekly review and the inbox filer aren't listed;
- a failure that was later fixed by a retry still counts as failed.

**Still missing:**
- **The classifier failing while n8n is up**, for example an expired mailbox login or an OpenAI problem. `alive` only checks that n8n answers.
- **One Monday line covering everything.**

### Proposed additions
1. **A classifier check.** A tiny cron script on the server runs every 15 minutes. If the last 2 classifier runs both errored, it reports a failure to a new healthchecks check, `classifier`. Otherwise it reports OK.
2. **A fuller digest health block.** The digest reads the healthchecks status for all 6 checks, using a *read-only* API key, and lists each job as **OK / late / down**. That covers missed slots, the weekly review and the inbox filer in one place.

### Health feed for Jarvis (spec only; `~/agentic-os` not touched)
- **Endpoint:** `GET https://healthchecks.io/api/v3/checks/` with header `X-Api-Key: <read-only key>`. Read-only keys can't change or ping anything.
- **Fields per check:**
  - `name`: "n8n daily", "n8n digest", "n8n weekly", "n8n alive", "n8n inbox-filer", plus "n8n classifier" once added;
  - `status`: one of `new`, `up`, `grace`, `down` or `paused`;
  - `last_ping` and `next_ping` (ISO times);
  - `n_pings`;
  - `timeout` and `grace` (seconds);
  - `unique_key`: a stable id that read-only keys return instead of the UUID.
- **Badge rule:** any `down` = red, any `grace` = amber, otherwise green. Show `name` and `last_ping` for anything that isn't `up`.
- **Optional history:** `GET /api/v3/checks/<id>/flips/` returns the list of up/down changes.
- **To decide:** where the read-only key lives on the Mac for Jarvis, as a file outside the repo.

## Idea 3: what can be shared

| Spot-check needs | Existing code | Plan |
|---|---|---|
| Gated learning (≥ 40 labels, held-out validation, no new costly miss) | `review.py`, which already calls the router's `jev_loop.gate` | **Reuse unchanged.** Write the Opus labels in the same format as corrections (`{row id: category}`). The existing review then treats them as independent "gold" labels. No new gate code. |
| Seeded 10% sampling with a cap | `school.export`, written for Jarvis's SQLite request log | About 5 lines in the weekly review. Sharing would mean importing Jarvis's database layer for a one-line `random.sample`. |
| Blind labelling prompt and strict reply parsing | `school.label_prompt` / `parse_labels`, hard-coded to T1–T3 tiers | **Copy and adapt about 25 lines** for the 7 email categories, with a comment pointing at the original. Making Jarvis's version generic would mean editing `~/agentic-os`, which is off-limits. |
| Locked-down Opus call | `school.claude_teacher` + `LOCKDOWN` flags | Reuse the same flags. Its cwd and tool set are Jarvis-specific, so it needs a 10-line adapted call. |

## Found along the way (not part of the three ideas)
- **The weekly review never sees fallback decisions.** `collect.py` matches emails by subject, but the fallback's output doesn't carry it. Over the last 9 days it kept 216 emails and silently dropped all 77 gpt-4o-mini ones. This must be fixed before idea 1, or the fallback can't be sampled. About a 5-line fix in `collect.py`, with a test.
- **The digest's "missed slot" blind spot** is the same pattern that hid the 12-week digest outage.

## Security: the weekly review's `claude -p`

**Before (as it runs now on the server):**
```
claude -p "<weekly_prompt.md>" --allowedTools Read Write Edit "Bash(python3:*)" "Bash(curl:*)" "Bash(cp:*)" "Bash(ls:*)" "Bash(date:*)"
```

The review reads untrusted email text: subjects and previews inside the teacher prompts. Problems with this setup:
- `Bash(python3:*)` and `Bash(curl:*)` let it run **any** Python and reach **any** website. A cleverly written email could in principle get it to send `~/.n8n-local-key` somewhere.
- There's no `--tools` list, so every built-in tool is loaded. Any tool not on the allow list would be blocked rather than run, because nobody is there to approve it.
- There's no `--strict-mcp-config`.

**Already fine:**
- The server has **no MCP servers and no claude.ai connectors** (I checked with `claude mcp list`), so the mail and file connectors can't be reached from there.
- The permission mode is `default`, not `auto`, because the server has no settings file.
- The Mac's old launchd job is retired.

**Proposed after:**
```
claude -p "<prompt>" --tools Read,Write,Edit,Bash --strict-mcp-config --permission-mode default \
  --allowedTools "Read(./**)" "Write(./data/**)" "Write(./out/**)" "Edit(./data/**)" "Edit(./out/**)" \
  "Bash(python3 fetch_corrections.py)" "Bash(python3 collect.py)" "Bash(python3 review.py:*)" \
  "Bash(python3 spot_check.py:*)" "Bash(python3 apply_live.py:*)"
```
- There is no `--mcp-config`. It doesn't need Notion: corrections come through the n8n webhook.
- The live workflow update, which is the only use of `curl`, moves into a small `apply_live.py` script. That script backs up, PUTs, verifies and restores if a check fails, exactly as README step 6 does now. The model then can't build its own network commands.

## What was built (2 Nov, after "Build all A–E")

All in `~/n8n-cloud`, branch **`phase/2-savvy-ideas`**, one commit per piece. Nothing is pushed. The weekly-review code is now tracked there, in `server/weekly-review/` (commit `3ea7677`). Its private data folders are git-ignored.

| Piece | Commit | What it does | How it was tested |
|---|---|---|---|
| A | `d273c1f` | `collect.py` keeps the fallback decisions | New unit test. On live data: 216 → 280 emails collected; fallback pairings spot-checked by hand. |
| B | `2b86d38` | `spot_check.py`: up to 40 a week, Opus labels them blind, the labels feed the existing gate as gold (your corrections win), and disagreements go to the Notion `Audit` column. The launcher runs it before the Claude session. | Unit tests: sampling, parsing, fencing. **Live staged run on the server:** 40/40 labelled in 35 s, about 13k tokens in and 2.5k out (Pro plan), 18 disagreements, 18 flagged in Notion. A re-collect then counted the 40 labels as gold. |
| B (Notion) | none: live config | New `Audit` text column on the Email Metrics Log. "Needs review" (Email Hub) and "Review" now show rows with Confidence < 80 **or** an Audit note, if not yet corrected. New n8n workflow **"Audit Flag"** (webhook, its own secret `~/.n8n-audit-secret`, validates the page id). | Wrong secret → 403. Bad page id → stopped before Notion. Real row → Audit written, then cleared. |
| C | `0217ae4` | Lockdown of the weekly `claude -p` (below) + `apply_live.py`, which replaces curl for the one live change the review may make | Unit test of the verify checks. **Red-team probe on the server** with the new flags: curl, free-form python, reading `~/.n8n-local-key` or the login token, `~` tricks and writing outside the folder were all **denied**. The review commands and writing to `./out` were allowed. |
| D | `0ee927e` | `check_classifier.py` runs from cron every 15 min and fails healthcheck `classifier` if the last 2 classifier runs errored | Self-test of the rule. Ran once on the server (exit 0). |
| E | `1a844fa` | The digest's System health reads healthchecks.io: every job shows OK / late / DOWN with its last run. If healthchecks is unreachable, it falls back to n8n counts and now flags jobs that didn't run. | Offline test with fake data: OK, late/down and fallback. Live since 2 Nov (see below). |

The live digest was backed up to `~/Desktop/Claude/selfhosted-ready/pre-health-digest-backup-*.json` before any of this. The live Email Classifier was **not touched**.

## Security after (live since 2 Nov)
```
claude -p "<prompt>" --tools "Read,Write,Edit,Bash" --strict-mcp-config --permission-mode default \
  --allowedTools "Read(./**)" "Edit(./data/**)" "Edit(./out/**)" \
  "Bash(python3 review.py:*)" "Bash(python3 apply_live.py)" "Bash(ls:*)" "Bash(date:*)"
```
- The spot-check's Opus call has **no tools at all** and no MCP.
- The review session still reads untrusted email text (inside the teacher prompts). What keeps that safe is that it can write only its teacher replies and its summary, and `apply_live.py` rebuilds the live code from the selected rulebook itself (review fix, 2 Nov).

## Left for you to approve
1. ~~Swap A–D into the live weekly review~~ **Done 2 Nov 15:47**, with your OK. Old code and crontab are backed up on the server (`~/weekly-review-code-backup-20261102-1546.tar.gz`, `~/crontab.before-savvy`). The 40 labels from the staged run were carried over. On the server, the tests pass and a dry `collect` counted 281 emails, 40 of them gold audit labels. The new crontab includes the classifier check. First full run: **Sun 8 Nov 18:00**.
2. ~~Healthchecks keys~~ **Done 2 Nov.** You saved both as files, never in chat. `hc_setup.py` created the `classifier` check, installed its ping URL on the server and deleted `~/.hc-api-key`. The read-only key stays in `~/.hc-ro-key`, for E and for Jarvis later. You may revoke the read-write key on the website.
3. ~~E live~~ **Done 2 Nov 16:12**, with your OK. The staged copy returned the email without sending it; its health section listed all 6 checks with OK and last-ping times. It was then swapped into the live Monday Digest, verified (active, one new node, no other node changed) and the staged copy deleted. Backup: `selfhosted-ready/pre-health-apply-digest-20261102-1612.json`. The read-only key is held in the n8n credential "Healthchecks Read-only". First real send: **Mon 9 Nov**.
4. **The fallback finding:** 17 of 26 fallback decisions disagreed with Opus. Reviewed together 2 Nov: about half are clear fallback mistakes (worst: a failed-CI email ignored, a prize-draw promotion filed as a job rejection), and the rest are judgment calls. Sender rules were considered and dropped: several marketplaces and travel sites send review requests from the same address as orders, bids and booking messages. **Decision: no rule changes; let the weekly learning use the 40 audit labels (18 of them disagreements)** (first run Sun 8 Nov). One label to double-check: a crypto-exchange account notice where Opus and the summary disagree.
5. **Merge/push** `phase/2-savvy-ideas` when the reviewing session is happy.

## What I recommended building (before approval)

| # | What | Where | Live change? |
|---|---|---|---|
| A | Fix `collect.py` dropping fallback decisions | `~/weekly-review` | No |
| B | Idea 1: `spot_check.py` samples up to 40 a week, Opus labels them blind, and the labels flow into the existing review as gold. Disagreements are listed in the weekly summary and flagged for you in Notion. | `~/weekly-review` (+ README) | Weekly-review scripts only: backed up, tested with a fake teacher, then swapped in |
| C | Security lockdown of the weekly `claude -p` (above) + `apply_live.py` | `~/weekly-review/run_weekly.sh` | Same: backed up, tested, swapped |
| D | Idea 2: the `classifier` healthcheck (cron script) | `~/n8n-cloud/server/jobs` | A new cron line on the server |
| E | Idea 2: the digest health block reads healthchecks (read-only key) | Monday Digest workflow | **Yes.** Backup, staged copy, test-send, then swap with your OK |

**Decisions needed from you:**
1. Which of A–E to build. My suggestion is all five, in that order.
2. For idea 1's Notion flag: may the weekly run mark disagreements on their Metrics Log row? That means a new **"Audit"** text property plus a Review-view filter. It is the first time the weekly review would write to Notion; today it is forbidden to.
3. For E: create a **read-only** healthchecks API key and save it to a file. I'll tell you where; same routine as before.

**Repositories:**
- `~/weekly-review` isn't a git repo. Its README says it holds private email text and must never be committed. So the script changes will live in `~/n8n-cloud/server/` (branch `phase/2-savvy-ideas`, one commit per idea), which already deploys them to the server, and private data stays out.
- This report and anything shared go in `~/Ai Workload` on branch `savvy-ideas-eval`.
