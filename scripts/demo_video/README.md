# Demo video tooling

Reproduces [`docs/videos/nexus-ai-demo.mp4`](../../docs/videos/nexus-ai-demo.mp4) from a real browser
session against the local stack. Nothing in the video is mocked: every application frame is a
Chrome DevTools screencast frame of the running app, Keycloak and Grafana.

| File | Role |
|---|---|
| `record_demo.mjs` | Playwright drives Chromium through the demo (SSO login, upload, extraction, review, hybrid RAG, cross-tenant check, evaluation, Grafana) and stores timestamped screencast frames plus `markers.json`. A small cursor overlay is injected so viewers can follow the clicks. |
| `make_demo_fixtures.py` | Generates the two synthetic **English** invoices in [`demo_documents/video/`](../../demo_documents/video/) with the evaluation corpus's own generator (the frozen `eval/` data is untouched). |
| `verify_demo_pipeline.mjs` | Uploads the fixtures through the real API and checks every extracted field against the fixture manifest. It also prints the answers to candidate questions, so the questions shown in the video are checked before recording. |
| `build_video.py` | Cuts the marked time ranges (waiting time removed with cross-fade jump cuts), adds the title card, section labels, captions and closing card, and encodes H.264 with FFmpeg. Also renders the thumbnail from a real frame. |

## Reproduce

```bash
# 1. a fresh stack (the demo uploads and approves a document, so start clean)
docker compose -p nexusdemo -f compose.yaml -f compose.oidc.yaml -f compose.observability.yaml up --build -d
curl -s localhost:8000/api/health/ready          # wait for "status":"ready"

# 2. (optional) regenerate the English fixtures
.venv/Scripts/python scripts/demo_video/make_demo_fixtures.py     # or .venv/bin/python

# 3. record (≈ 5 minutes of real interaction)
cd scripts/demo_video
npm install --no-save playwright@1.57.0 && npx playwright install chromium
node record_demo.mjs ./out

# 4. edit (Pillow + an FFmpeg build with libx264: FFMPEG=..., imageio-ffmpeg or PATH)
python build_video.py ./out ../../docs/videos/nexus-ai-demo.mp4 --thumbnail ../../docs/images/nexus-ai-demo-thumbnail.png

# 5. clean up
docker compose -p nexusdemo -f compose.yaml -f compose.oidc.yaml -f compose.observability.yaml down -v
```

The accounts are the synthetic local Keycloak users from [docs/DEMO.md](../../docs/DEMO.md). The
password is filled into the masked Keycloak field (never shown in clear text); override it with
`DEMO_PASSWORD`, and the Grafana admin with `GRAFANA_ADMIN` / `GRAFANA_ADMIN_PASSWORD`. `DEMO_HOLD`
scales the deliberate pauses (default 1.6). The `out/` directory is scratch output; do not commit it.

## Scenario notes (English-only video)

- Everything on screen is in English: the uploaded invoices, the typed questions, the answers and
  the captions. The questions are typed into the input rather than picked from the guided prompt
  cards, because the "New chat" view scrolls to cards 4–5, which are deliberately Croatian (they
  demonstrate cross-language search).
- Questions used: *"What is the total due on the Brightline Systems invoice?"* ([S1] is the uploaded
  invoice, quoting "Total due | 4,272.00 EUR") and *"99.9% availability and Atlas notice period"*
  (notice periods and the availability target quoted from three contracts). Without an LLM key the
  answers are **extractive**: verbatim sentences with citations.
- Known limitation, not hidden: for a "due date" question the extractive sentence selector (two
  sentences per source) prefers the invoice's "Invoice date" line, so that question is not used.
- Before the cross-tenant identifier question the page is reloaded (the session stays). That way the
  source inspector does not keep showing the previous answer's sources.
