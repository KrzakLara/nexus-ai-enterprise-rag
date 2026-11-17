# Demo video tooling

Reproduces [`docs/videos/nexus-ai-demo.mp4`](../../docs/videos/nexus-ai-demo.mp4) from a real browser
session against the local stack. Nothing in the video is mocked: every application frame is a
Chrome DevTools screencast frame of the running app, Keycloak and Grafana.

| File | Role |
|---|---|
| `record_demo.mjs` | Playwright drives Chromium through the demo (SSO login, upload, extraction, review, hybrid RAG, cross-tenant check, evaluation, Grafana) and stores timestamped screencast frames plus `markers.json`. A small cursor overlay is injected so viewers can follow the clicks. |
| `build_video.py` | Cuts the marked time ranges (waiting time removed with cross-fade jump cuts), adds the title card, section labels, captions and closing card, and encodes H.264 with FFmpeg. Also renders the thumbnail from a real frame. |

## Reproduce

```bash
# 1. a fresh stack (the demo uploads and approves a document, so start clean)
docker compose -p nexusdemo -f compose.yaml -f compose.oidc.yaml -f compose.observability.yaml up --build -d
curl -s localhost:8000/api/health/ready          # wait for "status":"ready"

# 2. record (≈ 4 minutes of real interaction)
cd scripts/demo_video
npm install --no-save playwright@1.57.0 && npx playwright install chromium
node record_demo.mjs ./out

# 3. edit (Pillow + an FFmpeg build with libx264: FFMPEG=..., imageio-ffmpeg or PATH)
python build_video.py ./out ../../docs/videos/nexus-ai-demo.mp4 --thumbnail ../../docs/images/nexus-ai-demo-thumbnail.png

# 4. clean up
docker compose -p nexusdemo -f compose.yaml -f compose.oidc.yaml -f compose.observability.yaml down -v
```

The accounts are the synthetic local Keycloak users from [docs/DEMO.md](../../docs/DEMO.md). The
password is filled into the masked Keycloak field (never shown in clear text); override it with
`DEMO_PASSWORD`, and the Grafana admin with `GRAFANA_ADMIN` / `GRAFANA_ADMIN_PASSWORD`. `DEMO_HOLD`
scales the deliberate pauses (default 1.6). The `out/` directory is scratch output; do not commit it.
