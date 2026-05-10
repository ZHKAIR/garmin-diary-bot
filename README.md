# BegovayaKuznitsa_Bot - Garmin Diary Telegram Bot

Telegram bot for Garmin Connect diary – extracts training summaries, interval breakdowns, and pace calculations from your Garmin data.

## Features

- Garmin Connect authentication via OAuth
- Last 10 activities overview with inline buttons
- Compact diary format for interval workouts (e.g. `10x400: 1:31, 1:32, ...`)
- Detailed lap-by-lap analysis with warmup/cooldown/recovery segments
- GPS distance smoothing for track intervals
- Pace calculator
- Webhook mode for cloud deployment (Render, Railway, etc.)
- Polling mode for local development

## Quick Start (Local)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
python telegram_bot.py
```

## Deploy to Render

See [DEPLOY_RENDER.md](DEPLOY_RENDER.md) for step-by-step instructions.

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Main menu |
| `/last` | Latest running activity in diary format |
| `/format <id>` | Format a specific activity by ID |
| `/help` | Help and usage info |

## License

MIT
