# LOKEN Buy/Sell → Telegram Relay

Receives TradingView webhook alerts from the `LOKEN Buy+Sell` combined
indicator and forwards them to your Telegram chat.

Architecture:

```
TradingView alert (webhook)  --POST-->  Render web service (this repo)  --sendMessage-->  Telegram
```

## 1. Push this folder to GitHub

```bash
git init
git add .
git commit -m "LOKEN Telegram relay"
git branch -M main
git remote add origin https://github.com/<your-username>/loken-telegram-relay.git
git push -u origin main
```

## 2. Deploy to Render

1. Go to [render.com](https://render.com) → **New** → **Blueprint**.
2. Connect the GitHub repo you just pushed. Render will read `render.yaml`
   automatically and create the web service.
   - No blueprint access? Use **New → Web Service** instead, point it at
     the repo, and set:
     - **Build command:** `pip install -r requirements.txt`
     - **Start command:** `gunicorn app:app`
3. In the service's **Environment** tab, set:
   - `TELEGRAM_BOT_TOKEN` — your existing bot token (from @BotFather /
     your NSE Dashboard project's `.env`)
   - `TELEGRAM_CHAT_ID` — your existing chat/group ID
   - `WEBHOOK_SECRET` — any random string you make up, e.g.
     `loken-9f3a2c`. This stops randoms from spamming your Telegram if
     they ever guess your Render URL.
4. Deploy. Once live, note your service URL, e.g.
   `https://loken-telegram-relay.onrender.com`.
5. Confirm it's alive: open that URL in a browser — you should see
   `{"status": "ok", ...}`.

> **Free-tier note:** Render's free web services spin down after 15
> minutes of no traffic and take ~30-60s to wake on the next request.
> The very first alert after a quiet period may arrive a little late.
> If that's a problem for you, upgrade to a paid instance ($7/mo
> "Starter") which stays always-on, or ping the health-check URL every
> few minutes with a free uptime monitor (e.g. UptimeRobot / cron-job.org)
> to keep it warm.

## 3. Set up ONE alert in TradingView

The Pine script already fires a JSON `alert()` for every signal (Buy A,
Buy B, Sell A, Sell A+, Sell A++, Sell B+, Sell E, Sell E+), so you only
need **one** TradingView alert to catch all of them:

1. Open the `LOKEN Buy+Sell` indicator on any chart.
2. Click **Alert** (clock icon) → **Create Alert**.
3. **Condition:** select the indicator, then choose **"Any alert() function call"**.
4. **Expiration:** set to "Open-ended" (or your plan's max).
5. **Webhook URL:** paste `https://<your-render-url>/webhook`
   (must be checked/enabled — this requires a paid TradingView plan;
   webhooks aren't available on the free plan).
6. **Message:** leave as the default `{{strategy.order.alert_message}}`
   placeholder is NOT needed here — TradingView automatically sends the
   exact text from the `alert()` call as the webhook body, so you can
   leave the message box as-is or clear it; it's ignored for
   `alert()`-based conditions.
7. If you set `WEBHOOK_SECRET` above, you need it embedded in the JSON
   payload — see the note below on adding it to the Pine script.
8. Save. This ONE alert now fires for every signal on that chart/symbol.

> **Repeat per symbol.** TradingView alerts are per chart/symbol, so
> you'll need to repeat step 3 (Create Alert → same steps) on each stock
> you want signals for — ICICI Bank, IHCL, Adani Green, etc.

## 4. Set the shared secret (optional but recommended)

The Pine script already has a **"Webhook Secret"** input (under group
**"LOKEN - Telegram Webhook"**) — set it to the same random string you
used for `WEBHOOK_SECRET` in step 2.3. It gets embedded into every
alert's JSON automatically; no code editing needed on your end.

Leave both blank if you don't want this check (not recommended for a
public Render URL, since anyone who finds it could spam your Telegram).

## 5. Test end-to-end

```bash
curl -X POST https://<your-render-url>/webhook \
  -H "Content-Type: application/json" \
  -d '{"signal":"Sell E+","ticker":"ICICIBANK","exchange":"NSE","price":"1420.50","time":"2026-08-08T10:30:00Z"}'
```

You should get a message in Telegram within a couple seconds.

## Files

- `app.py` — the Flask relay (health check at `/`, webhook receiver at `/webhook`)
- `requirements.txt` — Python dependencies
- `render.yaml` — Render Blueprint config (env vars, build/start commands)
