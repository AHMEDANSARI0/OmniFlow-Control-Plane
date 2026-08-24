# OmniFlow Control Plane — Render Test Deployment

This repository contains only the versioned Control Plane HTTP API. It does **not** contain WhatsApp browser sessions, connector credentials, AI keys, database passwords, `.env` files, or SQL migration runners.

## Architecture

- Website: existing Vercel Next.js deployment
- Control Plane API: Render Python web service
- Database: existing migrated Neon PostgreSQL project
- WhatsApp connector: personal laptop during the free testing phase

The included `render.yaml` creates one free Singapore-region web service with:

- Gunicorn WSGI runtime
- one process and four request threads
- `/api/v1/health` health check
- required PostgreSQL TLS
- pinned runtime dependencies
- compile, HTTP API, and customer-auth tests during every build

## 1. Create a private GitHub repository

Create an empty private repository named `OmniFlow-Control-Plane`. Do not initialize it with a README, `.gitignore`, or license because those files are already included here.

After creating that empty private repository, run the included publisher from this extracted directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\publish_to_github.ps1"
```

The script performs the equivalent checked Git operations below and can safely resume a retry:

```powershell
git init
git branch -M main
git add -A
git commit -m "Deploy OmniFlow Control Plane API v1"
git remote add origin https://github.com/AHMEDANSARI0/OmniFlow-Control-Plane.git
git push -u origin main
```

Authenticate to GitHub locally through its browser or credential manager. Never send credentials or personal access tokens through chat.

## 2. Create the Render Blueprint

1. Sign in at <https://dashboard.render.com> with GitHub.
2. Select **New > Blueprint**.
3. Choose the private `OmniFlow-Control-Plane` repository.
4. Render reads `render.yaml` and asks for the secret environment values listed below.
5. Confirm the service plan remains **Free** and region remains **Singapore**.

## 3. Configure secrets in Render

Copy the database fields from Neon **Connect > Direct connection**. Enter values only in Render's Environment/Blueprint form, never in Git or chat.

| Render key | Value source |
|---|---|
| `DB_HOST` | Neon direct host, such as `ep-...neon.tech` |
| `DB_PORT` | Already set to `5432` |
| `DB_NAME` | Neon database name, usually `neondb` |
| `DB_USER` | Neon role/user |
| `DB_PASSWORD` | Neon role password |
| `PGSSLMODE` | Already fixed to `require` |
| `OMNIFLOW_ADMIN_API_KEY` | A new random admin key of at least 32 characters |

Generate the administrator key locally in PowerShell:

```powershell
$bytes = New-Object byte[] 48
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$adminKey = [Convert]::ToBase64String($bytes)
Set-Clipboard -Value $adminKey
$adminKey = $null
$bytes = $null
Write-Host "Admin key copied to clipboard; paste it only into Render."
```

Do not reuse a GitHub, Neon, Supabase, or AI-provider password as the admin key.

## 4. Verify Render deployment

When the deployment says **Live**, open:

```text
https://YOUR-RENDER-SERVICE.onrender.com/api/v1/health
```

Expected response:

```json
{"api_version":"v1","service":"omniflow-control-plane","status":"ok"}
```

The free Render service sleeps after inactivity. For testing, open the health URL and wait for it to return `status: ok` before testing customer login.

## 5. Connect Vercel to the API

In the existing website project on Vercel, add this server-only environment variable to Production, Preview, and Development as required:

```text
OMNIFLOW_CONTROL_PLANE_URL=https://YOUR-RENDER-SERVICE.onrender.com
```

Use only the HTTPS origin. Do not append `/api/v1`, and do not expose it with a `NEXT_PUBLIC_` prefix. Trigger a new Vercel deployment after saving it.

`OMNIFLOW_ADMIN_API_KEY`, Neon credentials, and database passwords must never be configured in the website project or browser environment.

## Free-test limitations

- Render Free can sleep after inactivity, so the first wake may take about a minute.
- The personal laptop must remain on while testing the WhatsApp Web connector.
- This topology is for controlled testing, not the final always-on production connector deployment.
- Migrations `001`–`006` are already present in Neon and must not be reapplied.
