# Angular + Python starter

Two independent projects:

- `frontend`: Angular generated with the official CLI, routing, SCSS, runtime API
  configuration, Microsoft Entra sign-in, and PDF upload/status/download.
- `backend`: FastAPI with Uvicorn, token validation, Azure Language native PDF
  redaction, and private Azure Blob Storage.

No Azure provisioning or deployment automation is included.

## Workflow

1. The user signs in with Microsoft Entra and uploads a PDF (English, up to 10 MB).
2. The backend validates the access token and stores the original in a private container.
3. The backend splits the PDF at page boundaries, targeting 90,000 estimated UTF-16
   units per part, below Azure's 125,000 text-element document limit. Azure Language
   redacts each part using its asynchronous native-document PII API. If Azure's
   count exceeds the estimate (including OCR), the affected part is halved and
   retried. A single oversized page fails explicitly.
4. Status polling advances persisted part jobs sequentially. Once every part succeeds,
   the backend merges only redacted pages in their original order, checks page counts,
   and verifies ownership before serving one PDF. No original pages are substituted
   on failure. Reassembly preserves pages, not necessarily bookmarks, forms, signatures,
   or document-level navigation.

The original must be uploaded **before** redaction because the native-document API
requires Blob Storage source/target locations. Managed identity is recommended.
For key-based installations, the backend creates one-hour HTTPS SAS grants for Azure
Language; neither SAS tokens nor service keys reach the browser.
All available PII categories are requested with character masking and service logging
opt-out. Review every result: automated detection can miss PII, and document warnings
can indicate incomplete processing. This is not a guarantee of anonymization.

## Azure resources and app settings

Create these resources manually:

- A **single-service Azure Language resource** supporting native-document PII.
  The backend uses the GA `2026-05-01` analyze-documents API, not text extraction.
- An Azure Storage account with two **private**, pre-created containers, for example
  `originals` and `redacted`. Disable anonymous blob access. Shared-key access can
  remain disabled when using managed identity.
- Two single-tenant Microsoft Entra application registrations, as described below.

Set these in the backend App Service's **Settings > Environment variables / App settings**:

| Setting | Value |
| --- | --- |
| `ENTRA_TENANT_ID` | Directory tenant ID |
| `ENTRA_API_CLIENT_ID` | Backend/API registration's Application (client) ID |
| `AZURE_LANGUAGE_ENDPOINT` | Language resource HTTPS endpoint, without an API path |
| `AZURE_LANGUAGE_KEY` | Optional Language resource key; leave empty for managed identity |
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
| `AZURE_STORAGE_ACCOUNT_KEY` | Optional storage access key; leave empty for managed identity |
| `AZURE_STORAGE_ORIGINALS_CONTAINER` | Original container name, e.g. `originals` |
| `AZURE_STORAGE_REDACTED_CONTAINER` | Different output container name, e.g. `redacted` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated frontend origins, e.g. `https://your-frontend-host` |

For managed identity, leave **both** keys empty. Enable system-assigned identities on
the backend App Service and the regional Language resource. Grant the backend
**Storage Blob Data Contributor** on the two containers and **Cognitive Services User**
on the Language resource. Grant the Language identity **Storage Blob Data Contributor**
on both containers. Configure storage network access for both services separately;
RBAC does not bypass firewalls. Local development uses `DefaultAzureCredential`,
including your `az login` identity, which needs the backend's roles.

Alternatively, supply both keys and enable storage shared-key access for SAS signing.
Mixed key/identity configuration is rejected. Use Key Vault references for secret
app settings where possible. No secrets belong
in Angular configuration. Storage firewalls must permit both the backend and Language
service to access storage; SAS does not bypass network restrictions. This configuration
targets Azure public cloud. Resources, retention, quotas, and service charges remain
the administrator's responsibility.

After a terminal result is persisted, temporary source parts and per-part output
artifacts (including analysis JSON, which can contain PII) are deleted. Cleanup is
idempotent and retried on subsequent status requests if interrupted. Originals,
merged outputs, and job records are retained. Abandoned jobs and submissions that
fail before a record is written require storage lifecycle rules; configure suitable
retention on both containers before real use. Soft delete/versioning can retain
deleted data according to the storage account's policies.
Do not enable request-body or verbose Azure SDK logging for sensitive documents.

### Microsoft Entra registrations

1. Create a single-tenant **API** app registration. Under **Expose an API**, set its
   Application ID URI to `api://<api-client-id>` and expose a delegated scope named
   `access_as_user`. In the app manifest, set `api.requestedAccessTokenVersion` to `2`.
2. Create a separate single-tenant **SPA** registration. Under **Authentication**,
   add **Single-page application** redirect URIs `http://localhost:4200` and your
   production frontend origin (HTTPS). No SPA client secret is needed.
3. In the SPA's **API permissions**, add the API's delegated `access_as_user`
   permission and grant the consent required by your tenant.
4. Edit `frontend/public/config.json` before building (or the deployed `config.json`):

```json
{
  "apiBaseUrl": "/api",
  "auth": {
    "tenantId": "<tenant-id>",
    "clientId": "<spa-client-id>",
    "apiScope": "api://<api-client-id>/access_as_user"
  }
}
```

For separate hosting, replace `/api` with `https://<backend-host>/api`. The frontend
uses MSAL authorization-code/PKCE sign-in; API calls use delegated **access tokens**,
not ID tokens. The backend validates signature, issuer, audience, tenant, expiry,
and scope. Records are isolated by the validated tenant and user object ID, not
by any uploader/owner field supplied by the browser. Other users receive 404 even
if they know a document ID. Downloads are proxied through the authenticated API;
there is no original-file download endpoint.

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
Copy-Item backend\.env.example backend\.env
# Fill backend\.env with your settings; never commit this file.
Set-Location backend
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --reload --host 127.0.0.1 --port 8000
```

API: `http://127.0.0.1:8000/api/health`

Interactive documentation: `http://127.0.0.1:8000/docs`

The backend reads `CORS_ALLOWED_ORIGINS` as a comma-separated environment variable
and defaults to `http://localhost:4200`. `.env.example` documents all required settings.
Local `.env` loading is enabled explicitly by `--env-file .env`; Azure App Service
injects app settings directly, so omit that flag in Azure. The production startup
command, with `backend` as the deployed application root, is:

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`GET /api/health` is public and reports process health, not Azure connectivity.
All document endpoints require a bearer access token. Swagger `/docs` supports the
**Authorize** button for an API access token.

## Frontend

In another terminal, with Node 24 on its process PATH:

```powershell
Set-Location frontend
npm ci
npm start
```

Open `http://localhost:4200`. The development server proxies `/api/*` to
`http://127.0.0.1:8000`.

Populate the Entra fields in `frontend/public/config.json` as shown above. Until
configured, the UI shows a setup message and does not permit uploads. Keep the
tab open during processing; this starter has no document history UI. Refreshing
the page loses the current document selection. Existing Azure jobs continue, but
submitting later parts, merging, and cleanup require further status requests.
Progress survives backend restarts; status requests with the original document ID
resume it. A short blob lease serializes status updates across backend instances.
Keep `config.json` uncached when hosting; configure SPA route fallbacks in your host.

### Local development without Entra sign-in

Authentication remains required by default. To bypass user sign-in locally:

1. Set `DEV_AUTH_BYPASS=true` in `backend/.env` and restart the backend, keeping
   Uvicorn bound to `127.0.0.1` as shown above.
2. Temporarily replace your local `frontend/public/config.json` with:

   ```json
   {
     "apiBaseUrl": "/api",
     "devAuthBypass": true
   }
   ```

3. Run `npm start` in `frontend` and open `http://localhost:4200`.

The UI displays an authentication-disabled warning and sends no bearer token.
The backend permits bypass only for loopback peers, loopback request hosts, and
loopback browser origins (when present), and rejects it on Azure App Service.
Do not expose or tunnel these local servers. All bypass users share one fixed
development document identity; use isolated development storage and synthetic PDFs.
Azure Storage and Language access still require the configured credentials or
your Azure CLI identity and appropriate roles.

Do not deploy or commit the bypass configuration. To restore normal authentication,
set `DEV_AUTH_BYPASS=false` (or remove it), restore the frontend's Entra configuration,
and restart the backend and reload the frontend.

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

Tests use synthetic bytes, mocked Azure responses, and locally generated signing
keys; they do not upload documents or call Azure.

API endpoints:

- `POST /api/documents`: multipart field `file`; returns HTTP 202 and an opaque job ID.
- `GET /api/documents/{id}`: returns `running`, `succeeded`, or `failed`.
- `GET /api/documents/{id}/download`: authenticated PDF attachment, only after success.

Requests have a bounded multipart envelope and a hard 10,000,000-byte PDF limit.
PDF headers/extensions, readability, and encryption are checked locally; Azure may
still reject otherwise unsupported PDFs. Failed jobs never
fall back to serving originals. If an Azure job expires before the app polls it,
submit the document again; successful results already recorded by the backend
remain downloadable while retained in storage.

References: [Azure document PII](https://learn.microsoft.com/azure/ai-services/language-service/personally-identifiable-information/document-based-pii-overview)
and [GA REST API](https://learn.microsoft.com/rest/api/language/analyze-documents/analyze-documents-submit-job/analyze-documents-submit-job?view=rest-language-analyze-documents-2026-05-01).
