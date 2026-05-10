# Deploying BegovayaKuznitsa_Bot on Render (Free Tier)

## Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A [Render](https://render.com) account (free tier works)

## Step-by-step

### 1. Push code to a Git repository

Render deploys from GitHub/GitLab. Make sure your repo contains at least:

```
telegram_bot.py
requirements.txt
Dockerfile
```

### 2. Create a new Web Service on Render

1. Go to **Dashboard > New > Web Service**
2. Connect your repository
3. Configure:
   - **Name**: `begovaya-kuznitsa-bot`
   - **Region**: closest to you (e.g. Frankfurt)
   - **Runtime**: **Docker**
   - **Instance Type**: **Free**

### 3. Set environment variables

In the Render service settings, add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your bot token from BotFather |
| `WEBHOOK_URL` | `https://begovaya-kuznitsa-bot.onrender.com` (your Render URL) |
| `PORT` | `10000` (Render free tier uses port 10000) |

### 4. Deploy

Push to your main branch - Render auto-deploys on each push.

### 5. Verify

1. Check Render logs - you should see `Starting webhook mode on port ...`
2. Open your bot in Telegram, send `/start`
3. The bot should respond with the main menu
