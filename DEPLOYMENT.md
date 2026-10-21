# Quorum deployment

## Vercel frontend

Production: https://quorum-terminal.vercel.app

The Vercel project is `quorum-terminal` in `siddhant-swans-projects`. Its source root is `terminal/`. Redeploy from that directory with `npx vercel@65.0.0 deploy --prod --scope siddhant-swans-projects`.

The frontend is deployed. The persistent backend is prepared but has not yet been provisioned or connected. Until connected, the website uses its existing simulated feed and backend-dependent features are unavailable.

After the backend is healthy, add `VITE_API_BASE=https://BACKEND-HOST/api` to the Vercel project's Production environment and redeploy. This is a public URL compiled into browser JavaScript; never put credentials or LLM keys in `VITE_` settings. The frontend uses hash routes, so no SPA rewrite is needed.

## Free backend

`render.yaml` defines one Docker web service on Render's **free** plan, with no paid disk. The account owner requires zero-cost hosting. Do not upgrade the plan, attach a paid disk, or provision any paid resource.

The free backend sleeps when idle and can take time to wake. Its filesystem is ephemeral: uploaded reports, alerts, saved agent runs and SQLite caches can be lost on redeploy or restart. Alert monitoring pauses while the service sleeps. This is a working free API deployment, not a durable or continuously running alert service. A separate free external database/object store would require additional account connections and application changes for durability.

Render needs access to a source repository containing this project, including the vendored `TradingAgents/` source. The current workspace root is not a Git repository; its nested `TradingAgents/.git` belongs to upstream. Do not publish it as an empty Git submodule or push this application to the upstream repository. Use a separate private application repository with the actual source files.

Configure these variables on the backend:

| Variable | Value |
| --- | --- |
| `QUORUM_ALLOWED_ORIGINS` | `https://quorum-terminal.vercel.app` (comma-separated exact origins if others are needed) |
| `QUORUM_DATA_DIR` | `/tmp/quorum` |
| `SEC_USER_AGENT` | Application name and the operator's contact email, required for SEC discovery |

Database files, attached PDFs, discovered report indexes, saved agent runs and alert history use ephemeral storage under `/tmp/quorum`. Agent caches, results and memory also use `/tmp/agents`. Keep a single service instance and one Uvicorn worker: the alert monitor and in-process agent jobs assume one process.

The image installs the local TradingAgents package. It excludes optional PyTorch/Kronos dependencies and model weights; Kronos forecasts will report unavailable on this image. Its Docker build has not been executed locally because the Docker daemon is stopped. Backend tests and dependency resolution are checked separately.

No local `.env`, SQLite data, uploaded PDFs, private agent history or caches should be sent to either host. Ignore files exclude them. This deployment starts with a fresh shared workspace. Existing API routes do not isolate data by user; keep LLM keys unset until access controls are chosen for any public paid agent runs. Cloud credentials should be set only in the backend host's secret environment settings.

## Verify before calling the full application live

1. Build the backend image and check `GET /api/health` on its HTTPS URL.
2. Confirm the Vercel origin succeeds in a CORS preflight.
3. Set `VITE_API_BASE` in Vercel and redeploy.
4. Verify real quotes, automatic annual reports, and PDF upload/open from the hosted frontend.
5. Confirm the free-tier sleep and ephemeral-storage limitations are disclosed; do not claim durable alerts or uploads across redeploys.
