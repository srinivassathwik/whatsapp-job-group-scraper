# 🔶 C2C WhatsApp Job Scraper

Automatically scrape US C2C job postings from WhatsApp groups and view them in a searchable web dashboard with filters, Excel export, and review audit.

---

## 📸 What It Does

- Logs into WhatsApp Web automatically (QR scan once, stays logged in)
- Scrapes multiple WhatsApp groups in one run
- Filters spam, ads, training promotions, and India-based recruiter posts
- Extracts: title, location, rate, visa types, skills, contact email/phone
- Web dashboard with search, filters, Excel export, and full message review
- `review.json` lets you audit every filtering decision

---

## 🗂 Folder Structure

```
whatsapp/
├── main.py          ← Scraper (Playwright + WhatsApp Web)
├── app.py           ← Dashboard (Flask web server)
├── config.json      ← All settings — only file you need to edit
├── README.md
├── requirements.txt
├── render.yaml      ← Render.com deployment config
├── .gitignore
│
├── output/          ← Auto-created on first run
│   ├── jobs.json           ← Extracted job posts
│   ├── raw_messages.json   ← Every scraped message
│   ├── review.json         ← Every message + filter reason (for auditing)
│   ├── seen_jobs.json      ← Prevents re-scraping duplicates
│   └── scraper.log         ← Full run log
│
└── wa_session/      ← Auto-created after QR scan (your WhatsApp login)
```

> ⚠️ `output/` and `wa_session/` are in `.gitignore` — never commit these

---

## 💻 Local Setup (First Time)

### 1. Clone the repo
```bash
git clone https://github.com/yourusername/whatsapp-c2c-scraper.git
cd whatsapp-c2c-scraper
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure your WhatsApp groups
Edit `config.json` — set your group names:
```json
"groups": [
    "US IT STAFFING",
    "C2C , C2H and W2",
    "| C2C | REQUIREMENTS |"
]
```
> ⚠️ Names must match WhatsApp **exactly** including spaces, pipes, commas

### 4. Run the scraper
```bash
python main.py
```
- First run: Chrome opens, scan QR code with your phone
- Future runs: auto-logs in from saved session

### 5. Open the dashboard
```bash
python app.py
```
Visit **http://127.0.0.1:5000**

---

## 📅 Daily Usage

```bash
# Scrape new jobs (run in one terminal)
python main.py

# View dashboard (run in another terminal)
python app.py
# → open http://127.0.0.1:5000
```

---

## ⚙️ config.json Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `groups` | `[]` | WhatsApp group names (exact match) |
| `max_messages_per_group` | `200` | Messages to read per group. Use `500` for larger windows |
| `only_last_hours` | `24` | Only keep messages from last N hours. `72` recommended |
| `headless` | `false` | `true` = Chrome runs in background (use after QR scanned) |
| `login_timeout_seconds` | `120` | Seconds to wait for QR scan |
| `session_dir` | `./wa_session` | Where WhatsApp login is saved |
| `job_keywords` | `[...]` | Any one match = job post |
| `visa_types` | `[...]` | Visa types to extract |
| `skills` | `[...]` | Tech skills to detect |
| `contract_types` | `[...]` | C2C, W2, Contract etc |

**Sweet spot settings:**
```json
"max_messages_per_group": 500,
"only_last_hours": 72,
"headless": true
```

---

## 📁 Output Files

| File | What it contains | Use it for |
|------|-----------------|------------|
| `jobs.json` | Extracted job data | Main data — shows in dashboard |
| `raw_messages.json` | All messages with `is_job` flag | Manual verification |
| `review.json` | All messages + why kept or filtered | Audit false positives/negatives |
| `seen_jobs.json` | Hashes of processed messages | Dedup — delete to re-scrape everything |
| `scraper.log` | Full timestamped run log | Debugging |

### How to verify no jobs were missed
1. Open `review.json`
2. Look for `"status": "spam"` entries
3. Check `filter_reason` — if a real job was blocked, it shows the exact pattern
4. Fix by editing `job_keywords` or `spam_patterns` in `config.json`

---

## 🌐 Deployment

### Architecture Reality

> **The scraper (`main.py`) MUST run on your local machine.**
> It needs a real Chrome browser and your WhatsApp login session.
> WhatsApp blocks cloud server IPs.
> 
> **The dashboard (`app.py`) CAN be deployed to the cloud.**

```
Your Laptop                    Cloud
───────────                    ─────
main.py runs          →        app.py on Render.com
scrapes WhatsApp               shows dashboard
saves output/jobs.json         reads jobs.json
                               (need sync — see below)
```

---

### Option 1: Local Only (Simplest — no cloud needed)

Run both files on your laptop. Share with team using **ngrok**:

```bash
# Terminal 1
python app.py

# Terminal 2
ngrok http 5000
# Gives you a public URL like https://abc123.ngrok.io
```

**Cost: Free | Setup: 5 minutes**

---

### Option 2: Dashboard on Render.com (Free Tier)

Deploy the dashboard publicly. Scraper still runs locally and uploads data.

#### Step 1: Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOURNAME/whatsapp-c2c-scraper.git
git push -u origin main
```

#### Step 2: Deploy to Render
1. Go to **https://render.com** → Sign up free
2. Click **New** → **Web Service**
3. Connect your GitHub repo
4. Settings:
   - **Name:** `c2c-job-scraper`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python app.py`
   - **Plan:** Free
5. Click **Deploy**

Your dashboard is live at: `https://c2c-job-scraper.onrender.com`

#### Step 3: Sync your local data to Render

Render's free tier has no persistent disk — data resets on redeploy.

**Best solution: Use Supabase as shared database (free)**

See **SUPABASE_SETUP.md** for step-by-step (I will add this when you're ready to integrate).

---

### Option 3: VPS — Full Control ($5/month)

DigitalOcean, Linode, or Vultr. Everything runs on one server.

```bash
# On VPS (Ubuntu):
sudo apt update
sudo apt install python3 python3-pip chromium-browser -y
pip3 install -r requirements.txt
playwright install chromium

# Run dashboard (stays running)
nohup python3 app.py &

# Scraper runs from YOUR LOCAL machine and pushes data to VPS via rsync:
rsync -avz output/ user@your-vps-ip:/home/user/whatsapp/output/
```

---

## 🔒 Security

- `wa_session/` contains your WhatsApp login — **never commit to git**
- `output/` contains scraped messages — **never commit to git**
- Both are in `.gitignore` already
- Use a **secondary WhatsApp number** for scraping
- WhatsApp Terms of Service prohibits scraping — use responsibly

---

## 🐛 Troubleshooting

| Problem | Fix |
|---------|-----|
| Group not found | Check exact name in config.json — spaces/pipes/commas must match |
| QR code not showing | Delete `wa_session/` folder, run `python main.py` again |
| 0 jobs scraped | Increase `only_last_hours` or check if group has recent messages |
| Very few jobs with large time range | Increase `max_messages_per_group` to 500 or 1000 |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` |
| Browser not found | Run `playwright install chromium` |
| Excel export fails | Run `pip install openpyxl` |
| Selectors broken (WhatsApp updated) | Open browser DevTools, share search box HTML with developer |
| Render dashboard shows no jobs | Need Supabase integration — data lives locally only |

---

## 📊 Dashboard Pages

| Page | What it shows |
|------|--------------|
| **⚙ Config** | Edit groups, hours, keywords, visa types — Save to config.json |
| **▶ Scrape** | Start scraper with one click, live log output |
| **📋 Jobs** | Searchable job table with all filters + Excel export |
| **🔍 Review** | Every message with filter reason — audit false positives |

---

## 🔮 Roadmap

- [ ] Supabase integration — cloud database sync
- [ ] Auto-scheduled runs (no manual trigger)
- [ ] Email/Telegram alerts for matching jobs
- [ ] WhatsApp Channels support
- [ ] AI extraction for difficult messages
- [ ] Deduplication across groups
- [ ] Google Sheets export
- [ ] Job expiry (mark stale after 72h)

---

## 📝 License

MIT — free to use and modify.
Built for personal/internal recruiter use.
