# GhostTalk Bot — Render Deployment Guide

---

## Step 1 — GitHub Repo Banao

1. GitHub pe jaao → https://github.com/new
2. Repo name: `ghosttalk-bot` (ya kuch bhi)
3. **Private** rakho (token safe rahega)
4. Create repository karo

Apne PC pe ek folder banao aur ye 3 files daalo:
```
ghosttalk-bot/
├── bot.py
├── requirements.txt
└── README.md  (optional)
```

Terminal mein:
```bash
git init
git add .
git commit -m "GhostTalk v6.0"
git branch -M main
git remote add origin https://github.com/<TERA_USERNAME>/ghosttalk-bot.git
git push -u origin main
```

---

## Step 2 — Render pe Deploy

1. Jaao → https://render.com → Sign up (free)
2. Dashboard → **New +** → **Web Service**
3. **Connect GitHub** → apna `ghosttalk-bot` repo select karo

### Settings jo fill karni hain:

| Field | Value |
|-------|-------|
| **Name** | ghosttalk-bot |
| **Region** | Singapore (India ke paas) |
| **Branch** | main |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Instance Type** | Free |

### Environment Variables (IMPORTANT):

Render dashboard mein **Environment** tab pe jaao, ye variables add karo:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | `8991129605:AAH6_yZiyT4sq_JU57HFIYChSTiddIPvv9Q` |
| `ADMIN_ID` | `8361006824` |
| `PORT` | `10000` |

> ⚠️ BOT_TOKEN GitHub pe mat daalo — sirf Render env vars mein daalo

4. **Create Web Service** karo
5. Deploy hoga — 2-3 minute lagenge
6. Tera URL milega: `https://ghosttalk-bot.onrender.com`

---

## Step 3 — UptimeRobot Setup (Bot Active Rakhega)

Render free tier mein 15 min baad bot so jaata hai — UptimeRobot
har 5 minute mein ping karega taaki jaag ta rahe.

1. Jaao → https://uptimerobot.com → Free account banao
2. **Add New Monitor** karo
3. Settings:

| Field | Value |
|-------|-------|
| **Monitor Type** | HTTP(s) |
| **Friendly Name** | GhostTalk Bot |
| **URL** | `https://ghosttalk-bot.onrender.com/ping` |
| **Monitoring Interval** | 5 minutes |

4. **Create Monitor** karo

Ab `/ping` endpoint pe hit aayega har 5 min → Render jaagta rahega → Bot alive!

---

## Step 4 — Verify Karo

Deploy hone ke baad:

1. Render logs check karo — ye lines dikhni chahiye:
```
GhostTalk Bot v6.0 FINAL starting...
Flask running on port 10000
```

2. Telegram pe `/start` bhejo apne bot ko
3. UptimeRobot mein "Up" status dikhega

---

## Troubleshooting

**Bot respond nahi kar raha?**
- Render logs mein error dekho
- `BOT_TOKEN` env var sahi hai?
- Build command sahi chala?

**"Application failed to respond"?**
- `PORT` env var `10000` hai?
- `Start Command` mein `python bot.py` hai?

**UptimeRobot "Down" dikha raha hai?**
- URL mein `/ping` lagaya?
- Render deploy complete hua?

---

## Local Run (PC pe test karna ho)

```bash
# .env file banao (optional)
BOT_TOKEN=tera_token python bot.py

# ya directly
python bot.py
```

Local pe PORT=5000 default rahega — Flask `localhost:5000` pe chalega.

---

## File Structure Summary

```
ghosttalk-bot/
├── bot.py              ← main bot code
├── requirements.txt    ← pyTelegramBotAPI==4.21.0, Flask==3.1.0
└── data/               ← auto-create hoga (SQLite DB yahan save hogi)
```

> Note: Render free tier mein `data/` folder restart pe wipe ho sakta hai.
> Permanent DB chahiye to Render pe "Disk" add karo ya SQLite ki jagah
> Render ki free PostgreSQL use karo. Abhi ke liye SQLite theek hai testing ke liye.
