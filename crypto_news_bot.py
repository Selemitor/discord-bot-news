# Plik: crypto_news_bot.py
# Osobny bot Discord do filtrowania i publikowania ważnych newsów krypto.

import asyncio
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from threading import Thread
from zoneinfo import ZoneInfo

import discord
import feedparser
import requests
from discord.ext import commands, tasks
from flask import Flask


app = Flask(__name__)


@app.route("/")
def home():
    return "Crypto News Desk Bot jest aktywny!"


@app.route("/healthz")
def health_check():
    return "OK", 200


BOT_TOKEN = os.environ.get("NEWS_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
NEWS_CHANNEL_ID = int(os.environ.get("NEWS_CHANNEL_ID", "0"))
ALERT_SCORE_THRESHOLD = int(os.environ.get("NEWS_ALERT_SCORE_THRESHOLD", "70"))
DIGEST_HOURS = [int(h) for h in os.environ.get("NEWS_DIGEST_HOURS", "9,21").split(",") if h.strip()]
FEED_POLL_MINUTES = int(os.environ.get("NEWS_FEED_POLL_MINUTES", "10"))
STATE_FILE = Path(os.environ.get("NEWS_STATE_FILE", "crypto_news_state.json"))
TZ_POLAND = ZoneInfo("Europe/Warsaw")


DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.binance.com/en/support/announcement/rss",
]

NEWS_FEEDS = [
    feed.strip()
    for feed in os.environ.get("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)).split(",")
    if feed.strip()
]


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


STATE = {"seen": {}, "digest_items": [], "last_digest": {}}


CATEGORY_RULES = {
    "Regulacje": ["sec", "cftc", "lawsuit", "regulation", "regulator", "court", "ban", "approved", "approval"],
    "ETF": ["etf", "blackrock", "fidelity", "ishares", "spot bitcoin", "spot ethereum"],
    "Giełdy": ["binance", "coinbase", "kraken", "okx", "bybit", "listing", "delisting", "withdrawal", "deposit"],
    "Hack / exploit": ["hack", "exploit", "breach", "stolen", "drained", "phishing", "attack", "vulnerability"],
    "Makro": ["fed", "fomc", "cpi", "inflation", "rates", "powell", "treasury", "dollar"],
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "eth", "staking"],
    "Stablecoiny": ["stablecoin", "usdt", "usdc", "tether", "circle"],
    "Altcoiny": ["solana", "xrp", "bnb", "cardano", "dogecoin", "altcoin", "token"],
}


SCORE_RULES = [
    (35, ["hack", "exploit", "stolen", "drained", "insolvency", "bankruptcy"]),
    (30, ["sec", "cftc", "lawsuit", "court", "settlement", "regulation"]),
    (28, ["etf", "blackrock", "fidelity", "approval", "rejected"]),
    (24, ["binance", "coinbase", "kraken", "okx", "bybit", "delisting", "listing"]),
    (20, ["fed", "fomc", "cpi", "inflation", "interest rates", "powell"]),
    (18, ["bitcoin", "btc", "ethereum", "eth"]),
    (12, ["stablecoin", "usdt", "usdc", "tether", "circle"]),
    (-20, ["price prediction", "sponsored", "giveaway", "presale", "meme coin presale"]),
]


def load_state():
    global STATE
    try:
        if STATE_FILE.exists():
            STATE = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Błąd ładowania stanu news bota: {exc}")


def save_state():
    try:
        STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Błąd zapisu stanu news bota: {exc}")


def cleanup_state():
    cutoff = time.time() - 7 * 24 * 60 * 60
    STATE["seen"] = {key: value for key, value in STATE.get("seen", {}).items() if value >= cutoff}
    STATE["digest_items"] = [
        item for item in STATE.get("digest_items", [])
        if item.get("timestamp", 0) >= cutoff
    ][-200:]


def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def item_hash(title, link):
    base = f"{normalize_text(title).lower()}|{normalize_text(link).lower()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def classify_news(title, summary):
    text = f"{title} {summary}".lower()
    categories = []
    score = 20
    for category, keywords in CATEGORY_RULES.items():
        if any(keyword in text for keyword in keywords):
            categories.append(category)
    for points, keywords in SCORE_RULES:
        if any(keyword in text for keyword in keywords):
            score += points
    if not categories:
        categories.append("Rynek")
    return min(100, max(0, score)), categories[:3]


def impact_summary(title, categories, score):
    category_text = ", ".join(categories)
    if score >= 85:
        impact = "wysoki wpływ potencjalny"
    elif score >= 70:
        impact = "istotny wpływ potencjalny"
    elif score >= 50:
        impact = "umiarkowany wpływ potencjalny"
    else:
        impact = "niski wpływ potencjalny"

    assets = []
    lowered = title.lower()
    for symbol, keywords in {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "BNB": ["binance", "bnb"],
        "SOL": ["solana", "sol"],
        "XRP": ["xrp", "ripple"],
        "Stablecoiny": ["stablecoin", "usdt", "usdc", "tether", "circle"],
    }.items():
        if any(keyword in lowered for keyword in keywords):
            assets.append(symbol)

    return (
        f"**Kategoria:** {category_text}\n"
        f"**Ocena ważności:** {score}/100 ({impact})\n"
        f"**Aktywa do obserwacji:** {', '.join(assets) if assets else 'szeroki rynek'}\n"
        "To nie jest porada inwestycyjna."
    )


def fetch_feed_items():
    items = []
    headers = {"User-Agent": "CryptoNewsDeskBot/1.0"}
    for feed_url in NEWS_FEEDS:
        try:
            response = requests.get(feed_url, headers=headers, timeout=15)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            source = parsed.feed.get("title", feed_url)
            for entry in parsed.entries[:10]:
                title = normalize_text(entry.get("title", ""))
                link = normalize_text(entry.get("link", ""))
                summary = normalize_text(entry.get("summary", ""))
                if not title or not link:
                    continue
                score, categories = classify_news(title, summary)
                items.append({
                    "id": item_hash(title, link),
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source,
                    "score": score,
                    "categories": categories,
                    "timestamp": time.time(),
                })
        except Exception as exc:
            print(f"Błąd pobierania feedu {feed_url}: {exc}")
    return sorted(items, key=lambda item: item["score"], reverse=True)


def remember_for_digest(item):
    digest_items = STATE.setdefault("digest_items", [])
    if not any(existing.get("id") == item["id"] for existing in digest_items):
        digest_items.append(item)
        STATE["digest_items"] = digest_items[-200:]


async def publish_alert(channel, item):
    embed = discord.Embed(
        title=item["title"][:256],
        url=item["link"],
        description=impact_summary(item["title"], item["categories"], item["score"]),
        color=discord.Color.red() if item["score"] >= 85 else discord.Color.orange()
    )
    embed.add_field(name="Źródło", value=item["source"][:1024], inline=True)
    embed.set_footer(text=f"Crypto News Desk | Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    await channel.send(embed=embed)


async def run_feed_scan(publish=True):
    if not NEWS_CHANNEL_ID:
        print("Brak NEWS_CHANNEL_ID. Pomijam publikację newsów.")
        return []
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        print(f"Nie znaleziono kanału NEWS_CHANNEL_ID={NEWS_CHANNEL_ID}.")
        return []
    cleanup_state()
    new_items = []
    for item in await asyncio.to_thread(fetch_feed_items):
        if item["id"] in STATE.get("seen", {}):
            continue
        STATE.setdefault("seen", {})[item["id"]] = time.time()
        remember_for_digest(item)
        new_items.append(item)
        if publish and item["score"] >= ALERT_SCORE_THRESHOLD:
            await publish_alert(channel, item)
            await asyncio.sleep(1)
    save_state()
    return new_items


def build_digest_items():
    since = time.time() - 12 * 60 * 60
    items = [item for item in STATE.get("digest_items", []) if item.get("timestamp", 0) >= since]
    return sorted(items, key=lambda item: item["score"], reverse=True)[:8]


async def publish_digest(channel):
    items = build_digest_items()
    if not items:
        await channel.send("Brak istotnych newsów krypto do digestu.")
        return
    description_lines = []
    for index, item in enumerate(items, 1):
        categories = ", ".join(item["categories"])
        description_lines.append(
            f"**{index}. [{item['title']}]({item['link']})**\n"
            f"`{item['score']}/100` | {categories} | {item['source']}"
        )
    embed = discord.Embed(
        title="Crypto News Digest",
        description="\n\n".join(description_lines)[:4096],
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Digest | Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    await channel.send(embed=embed)


@bot.event
async def on_ready():
    print(f"Crypto News Desk zalogowany jako {bot.user}")
    load_state()
    if not feed_scan_loop.is_running():
        feed_scan_loop.start()
    if not digest_loop.is_running():
        digest_loop.start()
    try:
        synced = await bot.tree.sync()
        print(f"Zsynchronizowano {len(synced)} komend news bota.")
    except Exception as exc:
        print(f"Błąd synchronizacji komend news bota: {exc}")


@tasks.loop(minutes=FEED_POLL_MINUTES)
async def feed_scan_loop():
    await run_feed_scan(publish=True)


@tasks.loop(minutes=1)
async def digest_loop():
    if not NEWS_CHANNEL_ID:
        return
    now = datetime.datetime.now(TZ_POLAND)
    if now.minute != 0 or now.hour not in DIGEST_HOURS:
        return
    key = now.strftime("%Y-%m-%d-%H")
    if STATE.get("last_digest", {}).get("key") == key:
        return
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        return
    await publish_digest(channel)
    STATE["last_digest"] = {"key": key, "timestamp": time.time()}
    save_state()


@bot.tree.command(name="news_scan", description="Ręcznie skanuje źródła newsów i publikuje ważne alerty.")
async def slash_news_scan(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    items = await run_feed_scan(publish=True)
    await interaction.followup.send(f"Przeskanowano źródła. Nowe newsy: {len(items)}.", ephemeral=True)


@bot.tree.command(name="news_digest", description="Publikuje ręczny digest newsów krypto.")
async def slash_news_digest(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if not NEWS_CHANNEL_ID:
        await interaction.followup.send("Brak NEWS_CHANNEL_ID.", ephemeral=True)
        return
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        await interaction.followup.send("Nie znaleziono kanału newsowego.", ephemeral=True)
        return
    await publish_digest(channel)
    await interaction.followup.send("Digest opublikowany.", ephemeral=True)


def run_discord_bot_sync():
    if not BOT_TOKEN:
        print("Brak NEWS_BOT_TOKEN/BOT_TOKEN. News bot nie wystartuje.")
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.start(BOT_TOKEN))
    except Exception as exc:
        print(f"Krytyczny błąd news bota: {exc}")
    finally:
        loop.run_until_complete(bot.close())
        loop.close()


print("Inicjalizacja wątku Crypto News Desk...")
bot_thread = Thread(target=run_discord_bot_sync)
bot_thread.start()
