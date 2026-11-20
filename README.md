# Angular + Python starter

Two independent projects:

- `frontend`: Angular generated with the official CLI, routing, SCSS, runtime API
  configuration, Microsoft Entra sign-in, and PDF upload/status/download.
- `backend`: FastAPI with Uvicorn, token validation, Azure Foundry agent-based PDF
  redaction, and private Azure Blob Storage. The Azure Language implementation is
  retained as an explicit rollback option.

No Azure infrastructure provisioning or deployment automation is included. A
one-time setup command configures the customer's existing Foundry prompt agent. Per-job files,
execution containers and response records are temporary.

## Workflow

1. The user signs in with Microsoft Entra and uploads a PDF (English, up to 10 MB).
2. The backend validates the access token, stores the original and an owner-scoped
   job record in private Blob Storage, and returns HTTP 202 without waiting for the model.
3. Background processing uploads the original through Foundry's project Files API
   with `purpose=assistants`. It supplies that file ID as the `analysis_file_id`
   structured input to the retained native agent's published version.
4. Native Code Interpreter mounts the uploaded file into an automatic execution
   container for the request. The backend does not reuse conversations, previous
   responses, or container IDs between jobs. No agent or toolbox is created/deleted
   per upload. The agent uses PyMuPDF and the configured redaction prompt; subject
   placeholders are inferred from the original header.
5. The backend downloads the generated PDF and previews through authenticated
   container APIs. It checks PDF structure, unchanged page count/geometry, cleared
   metadata, and changed but nonempty extractable text. Before accepting outputs,
   it retrieves the original from the native execution container and confirms its
   SHA-256 matches the uploaded bytes. It stores the PDF, previews, and generation
   details under the owner's private job prefix.
6. Existing status and download endpoints continue to work. A successful job means
   **generated, not disclosure-approved**: the API and UI explicitly warn that
   independent content/layout review is required. The additional document-specific
   verification used for the example PDFs is not integrated into this workflow.
7. Temporary Foundry responses, uploaded Files API files and execution containers
   are deleted. The retained native agent is never placed on the cleanup list.
   Cleanup failures are persisted, surfaced as warnings, and retried on status requests.
   Temporary local files are removed when processing exits.

The default is `REDACTION_PROVIDER=foundry`. There is no automatic fallback to
Azure Language when Foundry fails, and original pages are never substituted.
The model, not a deterministic local rules engine, implements the redaction prompt.
Review every output before sharing it.

### Configure the existing agent (once)

The default follows Microsoft's [Code Interpreter uploaded-file example](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/code-interpreter)
and [runtime file-ID binding example](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/structured-inputs).
The agent's tool configuration is:

```json
{
  "type": "code_interpreter",
  "container": {
    "type": "auto",
    "file_ids": ["{{analysis_file_id}}"]
  }
}
```

From `backend`, configure the agent the customer already deployed:

```powershell
& .\.venv\Scripts\python.exe -m app.foundry_native `
  --project-endpoint "https://alexz1008-foundry-hrs1.services.ai.azure.com/api/projects/proj-default" `
  --agent sac-redaction
```

Use the customer's actual project endpoint and agent name. There is no source-agent
or copied-agent step. Setup reads the existing agent and publishes a new version
under the same name with native Code Interpreter's runtime file binding and the
required `analysis_file_id` structured input. Instructions, model, other definition
settings, unrelated structured inputs, and existing native container settings are
preserved. Existing static file bindings are replaced by the per-request file ID.
Previous versions remain available; an already-configured agent needs no new version.
Setup fails if the agent does not exist rather than creating a different agent.

The backend requires native Code Interpreter as the only tool for this workflow.
If the existing agent has MCP or other tools, setup refuses to remove them silently.
Only if replacing those tools is intended, rerun with `--replace-tools`; the change
applies to the new version, not previous versions.
Each job pins the configured agent version selected when processing starts.

Set `AZURE_FOUNDRY_EXECUTION_MODE=native` (default) and
`AZURE_FOUNDRY_AGENT_NAME=<customer-existing-agent-name>`, then restart the backend.
The local example uses `sac-redaction`. No second retained agent is required.
The native path instructs the agent to fail if PyMuPDF is unavailable. Unlike the
legacy MCP path, it does not run a separate backend-controlled Python preflight;
mounted input bytes are verified after execution, before accepting generated files.

Workers use a renewable Blob lease to prevent duplicate execution. Job/resource
checkpoints survive backend restarts; status polling schedules queued jobs and cleanup.
An interrupted, already-started job is failed and cleaned up rather than silently
running the model again. Upload the original again to retry it.
This starter uses in-process background tasks, not a durable queue: keep polling
until completion, configure lifecycle/reconciliation for abandoned jobs, and use
a durable worker/queue for production scale. Foundry containers are not a per-user
security boundary within a project; use a trusted project with restricted membership
and an appropriate isolation/retention design for sensitive or multi-user deployments.

### Toolbox workflow rollback

The previous working implementation is retained in `backend/foundry_poc.py`.
Set `AZURE_FOUNDRY_EXECUTION_MODE=toolbox` and
`AZURE_FOUNDRY_AGENT_NAME=sac-redaction`, then restart the backend to use it.
It creates a temporary agent/toolbox per job, uploads directly into an explicitly
created container, and checks file hash/PyMuPDF with an MCP execution probe.
There is no automatic fallback between native and toolbox execution.
Jobs persist their execution mode, agent name and resource IDs so queued work and
cleanup do not switch protocols just because configuration changes.

### Azure Language rollback

Set `REDACTION_PROVIDER=language` and restart the backend to use the retained
implementation in `backend/app/redaction.py`. Configure its Language settings and
permissions below. Existing jobs must be accessed using the provider that created
them; a provider mismatch returns HTTP 409. Complete active jobs before switching.

With this provider, the backend splits the PDF at page boundaries, targeting 90,000 estimated UTF-16
   units per part, below Azure's 125,000 text-element document limit. Azure Language
   redacts each part using its asynchronous native-document PII API. If Azure's
   count exceeds the estimate (including OCR), the affected part is halved and
   retried. A single oversized page fails explicitly.
Status polling advances persisted part jobs sequentially. Once every part succeeds,
   the backend merges only redacted pages in their original order, checks page counts,
   and verifies ownership before serving one PDF. No original pages are substituted
   on failure. Reassembly preserves pages, not necessarily bookmarks, forms, signatures,
   or document-level navigation.

For this rollback provider, the original must be uploaded **before** redaction because the native-document API
requires Blob Storage source/target locations. Managed identity is recommended.
For key-based installations, the backend creates one-hour HTTPS SAS grants for Azure
Language; neither SAS tokens nor service keys reach the browser.
All available PII categories are requested with character masking and service logging
opt-out. Review every result: automated detection can miss PII, and document warnings
can indicate incomplete processing. This is not a guarantee of anonymization.

## Standalone Foundry agent POC

`backend/foundry_poc.py` remains available as a standalone command-line runner.
The default backend reuses its transport, downloads, cleanup, and structural PDF
checks; native binding is in `backend/app/foundry_native.py`. The CLI uses your `az login` identity; the
backend uses `DefaultAzureCredential` (managed identity in Azure, CLI locally).

```powershell
& .\backend\.venv\Scripts\python.exe backend\foundry_poc.py `
  --project-endpoint "https://alexz1008-foundry-hrs1.services.ai.azure.com/api/projects/proj-default" `
  --agent sac-redaction `
  --input "examples\Chronos - For Microsoft.pdf" `
  --output-dir "examples\Foundry-POC"
```

The script creates a dedicated Code Interpreter container and uploads the PDF
directly through the container Files API. A job-specific toolbox references that
container ID, avoiding static shared file lists and the resource-level upload
ownership/GUID issue. Before invoking the model, a sandbox execution must confirm
that the uploaded original has the same SHA-256 hash and that PyMuPDF is available.
The script copies the source agent's latest published prompt and model into a
temporary agent with only the job's execution tool; web search is not enabled.
The shared source agent and toolbox are never modified.

Generated PDF/PNG file citations are downloaded through authenticated container
APIs, not public links. If a response omits file citations, the script explicitly
checks only the job's containers for the expected output PDF and preview PNGs.
Existing PDFs are not overwritten. The script checks PDF
signatures, page counts/geometry, cleared metadata, and changed extractable text.
These checks do **not** certify that every sensitive item was removed or that all
protected content was retained. Outputs are explicitly marked **unreviewed** in
`foundry-poc-result.json` and require independent redaction review.

Created agent responses, temporary agents/toolboxes, and containers (including
uploaded originals and remote outputs) are deleted in `finally`. Cleanup failures
are reported and result in a nonzero exit code. This is a bounded POC, not durable
job orchestration: an interrupted process can leave resources requiring manual
cleanup. Foundry/model/container charges apply. Access requires permission to
create/use/delete these project resources. Foundry containers are not a per-user
security boundary within a project; do not share container IDs or deploy this
POC as a multi-user service without an appropriate isolation/retention design.

## Azure resources and app settings

Create these resources manually:

- An Azure Foundry project with the customer's existing prompt agent, model deployment
  and Code Interpreter access. Run the one-time setup above with permission to
  create a new version of that existing agent. The backend needs permission to read/run it and
  upload/read/delete files, use containers and delete response/container resources.
  Agent/toolbox creation/deletion permissions are needed only for the legacy toolbox rollback.
- Only for rollback: a **single-service Azure Language resource** supporting
  native-document PII with the GA `2026-05-01` analyze-documents API.
- An Azure Storage account with two **private**, pre-created containers, for example
  `originals` and `redacted`. Disable anonymous blob access. Shared-key access can
  remain disabled when using managed identity.
- Two single-tenant Microsoft Entra application registrations, as described below.

Set these in the backend App Service's **Settings > Environment variables / App settings**:

| Setting | Value |
| --- | --- |
| `ENTRA_TENANT_ID` | Directory tenant ID |
| `ENTRA_API_CLIENT_ID` | Backend/API registration's Application (client) ID |
| `REDACTION_PROVIDER` | `foundry` (default), or `language` for explicit rollback |
| `AZURE_FOUNDRY_PROJECT_ENDPOINT` | Foundry project HTTPS endpoint ending in `/api/projects/<project>` |
| `AZURE_FOUNDRY_AGENT_NAME` | Customer's existing agent name; defaults to `sac-redaction` |
| `AZURE_FOUNDRY_EXECUTION_MODE` | `native` (default); `toolbox` restores the previous POC workflow |
| `AZURE_LANGUAGE_ENDPOINT` | Rollback only: Language resource HTTPS endpoint, without an API path |
| `AZURE_LANGUAGE_KEY` | Rollback only: optional Language resource key |
| `AZURE_STORAGE_ACCOUNT_NAME` | Storage account name |
| `AZURE_STORAGE_ACCOUNT_KEY` | Optional storage access key; leave empty for managed identity |
| `AZURE_STORAGE_ORIGINALS_CONTAINER` | Original container name, e.g. `originals` |
| `AZURE_STORAGE_REDACTED_CONTAINER` | Different output container name, e.g. `redacted` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated frontend origins, e.g. `https://your-frontend-host` |

For Foundry, leave the storage key empty to use managed identity and grant the
backend **Storage Blob Data Contributor** on the two private containers plus
the required Foundry project permissions. No Language resource, Language key, or
Language-to-storage permissions are required in Foundry mode. Backend network access
to Foundry and Storage is required; RBAC does not bypass firewalls.

For the Language rollback provider with managed identity, leave **both** keys empty. Enable system-assigned identities on
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

For Language jobs, after a terminal result is persisted, temporary source parts and per-part output
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
2. Create `frontend/.local/config.json` (Git-ignored) with:

   ```json
   {
     "apiBaseUrl": "/api",
     "devAuthBypass": true
   }
   ```

3. Run `npm run start:local` in `frontend` and open `http://localhost:4200`.
   This explicit local configuration serves the ignored file as `/config.json`.
   Normal `npm start` and production builds use the tracked
   `frontend/public/config.json`, which must keep authentication enabled.

The UI displays an authentication-disabled warning and sends no bearer token.
The backend permits bypass only for loopback peers, loopback request hosts, and
loopback browser origins (when present), and rejects it on Azure App Service.
Do not expose or tunnel these local servers. All bypass users share one fixed
development document identity; use isolated development storage and synthetic PDFs.
Azure Storage and Language access still require the configured credentials or
your Azure CLI identity and appropriate roles.

Both `backend/.env` and `frontend/.local/` are Git-ignored. Do not force-add or deploy
these local files. To restore normal authentication, set `DEV_AUTH_BYPASS=false`
(or remove it), restart the backend, and restart the frontend with `npm start`.

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
