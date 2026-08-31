# Emotion Fetchers

Standalone fetchers for crypto social sentiment data from Binance Square, OKX Orbit (Planet), and X/Twitter.

## Scope

- JSON API clients for Binance Square and OKX Orbit
- X/Twitter SearchTimeline integration through an external `twitter-scraper` checkout
- Optional XHunt ranking client and local follower scoring
- CLI and a small local demo server
- No Playwright or Chromium dependency

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
pip install -r fetch/requirements.txt
```

Run from this repository root:

```bash
python -m fetch binance BTC --mode hot --pretty
python -m fetch okx BTC --mode latest --pretty
python -m fetch both BTC --limit 10 --pretty
```

Twitter requires a separately installed `twitter-scraper` checkout and an authenticated session supplied through `TWITTER_COOKIE`, `TWITTER_ACCOUNTS_FILE`, or that checkout's local configuration. Set `TWITTER_SCRAPER_ROOT` to its path. Credentials are never bundled, persisted in this repository, or printed by this project.

Optional settings include `RESIDENTIAL_PROXY_POOL`, `OKX_REGION`, `OKX_SITE_CODE`, `OKX_ENTITY`, `OKX_X_SITE_INFO`, `CAPTCHA_API_KEY`, and `XHUNT_TOKEN`. Set them in the shell or a local untracked `.env`; do not commit secrets.

## Privacy and responsible use

This repository contains source code only. Do not commit cookies, API keys, account exports, raw captures, browser profiles, logs, or personal identifiers. Respect each platform's terms, rate limits, robots policies, and applicable privacy laws.

## License

The included `vendor/follower-standard` component retains its upstream license. Other files are provided under the MIT License; see `LICENSE`.
