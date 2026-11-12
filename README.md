# Angular + Python starter

Two independent projects:

- `frontend`: Angular generated with the official CLI, routing, SCSS, runtime API
  configuration, and a backend connection check.
- `backend`: FastAPI with Uvicorn, OpenAPI at `/docs`, and `GET /api/health`.

No Azure provisioning or deployment automation is included. No redaction,
authentication, or storage is implemented.

## Prerequisites

Node.js 24 LTS (24.15+), npm, and Python 3.13.

Portable runtimes were downloaded for this machine because its system installations
were outdated. In each new PowerShell terminal, use:

```powershell
$tools = Join-Path $HOME '.cache\ai-redaction-tools'
$env:PATH = "$tools\node-v24.22.3-win-x64;$env:PATH"
$python = "$tools\python\cpython-3.13-windows-x86_64-none\python.exe"
```

With your own supported Python installation, use `$python = 'python'` instead.
No system PATH changes are required.

## Backend

From the project root:

```powershell
& $python -m venv backend\.venv
& .\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
Set-Location backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

API: `http://127.0.0.1:8000/api/health`

Interactive documentation: `http://127.0.0.1:8000/docs`

The backend reads `CORS_ALLOWED_ORIGINS` as a comma-separated environment variable
and defaults to `http://localhost:4200`. `.env.example` documents the setting;
`.env` files are not loaded automatically. Extend the allowed CORS methods when
adding write endpoints.

## Frontend

In another terminal, with Node 24 on its process PATH:

```powershell
Set-Location frontend
npm ci
npm start
```

Open `http://localhost:4200`. The development server proxies `/api/*` to
`http://127.0.0.1:8000`.

`frontend/public/config.json` defaults to `{ "apiBaseUrl": "/api" }`.
For separate hosting, set the built artifact's `config.json` to
`{ "apiBaseUrl": "https://<backend-host>/api" }` and configure the backend's
CORS allowlist for the frontend origin. Runtime configuration is public and must
never contain secrets.

## Tests and build

From the project root:

```powershell
& .\backend\.venv\Scripts\python.exe -m pytest backend
Set-Location frontend
npm test -- --watch=false
npm run build
```

Production frontend output: `frontend/dist/frontend/browser`.
Frontend dependencies are locked in `package-lock.json`; Python dependencies use
bounded ranges in `requirements.txt` and `requirements-dev.txt`.
