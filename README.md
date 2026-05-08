# 🏛 Linebarger Tax Sale Lead Scraper

Automated daily scraper for **taxsales.lgbs.com** — collects active tax sales,
struck-off properties, and future sales, scores each lead 0–100, and publishes
a live searchable dashboard to GitHub Pages.

---

## 📁 File Structure

```
lgbs-scraper/
├── scraper/
│   ├── fetch.py            ← Main scraper (Playwright + requests)
│   └── requirements.txt    ← Python dependencies
├── dashboard/
│   ├── index.html          ← Live dashboard (deployed to GitHub Pages)
│   └── records.json        ← Auto-updated JSON data
├── data/
│   ├── records.json        ← Backup copy of JSON
│   └── leads_export.csv    ← GHL-ready CSV
└── .github/workflows/
    └── scrape.yml          ← Daily automation
```

---

## 🚀 Step-by-Step Setup Guide

### STEP 1 — Create a GitHub account (if you don't have one)
1. Go to https://github.com
2. Click **Sign up** and create a free account
3. Verify your email

---

### STEP 2 — Create a new repository
1. Click the **+** icon (top right) → **New repository**
2. Name it: `tax-sale-leads` (or anything you like)
3. Set to **Public** (required for free GitHub Pages)
4. Check **Add a README file**
5. Click **Create repository**

---

### STEP 3 — Upload the project files
You have two options:

**Option A — Upload via GitHub website (easiest):**
1. In your new repo, click **Add file → Upload files**
2. Upload ALL files from this ZIP, keeping the folder structure:
   ```
   scraper/fetch.py
   scraper/requirements.txt
   dashboard/index.html
   dashboard/records.json
   data/  (empty folder — add a .gitkeep file)
   .github/workflows/scrape.yml
   ```
3. Click **Commit changes**

**Option B — Use Git on your computer:**
```bash
git clone https://github.com/YOUR_USERNAME/tax-sale-leads.git
cd tax-sale-leads
# Copy all files here
git add .
git commit -m "Initial commit"
git push
```

---

### STEP 4 — Enable GitHub Pages
1. Go to your repo → **Settings** (top menu)
2. Scroll to **Pages** in the left sidebar
3. Under **Source**, select **GitHub Actions**
4. Click **Save**

---

### STEP 5 — Run the scraper for the first time
1. Go to your repo → **Actions** tab
2. Click **Linebarger Tax Sales Scraper** (left sidebar)
3. Click **Run workflow** (right side)
4. Optional inputs:
   - **State filter**: `TX` (or leave blank for all states)
   - **County filter**: `HARRIS` (or leave blank for all counties)
   - **Days back**: `7`
   - **Specific date**: `2026-06-02` (to scrape a specific sale date)
5. Click the green **Run workflow** button
6. Watch it run — takes about 3-5 minutes

---

### STEP 6 — View your dashboard
After the workflow completes:
1. Go to **Settings → Pages**
2. Your dashboard URL will be:
   ```
   https://YOUR_USERNAME.github.io/tax-sale-leads/
   ```
3. Bookmark this — it updates automatically every day!

---

### STEP 7 — Configure automatic daily runs
The scraper already runs automatically every day at **8:00 AM UTC**
(3:00 AM Central time). No extra setup needed.

To change the schedule, edit `.github/workflows/scrape.yml` and modify:
```yaml
- cron: "0 8 * * *"   # minute hour day month weekday
```

Common schedules:
- `"0 8 * * *"` = Every day at 8 AM UTC
- `"0 8 * * 1"` = Every Monday at 8 AM UTC
- `"0 8 1 * *"` = 1st of every month at 8 AM UTC

---

### STEP 8 — Filter to your target county/state
To always scrape only Texas / Harris County, edit `.github/workflows/scrape.yml`:

```yaml
- name: Run scraper
  env:
    STATE_FILTER:  "TX"
    COUNTY_FILTER: "HARRIS"
    DAYS_BACK:     "7"
```

Or set these per-run in the manual workflow dispatch inputs.

---

## 📊 Dashboard Features

| Feature | Description |
|---------|-------------|
| **Score filter** | Filter leads by score (50+, 60+, 70+, 80+) |
| **Sale type filter** | Active Sale / Struck Off / Future Sale |
| **County filter** | Dropdown populated from real data |
| **Free-text search** | Search owner, address, county, account ID |
| **Sort** | By score, date, amount, or county |
| **Map view** | Toggle interactive map with color-coded pins |
| **Detail drawer** | Full record details panel |
| **GHL Export** | One-click CSV download for GoHighLevel |

---

## 🎯 Seller Score (0–100)

| Condition | Points |
|-----------|--------|
| Base | +30 |
| Active Tax Sale | +20 |
| Struck Off | +15 |
| Future Sale | +10 |
| Amount > $50k | +15 |
| Amount > $10k | +10 |
| Has property address | +5 |
| Absentee owner (mail ≠ property) | +10 |
| Sale this week | +5 |
| LLC / Corp / Estate owner | +10 |
| Multi-year delinquent (inferred) | flag only |

---

## 📤 GHL CSV Columns

```
First Name, Last Name,
Mailing Address, Mailing City, Mailing State, Mailing Zip,
Property Address, Property City, Property State, Property Zip,
County, Precinct, Account ID,
Sale Type, Sale Date, Amount Owed,
Seller Score, Motivated Seller Flags,
Legal Description, Source, Public Records URL
```

---

## ❓ Troubleshooting

**Q: The scraper ran but shows 0 records**
- The LGBS site may have updated its API. Check the Action logs for "Intercepted API call" messages.
- Try running with a specific `sale_date_only` date that you know has sales (like `2026-06-02`).

**Q: Dashboard shows "Could not load records.json"**
- Make sure the scraper ran at least once successfully.
- Check that GitHub Pages is set to "GitHub Actions" as the source.

**Q: I want to scrape a different state**
- Set `STATE_FILTER` to the 2-letter abbreviation (e.g. `FL`, `CA`, `TX`).

---

## 🔗 Links

- **LGBS Portal**: https://taxsales.lgbs.com/map
- **Your Dashboard**: https://YOUR_USERNAME.github.io/tax-sale-leads/
- **GitHub Actions**: https://github.com/YOUR_USERNAME/tax-sale-leads/actions
