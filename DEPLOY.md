# Deploying to Vercel

The React dashboard (`frontend/`) and the Rust API (`api/traffic.rs`, `src/`) deploy as
one Vercel project. The database is PostgreSQL on [Neon](https://neon.tech). The setup is
in the repo already: `vercel.json` (build, rewrites, headers), `.vercelignore` (what gets
uploaded) and `database/api_readonly_role.sql`. What's left are the account steps below.

```
Browser ─► Vercel ─┬─ /api/*  → Rust function (api/traffic.rs) ─┬─► Neon PostgreSQL (read-only login)
                   │                                             └─► worker.brightwaterts.com (live)
                   └─ else    → frontend/dist (React)                    │ Cloudflare Tunnel
                                                                         ▼
                                          Michael's PC: worker on 127.0.0.1:7860 ─► Neon (write login)
```

Steps 1–5 deploy the dashboard and API. Step 6 adds the live map's worker: the
30-minute sweep and live analysis.

Commands are for Windows PowerShell from the repo root, with Docker Desktop running.
Nothing here costs money: Vercel Hobby and Neon Free are enough.

## Who does what

- **Michael:** creates the accounts, handles every password and connection string, and
  sets them in Vercel. Passwords never go into Git, chat, or a file in the repo.
- **Claude:** checks the Preview deployment once it's up.

**DECISION (Michael and Daksh) before step 1:** whose accounts own the Vercel project and
the Neon database (a personal account, or a team everyone can access), and when
Production should deploy from `main`.

## 1. Create the database (Neon)

1. Sign up at https://neon.tech (free plan) and create a project:
   - **Region:** AWS US East (N. Virginia), `us-east-1`. It sits next to the API's
     Vercel region (`iad1`), which keeps queries fast.
   - **Postgres version:** 17 (the version the tests run on).
2. In the project's **Connect** dialog, find two connection strings for the owner role:
   - the **direct** one (host without `-pooler`), used below for setup and imports;
   - the **pooled** one (host contains `-pooler`), whose host you'll reuse in step 3.

   Treat both as passwords.

We create Neon directly rather than through Vercel's Storage marketplace on purpose. The
marketplace integration copies the database **owner** credentials into the Vercel
project's environment variables, so the API function would carry admin access it never
uses. This way, Vercel only ever holds the read-only login.

## 2. Create the schema, the read-only login and the sample data

Put the **direct** owner connection string (host **without** `-pooler`; keep the
`?sslmode=require&channel_binding=require` ending) in this PowerShell session only. The
prompt hides what you paste, and it isn't saved to history:

```powershell
$s = Read-Host 'Neon owner connection string (direct)' -AsSecureString; $env:DATABASE_URL = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
```

Check it without printing it:

```powershell
if ($env:DATABASE_URL -like 'postgresql://*' -and $env:DATABASE_URL -notlike '*-pooler*') { 'set (direct)' } else { 'check it' }
```

If you pasted a connection string somewhere it was visible (a terminal, a chat), reset
that role's password in Neon (Roles → Reset password) once setup is done.

Apply the schema:

```powershell
docker run --rm -v "${PWD}:/work" -w /work postgres:17 psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/schema.sql
```

Create the API's read-only login (`traffic_api`) with its grants and a generated
password. The password goes into `API_PASSWORD` for this session only and is never
shown. (Neon's dashboard can't set the first password on a role created in SQL, and Neon
rejects psql's `\password`, so the script sets it from `API_PASSWORD`.)

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($b); $env:API_PASSWORD = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
docker run --rm -e API_PASSWORD -v "${PWD}:/work" -w /work postgres:17 psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/api_readonly_role.sql
```

It should end with `traffic_api is ready, with the password from API_PASSWORD.`

Add the live traffic map's tables (cameras and 30-minute sweeps), and their grants for
`traffic_api` and `traffic_writer`:

```powershell
docker run --rm -v "${PWD}:/work" -w /work postgres:17 psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/map_schema.sql
```

(Or paste `database/map_schema.sql` into Neon's **SQL Editor**, with the database dropdown
set to `traffic`, not `neondb`, and click Run. Check it with
`SELECT count(*) FROM traffic.camera_map();`. It's safe to run again.)

Import the sample data (787 events from `vehicles-2.mp4`), with the project's venv active:

```powershell
python -m database.load_events data/sample_events.csv --source-id sample-vehicles-2 --camera-id sample-camera --naive-offset=+00:00
```

It should print `Imported: 787 events (sample-vehicles-2)`. Keep this PowerShell window
open for step 3.

## 3. Build and test the API's connection string

Still in the same session, build it from the pooled host (the direct host from step 1
with `-pooler` added after the endpoint ID) and test it:

```powershell
$url = ($env:DATABASE_URL -replace '^postgresql://[^@]+@', "postgresql://traffic_api:$($env:API_PASSWORD)@" -replace '^(postgresql://[^@]+@ep-[^.]+)', '$1-pooler' -replace '\?.*$', '?sslmode=require')
docker run --rm postgres:17 psql $url -tAc "SELECT current_user, count(*) FROM traffic.events"
```

It must print `traffic_api|787`. That `$url` is `DATABASE_URL_READONLY`. Copy it to the
clipboard for step 4, and save it in your password manager:

```powershell
$url | Set-Clipboard
```

It can only read: writes are refused by both the grants and
`default_transaction_read_only`.

Once it's in Vercel (step 4), clear everything from the session and the clipboard:

```powershell
Set-Clipboard -Value ' '; Remove-Item Env:DATABASE_URL, Env:API_PASSWORD; Remove-Variable url, b; cls
```

## 4. Create the Vercel project and set the variables

1. Sign up at https://vercel.com (Hobby) with your GitHub account, and install the CLI
   (already done on Michael's PC): `npm install -g vercel`.
2. Log in and link this folder to a new project:

   ```powershell
   vercel login
   vercel link
   ```

   Choose **create a new project**. Keep the root directory as `./`. The settings come
   from `vercel.json`, so don't override the build settings when asked.
3. Add the API's connection string, for Preview first. The CLI prompts for the value,
   so it never lands in your shell history:

   ```powershell
   vercel env add DATABASE_URL_READONLY preview
   ```

   Add it for `production` the same way when you're ready to go live.
4. Florida cameras need nothing: `/api/fl511/cameras` is public since 2026-10-29 (FDOT
   confirmed in writing that the project may use FL511 with no end date, public display
   included). If an older deployment has `RESEARCH_ACCESS_TOKEN`, remove it; it is unused:

   ```powershell
   vercel env rm RESEARCH_ACCESS_TOKEN preview
   ```

**Connecting GitHub** (optional; automatic deploys on every push): the repo belongs to
`Monisha-Repos`, so its owner has to allow the Vercel GitHub app on it. Until then, deploy
from the CLI as below.

## 5. Deploy a Preview and check it

```powershell
vercel deploy
```

This builds on Vercel and prints a Preview URL. Previews are protected by Vercel login by
default, so teammates need access to the Vercel project to open them. Check:

- `<preview URL>/api/health` returns `{"database":"ok","status":"ok"}`
- the main page shows the camera map. It's empty until the first sweep (step 6), with
  a "No traffic readings yet" note while every camera is learning or it's dark
- **History** shows `sample-camera` with **787** vehicles, 368 inbound / 419 outbound,
  busiest lane 3 (191)

Then tell Claude the Preview URL for the full check: every endpoint, every view, and the
replay panel.

**Production** (`vercel deploy --prod`, or automatic from `main` once GitHub is connected)
comes after the review, the team's decision above, and the PR into `main`.

## 6. The live map's worker (sweep and live analysis)

The map works without this step: it shows whatever sweeps are stored, and live analysis
answers "offline". The worker runs on Michael's PC, because it needs the NVIDIA GPU.

**a. The worker's write login (once).** `traffic_writer` can add rows but never change or
delete them. In the PowerShell window from step 2 (owner connection string in
`DATABASE_URL`), create it with a generated password:

```powershell
$b = New-Object byte[] 32; [Security.Cryptography.RNGCryptoServiceProvider]::Create().GetBytes($b); $env:WRITER_PASSWORD = ($b | ForEach-Object { $_.ToString('x2') }) -join ''
docker run --rm -e WRITER_PASSWORD -v "${PWD}:/work" -w /work postgres:17 psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/worker_writer_role.sql
```

Then run `database/map_schema.sql` once more, so `traffic_writer` gets the map grants
(it skips grants for roles that don't exist yet). Keep the writer connection string for
step c: the owner string with the user and password swapped for `traffic_writer` and
`WRITER_PASSWORD`, on the direct host (no `-pooler`).

**b. The tunnel (once).** Install `cloudflared` and create the named tunnel to
`worker.brightwaterts.com`, as in [worker/README.md](worker/README.md#reaching-the-worker-from-vercel-cloudflare-tunnel).
The credentials JSON it writes in `%USERPROFILE%\.cloudflared\` is a secret.

**c. Vercel variables.** Generate a worker token (32+ characters), keep it for step d, and
add it with the worker's address. The CLI prompts for each value:

```powershell
vercel env add WORKER_URL preview      # https://worker.brightwaterts.com
vercel env add WORKER_TOKEN preview    # the worker token
vercel env add LIVE_ACCESS_CODE preview   # optional: only people with this code can start live analysis
```

Redeploy (`vercel deploy`) so the new variables apply.

**d. Each session**, in two PowerShell windows from the repo root:

```powershell
.\worker\run_local.ps1
cloudflared tunnel run traffic-worker
```

`run_local.ps1` asks for the `traffic_writer` connection string and the worker token with
hidden prompts. It serves on `127.0.0.1:7860` only and starts the district 3 sweep at once,
then every 30 minutes. A round takes about 15–20 minutes. Restarting starts a new sweep
straight away, so don't restart it more than you need: each sweep fetches every
camera's stills from Caltrans again.

**Check it:**

- `https://worker.brightwaterts.com/health` shows `"status":"ok"` and only `busy`,
  `running` and `next_run`. No session ids and no errors appear: it's the only route
  without a token.
- `vercel curl /api/map/cameras --deployment <preview URL>` lists the district 3 cameras with
  `"swept": true` after the first round.
- Clicking a camera on the Preview's map starts live analysis within about 10 seconds.

## If something goes wrong

- **Build fails in the Rust step:** the Rust runtime is in beta. Send Claude the build
  log from the Vercel dashboard (Deployments → the deployment → Build Logs).
- **`/api/health` returns 503 with `"database":"error"`:** `DATABASE_URL_READONLY` is
  wrong or missing for that environment. Check the user (`traffic_api`), the `-pooler`
  host, and `?sslmode=require`, then redeploy, since variables apply to new deployments
  only.
- **Dashboard loads but shows "The API is not reachable":** the `/api` rewrite or the
  function failed. The function logs are under Deployments → Functions.
- **Live analysis says "offline":** the worker or the tunnel isn't running, or `WORKER_URL`
  or `WORKER_TOKEN` is missing or wrong for that environment (redeploy after changing them).
  Check `https://worker.brightwaterts.com/health` first.
- **"Video unavailable" for one camera:** Caltrans isn't serving that camera's stream at
  the moment, even though its feed lists it. Try another camera.
- **The worker can't log in to Neon (`password authentication failed for user
  "traffic_writer"`):** re-run `database/worker_writer_role.sql` with a new
  `WRITER_PASSWORD` and use the new connection string.
- **Change the API password:** reset it in the Neon dashboard (Roles → `traffic_api`),
  update `DATABASE_URL_READONLY` in Vercel, and redeploy.
