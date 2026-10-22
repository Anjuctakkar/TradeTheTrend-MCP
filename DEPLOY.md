# Deploying the NSE dashboard (multiuser)

This guide takes you from the project folder to a running, invite-only service where every user signs in
with Google and has their own Evaluation list (kept on their own computer), settings and assistant quota.

## 0. What you are deploying

```
Browser ──HTTPS──> Caddy (certificate) ──> App (FastAPI, one process) ──> Postgres (users, settings,
                                              │                                 chat quota log, shared NSE cache)
                                              ├──> NSE MCP servers (market data, shared cache in front)
                                              └──> Anthropic API (the chat assistant)
```

* **Sign-in:** Google only, by invitation. Your own signed session cookie lasts 12 hours.
* **Per user, on the server:** funding rules, live watch list, assistant quota and token log.
* **Per user, on their own computer:** the Evaluation list and the suggestions it receives (see section 10b).
* **Shared:** NSE market data (cached, so ten users asking about TCS cost one NSE request) and the Anthropic key.
* **Admin only:** the Anthropic key and model (Settings), the tool explorer, `/api/admin/usage`, `/api/admin/cache`.

### Pick a route

| Route | Best for | Effort | You manage |
|---|---|---|---|
| **A. Docker on a VPS** | Full control, lowest running cost | about 30 min | server, updates, backups |
| **B. Render** | No server to look after | about 20 min | Git repo, Render plan |
| **C. VPS without Docker** | You prefer systemd and apt | about 45 min | server, Python, Postgres, Caddy |

Steps 1 to 3 are the same for every route. Step 8 onwards (first sign-in, operations) applies to all.

### What is in this folder

| Path | What it is |
|---|---|
| `server.py`, `auth.py`, `db.py`, `usage.py`, `user_store.py`, `nse_cache.py` and the other `.py` files | The application |
| `static/` | The web page (`index.html`) and the chat widget |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile` | Route A |
| `render.yaml` | Route B |
| `deploy/nse-dashboard.service`, `deploy/Caddyfile.vps` | Route C |
| `deploy/backup.sh`, `deploy/restore.sh` | Database backup and restore (Docker route) |
| `.env.production.example`, `.env.local.example` | Settings templates |
| `requirements.txt`, `requirements.lock.txt` | Packages: loose (local) and exact tested versions (Docker, Render) |
| `tests/`, `scripts/run_tests.sh` | The automated tests |
| `static/eval-store.js` | Keeps each user's Evaluation list on their own computer |
| `import_legacy.py` | One-time import of your old funding rules |

`agent_app.py` is an older anonymous chat service. **Do not deploy it** in the multiuser setup: it has no sign-in.

---

## 1. Before you start

- [ ] An **Anthropic API key** with credit (console.anthropic.com), and a spending limit set there.
- [ ] A **domain name** you can edit DNS for (for example `app.yourdomain.com`). Routes A and C need it. Render gives a free `onrender.com` address you can use first.
- [ ] A **Google account** to create the sign-in credentials.
- [ ] Read the **compliance note** at the end of this guide before you invite anyone outside a small trusted group.

---

## 2. Set up Sign in with Google

Google labels and menus change; if a name below differs, look for the closest match.

1. Open the Google Cloud console (console.cloud.google.com) and **create a project** (any name, for example "NSE dashboard").
2. Open **Google Auth Platform** (older guides call this *APIs & Services > OAuth consent screen*) and set it up:
   * App name, support email and developer contact email.
   * Audience: **External** (unless every user is in one Google Workspace, then **Internal** is simpler).
   * Add your **privacy policy** and **terms** page links if you have them. Publishing for the public expects them.
3. Create the credential: **Clients > Create client** (older: *Credentials > Create credentials > OAuth client ID*).
   * Application type: **Web application**.
   * **Authorized JavaScript origins**: add each address users will open, exactly (scheme, host, port, no trailing slash, no path):
     * `https://app.yourdomain.com` (production; for Render use your `https://….onrender.com` address)
     * `http://localhost:8000` and `http://127.0.0.1:8000` (only if you want to test on your own computer)
   * **Authorized redirect URIs**: leave empty. This app does not use a redirect.
4. Copy the **Client ID** (it ends in `.apps.googleusercontent.com`). It is not a secret. This is your `GOOGLE_CLIENT_ID`.
5. **Testing or In production?** This matters:
   * In **Testing**, Google only lets people listed as *test users* sign in, up to 100, and their Google authorization expires after 7 days (they just see Google's prompt again). You would then keep **two** lists: Google's test users and your own `ALLOWED_EMAILS`.
   * In **In production** (the *Publish app* button), any Google account can reach your sign-in, and **your own invite list (`ALLOWED_EMAILS`) is the only gate**. This is simpler for more than a handful of users. This app asks only for basic sign-in information (name, email, picture), but check the console's verification messages for anything Google asks you to complete.
6. New origins can take a few minutes, occasionally longer, to start working.

## 3. Make your secrets

On any computer with OpenSSL (or use `python -c "import secrets; print(secrets.token_hex(32))"`):

```bash
openssl rand -hex 32     # DASHBOARD_SECRET: signs the session cookie. Keep it private.
openssl rand -hex 24     # POSTGRES_PASSWORD (Docker route): letters and digits only, so it is safe inside a URL.
```

Never put these, or your Anthropic key, in the website code or in Git.

---

## Route A: Docker on a VPS

You need a small Linux server (1 GB RAM is enough to start) with a public IP address.

1. **DNS.** In your domain's DNS settings add an `A` record: `app.yourdomain.com` → the server's IP. Wait a few minutes.
2. **Firewall and Docker.** On the server:
   ```bash
   sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
   ```
   Install Docker Engine and the Compose plugin from https://docs.docker.com/engine/install/. Use SSH keys, not a password, to log in.
3. **Copy the project** to the server (for example `scp -r nse-dashboard user@server:~/`) and go into it:
   ```bash
   cd ~/nse-dashboard
   ```
4. **Settings file.**
   ```bash
   cp .env.production.example .env
   chmod 600 .env
   mkdir -p data && sudo chown 1000:1000 data
   nano .env
   ```
   Fill in at least: `DOMAIN`, `POSTGRES_PASSWORD`, `GOOGLE_CLIENT_ID`, `DASHBOARD_SECRET`, `ADMIN_EMAILS` (your Google address),
   `ALLOWED_EMAILS` (friends, optional), `ANTHROPIC_API_KEY`.
5. **Start.**
   ```bash
   docker compose up -d --build
   docker compose ps
   docker compose logs -f app        # Ctrl+C to leave
   ```
   Caddy gets the HTTPS certificate by itself on the first visit. That needs the DNS record and ports 80 and 443 open.
6. Open `https://app.yourdomain.com`. You should see the Google sign-in page. Continue with **step 8**.
7. **Schedule backups** (step 9).

## Route B: Render

Render builds from a Git repository and gives you HTTPS, a managed Postgres and automatic redeploys on every push.

1. **Put the project on GitHub in a private repository.** `.gitignore` already keeps `.env`, data and backups out.
   ```bash
   git init && git add . && git commit -m "NSE dashboard"
   # create an empty PRIVATE repo on GitHub, then run its two "push an existing repository" lines
   ```
2. In Render choose **New > Blueprint**, connect the repository. Render reads `render.yaml`, shows a web service and a Postgres database, and asks for the values marked "ask":
   * `GOOGLE_CLIENT_ID`, `ADMIN_EMAILS`, `ALLOWED_EMAILS` (comma separated), `ANTHROPIC_API_KEY`.
   * `DASHBOARD_SECRET` and `DATABASE_URL` are filled in for you.
3. Click **Apply** and wait for the first deploy (a few minutes).
4. Open your `https://nse-dashboard-….onrender.com` address. **Add that exact address to the Google client's Authorized JavaScript origins** (step 2.3) if you have not already, wait a few minutes, then sign in.
5. Your own domain (optional): service > **Settings > Custom Domains > Add**, create the DNS record Render shows, then add `https://app.yourdomain.com` to the Google origins too.
6. Continue with **step 8**.

Things to know on Render:
* The plan names in `render.yaml` (`starter` web, `basic-256mb` database) were current when written. Check Render's pricing page; free databases expire after 30 days, so do not use one for real data.
* Web and database must be in the **same region** (both are `singapore` in the file) for the private connection.
* Render has no persistent disk in this setup, which is fine: all data is in Postgres. But the dashboard's **Settings > Anthropic API key** form writes to a file that is lost on redeploy. **Set `ANTHROPIC_API_KEY` in Render's Environment tab** and treat the Settings form as a test button.
* Check your database plan's backup options in the Render dashboard, or take your own dumps (step 9).
* The file pins Python 3.12 through `.python-version`, because Render's default is newer than the one tested.
* Check the blueprint before applying: `render blueprints validate render.yaml`.

## Route C: VPS without Docker

1. **DNS and firewall** as in Route A steps 1 and 2.
2. **Packages.**
   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv postgresql caddy
   ```
   Use Python 3.11 or newer (3.12 is what was tested).
3. **Database.**
   ```bash
   sudo -u postgres psql -c "CREATE USER nse WITH PASSWORD 'PUT_THE_HEX_PASSWORD_HERE';"
   sudo -u postgres psql -c "CREATE DATABASE nse OWNER nse;"
   ```
4. **App.**
   ```bash
   sudo mkdir -p /opt/nse-dashboard && sudo cp -r . /opt/nse-dashboard   # run from the project folder
   cd /opt/nse-dashboard
   sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.lock.txt
   sudo cp .env.production.example .env && sudo nano .env
   ```
   In `.env` set `DATABASE_URL=postgresql://nse:YOUR_PASSWORD@127.0.0.1:5432/nse` plus `GOOGLE_CLIENT_ID`, `DASHBOARD_SECRET`, `ADMIN_EMAILS`, `ALLOWED_EMAILS`, `ANTHROPIC_API_KEY`. (`DOMAIN` and `POSTGRES_*` are not needed.)
5. **Run as a service.**
   ```bash
   sudo useradd --system --home /opt/nse-dashboard nse
   sudo mkdir -p /opt/nse-dashboard/data
   sudo chown -R nse:nse /opt/nse-dashboard && sudo chmod 600 /opt/nse-dashboard/.env
   sudo cp deploy/nse-dashboard.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now nse-dashboard
   sudo systemctl status nse-dashboard
   ```
6. **HTTPS.** Edit `deploy/Caddyfile.vps` (your domain), then
   `sudo cp deploy/Caddyfile.vps /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
7. Open `https://app.yourdomain.com` and continue with **step 8**. Logs: `journalctl -u nse-dashboard -f`.
   Back up with `sudo -u postgres pg_dump nse | gzip > nse-$(date +%F).sql.gz`.

---

## 8. First sign-in and checks (all routes)

1. Open your address and sign in with the Google account listed in `ADMIN_EMAILS`. You are now an admin.
2. **Settings > Test NSE connection.** NSE may treat a data-centre address differently from your home connection, and this has not been tested from your host. If both servers fail only from the server, that is the reason.
3. **Settings > Anthropic API key > Test current key**, then ask the chat assistant a question.
4. Open `/api/admin/cache` and `/api/admin/usage` in the same browser tab (you are signed in). After a few lookups the cache report should show hits.
5. **Bring over your old data** (optional, one time).
   * **Evaluation list** (`suggestions_log.json`): it is no longer kept on the server. In the app open the **Evaluation** tab, click **Open my file…** and choose your old `suggestions_log.json`. It is read in your browser and kept on your computer (see section 10b).
   * **Funding rules** (`funding_rules.json`): copy the file into the folder the app reads, then import it into *your* account:
     * Docker: `cp funding_rules.json data/` then `docker compose exec app python import_legacy.py you@gmail.com`
     * No Docker: put it in `/opt/nse-dashboard/data/`, then `cd /opt/nse-dashboard && sudo -u nse env DASHBOARD_DATA_DIR=/opt/nse-dashboard/data .venv/bin/python import_legacy.py you@gmail.com`
     * Render: run it from your own computer with `DATABASE_URL` set to the database's **external** URL (from the Render dashboard) and the file beside the code: `python import_legacy.py you@gmail.com`
6. **Invite someone.** Add their Google address to `ALLOWED_EMAILS`, then restart:
   * Docker: `docker compose up -d --force-recreate app`
   * Render: save the Environment tab (it redeploys)
   * No Docker: `sudo systemctl restart nse-dashboard`

   (If the Google app is in *Testing*, also add them as a test user in Google.) They sign in, start with an empty Evaluation list and the default settings, and nobody else can see their data.

## 9. Running it

**Updating the app.** Replace the files, then:
* Docker: `docker compose up -d --build`
* Render: `git commit` and `git push` (it redeploys)
* No Docker: copy the files, `sudo .venv/bin/pip install -r requirements.lock.txt`, `sudo systemctl restart nse-dashboard`

New tables are created automatically on start. Changing the *columns* of an existing table later needs a migration (Alembic), which this version does not include.

**Backups** (Docker route):
```bash
./deploy/backup.sh                       # writes backups/nse-DATE.sql.gz, keeps 14 days
crontab -e                               # then add:  15 2 * * * /home/YOU/nse-dashboard/deploy/backup.sh
./deploy/restore.sh backups/FILE.sql.gz  # replaces the current data (asks first)
```
Copy the `backups/` folder somewhere off the server regularly. A backup on the same disk does not survive losing the disk. Also keep your `.env` somewhere safe (a password manager).

**Managing users** (Docker; for no-Docker use `sudo -u postgres psql nse`, for Render the database's external URL with `psql`):
```bash
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```
```sql
SELECT id, email, role, is_active, last_login_at FROM users ORDER BY id;
UPDATE users SET is_active = false WHERE email = 'person@gmail.com';              -- block them at once
UPDATE users SET session_version = session_version + 1 WHERE email = 'person@gmail.com';   -- sign them out everywhere
UPDATE users SET role = 'admin' WHERE email = 'person@gmail.com';                 -- or list them in ADMIN_EMAILS
```
Removing an address from `ALLOWED_EMAILS` stops *new* sign-ins; a person already signed in keeps their session for up to 12 hours unless you also deactivate them. Deactivated users stay blocked even if still on the list.

**Signing everyone out:** change `DASHBOARD_SECRET` and restart.

**Watching cost and health**
* `/api/admin/usage`: questions and tokens per user. Set `AGENT_USER_DAILY_CAP` and `AGENT_DAILY_CAP` to what you are happy paying for, and keep a spending limit in the Anthropic Console.
* `/api/admin/cache`: hit rate. If it stays near zero, check `NSE_CACHE` is not `0`. `DELETE /api/admin/cache` clears it (for example after NSE corrects data).
* Point an uptime monitor (UptimeRobot or similar) at `https://your-address/health`. It returns 200 only when the app is up *and* the database answers.

**Scaling.** One app container is plenty for a small group. You may run more than one: all user data and the NSE cache are in Postgres. Four things stay per process, so limits get a little looser with several: a user's cap on questions running at once, the scan queue (`NSE_HEAVY_JOBS`), merging of identical simultaneous NSE requests, and the sign-in attempt limiter. Do not raise uvicorn's `--workers`; add containers instead.

---

## 10. How the shared NSE cache behaves

| Data | Kept for | If NSE is down |
|---|---|---|
| End-of-day prices, history, moving averages, top-by-volume | 15 min between 16:00 and 22:00 India time (when NSE publishes), otherwise 6 h; never past the next 16:00 or 22:00 boundary | Serves an older copy up to 24 h, marked `stale` |
| Older history chunks | 24 h | Up to 24 h |
| Corporate actions | 6 h | Up to 3 days |
| Symbol search | 24 h | Up to 7 days |
| Live quotes, movers, feed status | 30 s | Error (never stale) |

Errors and empty answers are never cached. The admin tool explorer (`/api/call`) always asks NSE directly. Suggest-stocks scans queue (`NSE_HEAVY_JOBS`, default 2 at a time across everyone), and one user can run only one scan at a time. The first scan of the day is slow (about a minute); later scans reuse the cached prices and are much faster.

Live quotes on the Live tab can be up to 30 seconds old, so the "Updated" time on that tab may be slightly ahead of the quote's own timestamp. The quote's own age column is the real signal.

## 10a. Which prices are live and which are end-of-day

* **Signals stay on end-of-day closes.** Entry, stop, target, RSI, MACD and the moving averages come from the Bhavcopy closes, so they change once a day. That is how the rules are defined, and it keeps backtests comparable with live results.
* **The current market price (CMP) is live.** It comes from the CM live feed (shared 30-second cache). It is labelled **live** while the market is open and the quote is under 10 minutes old, **last trade** (with its time) when the market is closed, and **end-of-day close** only if the live feed failed. A fallback is always labelled, never passed off as live.
* **Where the CMP is used:**
  * Portfolio analysis: Qty, Invested and Leftover are sized at the CMP; the 52-week position is shown at the CMP; the signal box says whether the setup still holds at today's price (price now, % from the entry, reward:risk if bought now, or a warning when price is already below the stop or at the target).
  * Funding and net P/L: costs, interest and break-even assume you buy at the CMP when the setup still holds there, and at the signal's entry otherwise.
  * Suggest stocks: quantities use the CMP, and setups already below their stop or at their target at that price are skipped and listed.
  * Evaluation: open trades show the CMP and a flag when today's price is at or beyond the stop or target. The status itself only changes once the day's bars confirm it.
  * Symbol lookup, the Live tab, and the assistant (its answers state the price source and time).
* **Cost:** one extra live-quote request per stock analysed, shared across users for 30 seconds.
* **Not covered:** the market-open check uses the clock and weekday only, not NSE holidays. On a holiday the CMP shows as *last trade* with its real timestamp.

## 10b. Where the Evaluation list lives

The Evaluation tab (stocks you ticked, and stocks **Suggest stocks** found) is stored on **each user's own computer**, not on the server. Nothing about it is in the database or on Render's disk.

* **Automatically:** every change is saved in the browser's own storage, kept separately for each signed-in user, and loaded again every time the app opens.
* **As a JSON file (recommended):** in the Evaluation tab, **Your saved list**:
  * *Create a file…* (Chrome and Edge): choose where `nse-evaluation.json` lives, for example in a synced folder. From then on every change is written to it, and it is loaded each visit. The browser asks permission once per visit; click **Reconnect file**.
  * *Open my file…*: merges a file into your list (an old `suggestions_log.json` works too). In Chrome and Edge it also links the file.
  * *Download a copy*: works in every browser, including Firefox and Safari, which cannot keep a file linked.
* **Checking outcomes:** **Refresh outcomes** sends the list to the server, which checks prices against the shared NSE data and sends the results back. Nothing is stored. Up to 300 stocks with levels are checked at a time.
* **The assistant:** the chat widget sends the newest 50 entries with each question so the assistant can discuss them. They are used for that answer only.

Things to tell your users:
* The list is **per browser and per computer**. A different browser or computer starts empty until they open their file there. Linking a file in a synced folder (OneDrive, Google Drive, Dropbox) gives the same list everywhere.
* Browsers can clear their storage (clearing site data, private windows). A linked or downloaded file is the safe copy.
* If a file is linked and permission is missing at the start of a visit, changes are kept in the browser and merged into the file when they reconnect.
* A file that is not an evaluation list is never overwritten.

Because of this, **backing up the database does not back up anyone's Evaluation list**, and you cannot recover a user's list for them.

## 10c. Two rule sets

Setups come from two fixed, mechanical rule sets. Neither is advice.

* **Trend and momentum** (`technical.py`): 20/50-day SMA trend, price vs 20-day SMA, MACD vs signal, RSI zone.
* **Support and resistance** (`sr_model.py`): swing-pivot zones with at least 2 touches, then a bounce off support or a fresh breakout above resistance. Stop below the zone, target just under the next resistance (or 2x the risk if none).

Where they show up:
* **Portfolio analysis:** each stock card shows both setups. The support/resistance zones are drawn on the price chart. **Sizing and funding use** (Capital section) chooses which setup drives the suggested capital split and the net P/L figures; the default is trend and momentum.
* **Suggest stocks:** a **Rule set** selector (trend, support/resistance, or either). Each pick is labelled with the rule set that found it.
* **Backtest:** a **Rule set** selector. The holding-period cross-check also replays the same rule set that produced the setup.
* **Evaluation list:** each entry records its rule set, and the summary table compares them. Ticking **Eval** on a stock adds one entry per rule set that shows a LONG setup. Old lists load as trend and momentum.
* **Assistant:** `sr_levels`, `strategy` on `backtest_stock`, `model` on `suggest_stocks`, and the rule set on watchlist entries.

The two rule sets are separate setups with their own entry, stop and target. Compare their outcomes on the Evaluation tab over a real sample before relying on either. Run the checks in `tests/test_sr_model.py` after any change to `sr_model.py`.

## 10d. Sector momentum

The **Sectors** tab follows NSE's 12 macro sectors (Commodities, Consumer Discretionary, Energy, FMCG, Financial Services, Healthcare, Industrials, Information Technology, Services, Telecommunication, Utilities, Diversified) and their industry groups (for example Financial Services: Banks, Financial Institutions, Capital Markets, Insurance). It ranks the sectors by momentum and charts each one daily, weekly or monthly; click a sector to see its industry groups, then click an industry group to chart it and list its stocks.

Each sector and each industry group is an equal-weight index of a short basket (large, mid and small caps), built from adjusted prices: a proxy, not an official NSE index. The stock lists and cap tiers are approximate and should be reviewed. Each is indexed for all stocks, large caps only, and mid and small caps only. Loading all sectors fetches about 290 stocks (30 minutes or more); use **Load** to fetch one sector (4 to 56 stocks). End-of-day history is reused for 15 minutes (`SECTOR_TTL`, in seconds). On top of it the live feed's latest traded price (LTP) is added as the newest point, refreshed at most every 30 seconds (`SECTOR_LIVE_TTL`), so **1D is the LTP against the previous close** (when the market is closed, the last trade). Setups for the stocks still use end-of-day bars. The tab shows the market state, how many stocks have a current price, the history date and the age of each refresh; **Refresh live prices** can update loaded sectors every minute or 5 minutes while the tab is open. To add stocks or sectors, create a file named `sector_map.json` (underscore, not hyphen) in the app's data folder: next to `server.py` when you run locally or on Render (commit it to the repo; Render's disk is not kept between deploys), or `data/` beside `docker-compose.yml` for Docker (`DASHBOARD_DATA_DIR`). The Sectors tab shows the exact path and whether the file was found. The file is merged with the built-in lists; add `"_mode": "replace"` to use only your file. See `sector_map.example.json`: a sector maps industry groups to tiers (`large`, `mid`, `small`), and a plain list is treated as large caps. Restart the server after editing. Tick the stocks you want in a sector and send only those to Portfolio analysis.

Settings > **Discover NSE tools** (admins only) lists every tool the two NSE servers offer and highlights any that mention sectors, industries, indices or constituents. Use it to check whether NSE can supply sector classification or index constituents before editing baskets by hand.

## 10e. Sector rotation

Under the sector ranking, the **Sector rotation** chart plots each sector's relative strength against the equal-weight of the sectors loaded (RS-Ratio across, RS-Momentum up), with a short trail showing where it came from. It is a simplified relative-rotation view, not the licensed JdK RRG formula. **Improving** (relative strength still below its norm but recovering) and sectors that just stepped from Improving into Leading are flagged as early movers. It follows the Chart (daily, weekly, monthly) and Stocks (all, large, mid and small) selectors. **Rotation view** switches to the industry groups of the selected sector. Load at least 3 sectors first. It uses data already loaded, so it makes no extra NSE calls.

## 10f. Stocks to review

Below the rotation chart, **Stocks to review** lists stocks from the loaded sectors that are strong or turning up: a positive 1-month return, not below their 50-day average, and either a LONG setup (trend or support/resistance rules) or a 1-month return ahead of their sector, inside a sector with a score of at least 40 or an early mover. Each gets a score out of 100 (sector 40, against its sector 30, setups 30), the reasons, and cautions (extended, mid or small cap, no live price). At most 4 per sector. Tick stocks and send only those to Portfolio analysis. It is a mechanical screen of past prices, not a recommendation. It uses sectors already loaded, so load several for a useful list.

The illustrated user guide is `static/TradeTheTrend-User-Manual.pdf`. It is served by the app (so it needs sign-in like every other page) and linked as **Guide** in the top bar and under Settings > User guide. To update it, replace that file and redeploy. It opens in its own tab rather than inside the page, because the app's security headers forbid framing.

## 10g. Table filters

Every table with four or more rows, in every tab, has a filter box under each column heading and click-to-sort headings, plus a row count and **Clear filters**. In a box, type text (contains), a comparison for numbers (`>5`, `<=-2`, `=3`), a range (`3..8`), several choices separated by commas (`TCS, INFY`), or start with `!` to exclude. Filters are kept when a tab refreshes itself (for example Live). Tick-all boxes only tick the rows left showing. Tables with fewer than four rows are left alone.

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| No Google button; browser console says the origin is not allowed | The address you opened is not in the Google client's **Authorized JavaScript origins**. It must match exactly (https vs http, `www`, port). Wait a few minutes after adding. |
| "This Google account hasn't been invited yet" | The address is not in `ADMIN_EMAILS`, `ALLOWED_EMAILS` or `ALLOWED_DOMAINS`. Add it and restart. |
| Google says the app is blocked or the user is not a test user | The Google app is in **Testing**. Add the person as a test user, or publish the app (step 2.5). |
| "This account has been deactivated" | `is_active` is false for that user (see step 9, managing users). |
| App refuses to start: `DASHBOARD_PUBLIC is on, but GOOGLE_CLIENT_ID is not set` | The client ID is missing from the environment. |
| App refuses to start: `DASHBOARD_SECRET must be at least 32 characters` or `Nobody could sign in` | Set a longer secret; set `ADMIN_EMAILS`. |
| Signed in, then sent back to the login page | The cookie is not being kept: check you are on HTTPS (cookie is `Secure`), the clock is right, and `DASHBOARD_SECRET` has not changed. |
| `/health` returns 503 | The database is not reachable. Docker: `docker compose logs db app`. Render: check `DATABASE_URL` is wired and both are in one region. |
| `password authentication failed` in the app logs | `POSTGRES_PASSWORD` in `.env` differs from the one the database was first created with. The volume keeps the first password: either restore the old value or recreate the volume (this deletes data). |
| Everyone shares one rate limit or gets locked out together | The app is not seeing real visitor addresses. Behind Caddy this works by default. On Render keep `DASHBOARD_CLIENT_IP_HEADER=cf-connecting-ip`. |
| Settings > Test NSE connection shows `HTTP 404` from mcp.nseindia.in, often with "Session not found" | NSE's MCP server did not recognise the session the app had just opened (it can happen when NSE runs several servers behind one address, or restarts one). NSE's published addresses (`https://mcp.nseindia.in/bhavcopy/cm/mcp` and `https://mcp.nseindia.in/cmmkt/mcp`) are unchanged. The app retries the whole handshake with a fresh session (`NSE_RETRIES`, default 4). If it still fails, wait a few minutes and test again, and compare with running the app on your own computer. |
| Analysis fails with "NSE did not reply" | NSE's servers are slow or down, or block your host's address. Try Settings > Test NSE connection; raise `NSE_READ_TIMEOUT`. Recently cached data keeps working. |
| Assistant says it is unavailable | Generic message for users; the log has the cause (billing, key, model). As admin, Settings > Test current key shows it plainly. |
| `.env` values are ignored in Docker | Comments after a value are part of the value in Compose. Keep comments on their own lines. |

Logs: Docker `docker compose logs -f app` · Render: the service's Logs tab · systemd `journalctl -u nse-dashboard -f`.

## 12. Run it on your own computer

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.local.example .env                              # add ANTHROPIC_API_KEY
python server.py                                        # http://127.0.0.1:8000
```
With `GOOGLE_CLIENT_ID` empty there is no login and you are one local admin; data is kept in `app.db` beside the code.
Run the tests with `./scripts/run_tests.sh`.

## 13. Before other people use it (compliance)

This setup lets other people see entry, stop and target levels produced by a rule set. Before inviting anyone outside a small trusted group:

- Check **NSE's terms**: are you allowed to show this data to others?
- Check **Indian rules on public investment guidance.** Showing entry, stop and target levels to others may need SEBI research-analyst or investment-adviser registration. Ask someone qualified. The "information only" wording in the app and the assistant's prompt does not by itself settle this.
- Add **terms of use and a privacy notice** (Google also asks for these when you publish the sign-in app). The app stores each user's Google email, name and picture, their funding rules and live watch list, and chat usage counts (not chat text). The Evaluation list stays on each user's own computer.
- Keep the Anthropic spending limit and the per-user caps in place.

Not included in this version: email invites or an in-app user admin screen (users are managed with the SQL above), schema migrations, two-step sign-in beyond what Google provides, and an audit log.
