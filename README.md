# AYNIMO VM & Marketing bot

Every **Monday** the weekly `VM_MARKETING` Excel is sent to the bot → the bot posts every product
(photo, comment, stock per store) into **one group**. VM staff confirm VM posts, Marketing staff confirm
Marketing posts with **✅ Bajarildi**. Every **Sunday** the bot posts a report: how much each department did,
what is still pending, who did what (+ Excel file).

```
Monday:  admin ──Excel──▶ bot ──preview──▶ "🚀 Hammasini yuborish" ──▶ group (VM + Marketing posts)
Week:    VM team ✅ VM posts · Marketing team ✅ Marketing posts
Sunday:  bot ──▶ group: "📊 Haftalik hisobot" + Excel
```

## Project layout

| Path | What |
|---|---|
| `app.py` | entry point (hosting panel starts this) |
| `vm_marketing_bot/` | the bot: `excel.py` (reads template), `captions.py` (post design), `sender.py` (sending), `handlers.py` (Telegram), `reports.py` (weekly report), `storage.py` (SQLite) |
| `tests/` | automatic tests (run by GitHub on every push) |
| `scripts/deploy_pterodactyl.py` | uploads code to bot-hosting.net and restarts the bot |
| `.github/workflows/ci-cd.yml` | CI (lint + tests) and CD (deploy on push to `main`) |
| `.env.example` | all settings — copy to `.env` |

## Settings (`.env`)

See [.env.example](.env.example). The important ones:

| Key | Meaning |
|---|---|
| `BOT_TOKEN` | from @BotFather |
| `WEBDAV_PASSWORD` | product photo server password |
| `GROUP_CHAT_ID` | the group for all posts and the Sunday report (`/chatid` inside the group) |
| `ADMIN_IDS` | who may upload/send |
| `VM_TEAM_IDS`, `MARKETING_TEAM_IDS` | who may confirm each department's posts (empty = anyone) |
| `REPORT_DAY`, `REPORT_TIME`, `TIMEZONE` | default `sunday`, `18:00`, `Asia/Tashkent` |

Everyone can see their own ID by sending `/chatid` to the bot.

## Run locally

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest        # tests
.venv\Scripts\python.exe app.py           # run the bot
```

## Hosting + CI/CD (GitHub → bot-hosting.net)

**One-time setup**

1. **bot-hosting.net**: create a Python server (3.11+). Startup file `app.py`, requirements file `requirements.txt`.
   In the file manager create `.env` (from `.env.example`) — the token stays only on the server.
2. **Panel API key**: panel → Account → API Credentials → create key (starts with `ptlc_`).
   Server ID = the short code in the server URL (`.../server/abcd1234`).
3. **GitHub**: create a **private** repository and push this folder (see below).
4. GitHub repo → Settings → Secrets and variables → Actions → New repository secret:
   - `PANEL_URL` — panel address, e.g. `https://control.bot-hosting.net` (whatever is in your browser)
   - `PANEL_API_KEY` — the `ptlc_...` key
   - `SERVER_ID` — e.g. `abcd1234`

**Every change after that**

```powershell
git add .
git commit -m "what changed"
git push
```
GitHub → Actions: **Lint & test** runs → if green, **Deploy** uploads the code and restarts the bot.
If tests fail, nothing is deployed. Avoid pushing on Monday while products are being sent (restart stops sending).

**First push**

```powershell
git init
git add .
git commit -m "VM & Marketing bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## Excel rules

| Column | Notes |
|---|---|
| `Department` | **Required.** `VM` or `MARKETING` (`VM/MARKETING` → the product is posted for both). |
| `Comment` | Note shown on the post. A repeated `VM`/`MARKETING` at the start is removed. (Old files without `Department`: the department is read from the start of `Comment`.) |
| `Модель` / `Наименование` | at least one required |
| `Бренд`, `Категория`, `Подкатегория`, `PRICE`, `PHOTO_PATH`, `stock` | optional |
| `s01` … `s10`, `w1` | store / warehouse stock; new stores (`s11`…) are picked up automatically |

`-` or empty = "no data". Header names are case/space-insensitive. Store regions: `vm_marketing_bot/config.py → STORE_REGIONS`.

## Data on the server

`vm_marketing_data/` — `vm_marketing.db` (posts, confirmations, reports), uploaded Excel files, `bot.log`.
Deploys never touch it. Download `vm_marketing.db` from the panel now and then as a backup.
