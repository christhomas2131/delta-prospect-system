# Deploying Delta Prospect System to Railway

Step-by-step guide. No prior Railway experience needed.

---

## Step 1: Create a Railway Account

1. Go to **https://railway.app** in your browser
2. Click **"Start a New Project"** or **"Login"**
3. Sign up with your **GitHub account** (this is the easiest way — it also lets Railway access your repos)
4. You'll land on the Railway dashboard

---

## Step 2: Create a New Project

1. On the Railway dashboard, click **"New Project"**
2. Choose **"Deploy from GitHub Repo"**
3. Find and select **delta-prospect-system** from the repo list
   - If you don't see it, click "Configure GitHub App" and grant Railway access to the repo
4. **Don't deploy yet** — click "Add variables first" or just let it start (we'll add variables next and it will auto-redeploy)

---

## Step 3: Add a PostgreSQL Database

1. Inside your project, click **"New"** (the + button)
2. Choose **"Database"** then **"Add PostgreSQL"**
3. Railway will spin up a PostgreSQL instance — this takes about 30 seconds
4. Once it's ready, click on the PostgreSQL service
5. Go to the **"Variables"** tab
6. Find `DATABASE_URL` — copy its value (you'll need it in the next step)

---

## Step 4: Connect the Database to Your App

1. Click on your **delta-prospect-system** service (not the database)
2. Go to the **"Variables"** tab
3. Click **"Add a Variable Reference"** or **"New Variable"**
4. The easiest way: click on the PostgreSQL service's variable reference to link `DATABASE_URL` automatically. Railway can auto-inject it.
   - Or manually add: `DATABASE_URL` = paste the value you copied from the PostgreSQL service

---

## Step 5: Set All Environment Variables

Still in the **Variables** tab of your app service, add these one by one:

| Variable | Value | Required? | What It Does |
|---|---|---|---|
| `DATABASE_URL` | (linked from PostgreSQL above) | Yes | Tells the app where the database is |
| `PORT` | `8000` | No (Railway sets this) | Railway usually injects this automatically. If not, set it to 8000 |
| `AUTH_USER` | Pick a username (e.g. `admin`) | Recommended | Login username for the web dashboard |
| `AUTH_PASSWORD` | Pick a strong password | Recommended | Login password for the web dashboard |
| `ALLOWED_ORIGINS` | `*` | No | Leave as `*` for now. Once you have a custom domain, set it to that domain |
| `CRON_SECRET` | Make up a long random string | Optional | Protects the scheduled enrichment endpoint. Only needed if you set up the cron job |
| `ANTHROPIC_API_KEY` | Your `sk-ant-...` key | Optional | Only needed for the Deep Analysis premium feature |

**Important:** If you skip `AUTH_USER` and `AUTH_PASSWORD`, anyone with the URL can access your dashboard. Set these before sharing the link with anyone.

---

## Step 6: Deploy

After setting variables, Railway will automatically redeploy. Here's what happens:

1. Railway reads the `Dockerfile` and builds a container image
2. It installs Python dependencies, Node.js, and Playwright's Chromium browser
3. It builds the React frontend (`npm run build`)
4. It starts the FastAPI server with `uvicorn`
5. On first startup, the app detects the empty database and automatically runs `schema.sql` to create all tables

**This first build takes 5-10 minutes** because it's installing a lot (Playwright + Chromium is large). Future deploys are faster because Railway caches layers.

Watch the build logs:
- Click on your service
- Go to the **"Deployments"** tab
- Click the active deployment to see build logs

Look for: `Schema initialized successfully` and `Database pool initialized — connection verified` in the logs. That means it worked.

---

## Step 7: Verify It's Working

1. Once deployed, Railway gives you a URL like `https://delta-prospect-system-production-xxxx.up.railway.app`
2. Find it by clicking your service, then the **"Settings"** tab, under **"Networking"** > **"Public Networking"** — you may need to click "Generate Domain"
3. Open that URL in your browser
4. If you set `AUTH_USER` and `AUTH_PASSWORD`, you'll get a login prompt — enter your credentials
5. You should see the Delta Prospect dashboard (it'll be empty — no data yet)

Quick health check: visit `https://your-railway-url.up.railway.app/api/health` — you should see `{"status": "healthy", ...}`

---

## Step 8: Populate the Database

Your local data (the 994 companies, signals, etc.) is NOT on Railway. The production database starts empty. You need to run the scraper + enrichment to populate it.

**Option A: Use the dashboard UI**
1. Open your Railway URL in the browser
2. Click "Enrich All" on the Dashboard page
3. This scrapes all ASX listings, filters target sectors, and runs enrichment
4. Takes about 20-30 minutes (rate-limited ASX API calls)

**Option B: Hit the cron endpoint manually**
If you set a `CRON_SECRET`, you can trigger it via URL:
```
https://your-railway-url.up.railway.app/api/cron/enrich-all?token=YOUR_CRON_SECRET
```
Open that in your browser or use curl. This runs the full scrape + enrichment cycle.

---

## Step 9: Set Up Scheduled Enrichment (Optional)

To keep data fresh automatically, you can set up a Railway cron job:

1. In your Railway project, click **"New"** > **"Cron Job"**
   - If Railway doesn't show this option natively, create a new **"Empty Service"** instead
2. Set the schedule to run daily (e.g., `0 2 * * *` = 2:00 AM UTC every day)
3. The command to run:
   ```
   curl -s -X POST "https://your-railway-url.up.railway.app/api/cron/enrich-all?token=YOUR_CRON_SECRET"
   ```
4. Replace `your-railway-url` with your actual Railway URL and `YOUR_CRON_SECRET` with the secret you set in Step 5

**Alternative without cron:** Just click "Enrich All" in the dashboard UI whenever you want fresh data. For a consultancy checking weekly, manual refreshes work fine.

---

## Step 10: Custom Domain (Optional, For Later)

1. In your Railway service, go to **"Settings"** > **"Networking"**
2. Click **"Custom Domain"**
3. Enter your domain (e.g., `prospects.yourdomain.com`)
4. Railway will give you a CNAME record
5. Go to your domain registrar (Namecheap, Cloudflare, etc.) and add that CNAME record
6. Wait for DNS to propagate (usually 5-30 minutes)
7. Update `ALLOWED_ORIGINS` to your custom domain if you set it to something other than `*`

---

## Checking Logs If Something Breaks

1. Go to your Railway project dashboard
2. Click on the **delta-prospect-system** service
3. Click the **"Deployments"** tab
4. Click the current deployment
5. You'll see both **build logs** (from Docker) and **runtime logs** (from the running app)

Common issues:
- **"Cannot connect to database"** — Check that `DATABASE_URL` is set correctly in Variables. Make sure the PostgreSQL service is running.
- **Build fails at "npm ci"** — The frontend dependencies might have an issue. Check that `frontend/package-lock.json` is committed to the repo.
- **Health check failing** — The app might still be starting up. The health check timeout is set to 300 seconds (5 minutes), which should be enough. Check runtime logs for errors.
- **502 Bad Gateway** — The app hasn't started yet, or it crashed on startup. Check runtime logs.
- **Playwright errors** — The Docker image includes Chromium. If enrichment fails with browser errors, check logs — it might be a memory issue. Railway's Hobby plan has limited memory; upgrading may help.

---

## Cost Estimate

Railway's pricing (as of 2024):
- **Trial plan**: $5 free credit, no credit card needed
- **Hobby plan**: $5/month, includes $5 credit (so effectively free for small usage)
- **Usage-based**: You pay for what you use beyond the credit

This app uses:
- ~256-512 MB RAM (more during enrichment when Playwright is running)
- Minimal CPU except during enrichment runs
- PostgreSQL storage: a few MB for the data

Expect to stay well within the Hobby plan's $5 credit for normal usage.

---

## Quick Reference

| What | Where |
|---|---|
| Your app | `https://your-railway-url.up.railway.app` |
| Health check | `https://your-railway-url.up.railway.app/api/health` |
| API docs | `https://your-railway-url.up.railway.app/docs` (auto-generated by FastAPI) |
| Trigger enrichment | `POST https://your-railway-url.up.railway.app/api/cron/enrich-all?token=CRON_SECRET` |
| GitHub repo | `https://github.com/christhomas2131/delta-prospect-system` |
| Railway dashboard | `https://railway.app/dashboard` |
