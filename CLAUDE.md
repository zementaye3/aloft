# CLAUDE.md — Working rules for this project

This file documents how Claude and the project owner work together.
Read this first in any new session before making changes.

**If any section below is marked "Not yet filled in," do not guess, do not
invent a plausible-sounding value, and do not proceed as if it were
answered. Ask the project owner directly, get the real answer, then update
this file with it (in this same file, under the right section) so future
sessions don't have to ask again.** A short back-and-forth at the start of
a session is normal and expected — it's cheaper than working from a wrong
assumption for an hour.

---

## Project basics

- **Repo:** https://github.com/zementaye/aloft (git-based project)
- **Structure:** monorepo — `backend/` (FastAPI, Python 3.12) and
  `frontend/` (static HTML/CSS/vanilla JS pages, no build step, no
  framework) at the repo root.
- **Hosting/deployment:** live on Render (free tier), as of session 3:
  - Backend (Web Service, root dir `backend/`): https://aloft-backend-6rfm.onrender.com
  - Frontend (Static Site, root dir `frontend/`): https://aloft-frontend-b64d.onrender.com
  - Both spin down after 15 min of inactivity on the free plan and take
    ~30-60s to wake up on the next request — that's normal, not broken.
  - MongoDB Atlas (free M0) and Redis Cloud (free) are the databases,
    for both local dev and Render — see "Other conventions" below.
- **Stack:**
  - Backend: FastAPI + Motor (MongoDB async driver) + Redis, JWT auth,
    Groq (LLM narration text), ElevenLabs (TTS), AviationStack +
    AeroDataBox (flight/airport lookups). See `backend/requirements.txt`
    for the full list.
  - Frontend: plain HTML files (one per screen, originally exported as
    "Coded UI" static mockups) with inline `<style>` blocks per page and
    a small shared `frontend/js/` folder (`config.js`, `api-client.js`)
    added by Claude to talk to the backend. No bundler, no npm install
    needed to run it.
- **Data persistence:** MongoDB is the system of record (users, flight
  sessions, POIs, stories, favorites, journal entries, etc. — see
  `backend/app/models/`). Redis is used for rate limiting, refresh-token
  revocation, and background job queues — it's optional infra (the app
  degrades gracefully without it) but the content-generation worker won't
  run without it. Locally both run via Docker (`backend/docker-compose.yml`
  already has services for `mongodb` and `redis`).
- **Costs money:** Render hosting, and if/when the project moves past free
  tiers — MongoDB Atlas, Redis hosting, Resend/SendGrid email, R2 storage,
  OpenSky paid tier. Nothing paid has been set up yet. Always flag before
  proposing a paid upgrade, a new paid service, or a plan change.

## Shell and environment

- **Shell:** Windows **PowerShell** (not bash, not WSL). Every command
  Claude gives for local execution must be real PowerShell syntax —
  `Expand-Archive` / `Copy-Item` / `Remove-Item`, not `unzip` / `cp` / `rm`.
- **Downloads folder:** not yet confirmed as a specific path — ask if a
  delivery script needs to reference it directly. So far, delivery scripts
  are written to be run *from inside the project folder* (the person `cd`s
  there first), which avoids needing to know the exact path.
- If Claude is ever unsure which shell is in play (e.g. the person
  switches machines), ask rather than guessing from the last-known answer.

## Delivery and push workflow

Confirmed: **git-based**, zip delivery. When Claude makes code changes in
a session, the deliverable is a zip containing only the changed/new files
(preserving folder structure relative to the repo root), plus **one
PowerShell script, in the chat response**, that does everything from
unzipping through pushing to GitHub in a single copy-pasteable block.

**Each delivery gets a unique, descriptive internal folder/file name** —
never reuse the same name across deliverables in a session or across
sessions.

**All commands for a given delivery go in a single PowerShell block, start
to finish**: extract the zip, copy files into place (explicit per-file
copy commands, never a blind folder overwrite), `git status` / `git diff`
for review, stage, commit with a specific message, push. The project owner
reviews the diff after the block runs, not before — don't pause mid-flow
waiting for approval unless asked.

Rules for this flow:
- List exact copy commands for each changed file individually.
- Write a short, specific commit message describing the actual change.
- Note any harmless recurring warnings (e.g. CRLF/LF conversion notices)
  so they aren't mistaken for real errors.
- If a command opens a pager or interactive prompt, say how to exit it.
- If a download or path doesn't match what Claude expected, ask the
  project owner to list recent downloads to confirm the real name —
  don't guess a suffix like `(1)`.
- `backend/.env` is git-ignored (see root `.gitignore`) — it is delivered
  in the zip for the person to copy into place locally, but it will never
  show up in `git status` / get committed. That's expected, not a bug —
  don't "fix" it by un-ignoring it.

## History tracking

Confirmed: **yes, per-session summary.** Kept at `docs/SESSION_LOG.md`.
Written by Claude, not generated from git history — a short dated entry
per session where real work happened (what was built, decided, or fixed).
A few lines per session, not a transcript.

A plain commit-log file (regenerated from real git history) was **not**
requested — skip it unless asked for later.

**When asked to "remember" a rule or convention going forward, it goes in
this file (CLAUDE.md), not in the session log.** This file is the
rulebook; `docs/SESSION_LOG.md` is the history.

## Other conventions established so far

- **API base URL** lives in one place: `frontend/js/config.js`
  (`window.ALOFT_API_BASE`). When the backend moves to Render, only that
  one line changes — no other frontend file should hardcode the backend
  URL.
- **Auth token storage:** `frontend/js/api-client.js` (`AloftAuth`) keeps
  `aloft_access_token` / `aloft_refresh_token` in `localStorage` and
  auto-refreshes once on a 401 before giving up. Any page that calls a
  protected endpoint should use `AloftApi.*` / `aloftApiRequest()` from
  that file rather than raw `fetch()`, so auth handling stays consistent.
- **"Remember me" checkbox on Log In** is currently cosmetic — tokens are
  always persisted to `localStorage` regardless of whether it's checked.
  Flagged as a known simplification; revisit if the project owner wants a
  real session-only mode (would mean switching to `sessionStorage` when
  unchecked).
- **Frontend pages keep their original filenames** from the "Coded UI"
  export (e.g. `Log_In__Light_.html`, underscores and all) rather than
  being renamed to something more URL-friendly. Not revisited yet — flag
  to the project owner if this becomes annoying to work with.
- **No email provider configured yet** (no `RESEND_API_KEY` /
  `SENDGRID_API_KEY` in `backend/.env`). The `/v1/auth/forgot-password`
  endpoint still responds successfully either way (it always returns a
  generic "if this email exists..." message, by design, to prevent
  account enumeration) — but in development, when no email provider is
  configured, the backend **logs the actual reset link with its token to
  the server console** instead of emailing it. That's how to test the
  reset-password flow locally right now. Ask the project owner which
  provider (Resend or SendGrid) they want before wiring real email
  sending.
- **Database/cache are hosted, not local Docker.** The project owner set
  up a free MongoDB Atlas cluster (M0) and a free Redis Cloud instance
  instead of using `backend/docker-compose.yml`'s local `mongodb`/`redis`
  services. `backend/.env`'s `MONGODB_URI` and `REDIS_URL` point at those
  hosted instances for local runs too — there's no need to
  `docker compose up` Mongo/Redis anymore. The `docker-compose.yml` file
  is left in the repo in case that's ever useful again, but it's not part
  of the current workflow.
- **Deploying to Render — live.** Backend:
  https://aloft-backend-6rfm.onrender.com (Web Service, root dir
  `backend/`, free plan). Frontend:
  https://aloft-frontend-b64d.onrender.com (Static Site, root dir
  `frontend/`, free plan). `frontend/js/config.js`'s `ALOFT_API_BASE`
  points at the live backend URL now (not `localhost`). The backend's
  Render dashboard env vars `CORS_ALLOWED_ORIGINS` and `FRONTEND_BASE_URL`
  must be set to the live frontend URL above — set directly in the Render
  dashboard, never in `render.yaml` (no secrets/URLs get committed there).
- **No R2 configured yet** — `ENVIRONMENT=production` makes the app
  **refuse to start at all** without R2 (`R2_ACCOUNT_ID`,
  `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`) and
  without an email provider (`RESEND_API_KEY` or `SENDGRID_API_KEY`) —
  see `backend/app/core/config_validation.py`. This is a hard boot-time
  validation error, not a warning. Until R2 + an email provider are set
  up, **the Render backend's `ENVIRONMENT` must be `staging`, not
  `production`** (`staging` skips those two production-only checks;
  everything else about the deploy is unaffected). Revisit this the
  moment audio generation or real password-reset emails are being
  wired — don't just flip it to `production` without adding R2 + email
  config first, or the service will 502 on every boot again.
- **`gunicorn.conf.py` binds to `$PORT`, not a hardcoded port.** Render
  assigns the port dynamically; a hardcoded `bind` value causes every
  request to 502 even though the process is running fine. If a health
  check or a fresh Render service ever 502s with no obvious app error in
  the logs, this is the first thing to check.
- **Local frontend serving:** the frontend is meant to run on
  **`http://localhost:5500`** specifically — this isn't arbitrary, the
  backend's password-reset email link is built as
  `{FRONTEND_BASE_URL}/reset-password.html?token=...` and
  `backend/.env` / `backend/.env.example` both set
  `FRONTEND_BASE_URL=http://localhost:5500`. If the frontend is ever
  served on a different port, update `FRONTEND_BASE_URL` and
  `CORS_ALLOWED_ORIGINS` in `backend/.env` to match.
