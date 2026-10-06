# Stealth Quest Completer

Discord bot that auto-completes Discord quests using the user's token.

## How it works

1. User types `,autoquest` in the server where the bot is
2. If no token is stored, the bot **DMs** the user asking for their Discord token
3. User replies to the DM with their token (stored in RAM only, never on disk)
4. User types `,autoquest` again in the server
5. Bot completes all available quests automatically
6. Token is deleted from memory after 1 hour or with `,clear`

## Token security

- Token is requested via **DM** (private, not visible in server)
- Stored in **memory only** (RAM dict keyed by user_id)
- **Never logged** — no log file contains the token
- **Never written to disk** — no database, no file
- **Never shared** — no external API calls except Discord's own API
- Auto-expires after 1 hour of inactivity
- `,clear` deletes it immediately

## Setup

### 1. Create the bot on Discord

1. Go to https://discord.com/developers/applications
2. Click "New Application" → give it a name
3. Go to "Bot" → copy the token
4. Enable: **Message Content Intent**, **Server Members Intent**, **Presence Intent**
5. Go to "OAuth2 → URL Generator" → select `bot` + `applications.commands`
6. Permissions: Send Messages, Read Message History, Embed Links
7. Invite the bot to your server using the generated URL

### 2. Install and run

```bash
git clone https://github.com/requiemzc/stealth-quest-completer.git
cd stealth-quest-completer
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your bot token:
```bash
cp .env.example .env
# Edit .env and paste your bot token
```

Run:
```bash
python bot.py
```

### 3. Host 24/7

You can host the bot on:
- **VPS** (Contabo, Hetzner, DigitalOcean) — cheapest
- **Raspberry Pi** at home
- **Railway.app** (free tier)
- **Render.com** (free tier)

## Commands

| Command | Description | Where |
|---|---|---|
| `,autoquest` | Complete all your quests (DMs you for token if you don't have one) | Server |
| `,quests` | List your pending quests | Server |
| `,status` | Check if you have a token stored | Server |
| `,clear` | Delete your token from memory | Server or DM |
| `,help` | Show this help message | Server |

## Usage flow

```
User: ,autoquest
Bot: I've sent you a DM to enter your token.

(DM from bot)
Bot: Enter your Discord token...
User: [pastes their token]
Bot: Token stored in memory. Now go to the server and type ,autoquest.

(Server)
User: ,autoquest
Bot: Starting quest completion for @user...
Bot: Connected as username123
Bot: Found 3 quests. Completing...
Bot: Quest 1 — done!
Bot: Quest 2 — done!
Bot: Quest 3 — done!
Bot: Summary: 3 completed | 0 already done | 0 failed
```

## ⚠️ Disclaimer

Discord has been enforcing against quest automation since April 2026. Using this tool may result in quest restrictions (up to 14 days) or Account Standing violations on your Discord account. Use at your own risk.

## License

MIT
