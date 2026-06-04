# Plik: crypto_news_bot.py
# Osobny bot Discord do filtrowania i publikowania ważnych newsów krypto.

import asyncio
import datetime
import hashlib
import html
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
MAX_PUBLISH_PER_SCAN = int(os.environ.get("NEWS_MAX_PUBLISH_PER_SCAN", "5"))
TRANSLATE_TITLES = os.environ.get("NEWS_TRANSLATE_TITLES", "false").lower() in ("1", "true", "yes", "on")
DEEPL_API_KEY = os.environ.get("DEEPL_API_KEY")
DEEPL_API_URL = os.environ.get("DEEPL_API_URL", "https://api-free.deepl.com/v2/translate")
DIGEST_HOURS = [int(h) for h in os.environ.get("NEWS_DIGEST_HOURS", "9,21").split(",") if h.strip()]
FEED_POLL_MINUTES = int(os.environ.get("NEWS_FEED_POLL_MINUTES", "10"))
STATE_FILE = Path(os.environ.get("NEWS_STATE_FILE", "crypto_news_state.json"))
TZ_POLAND = ZoneInfo("Europe/Warsaw")


DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://cryptoslate.com/feed/",
    "https://bitcoinmagazine.com/.rss/full/",
]

NEWS_FEEDS = [
    feed.strip()
    for feed in os.environ.get("NEWS_FEEDS", ",".join(DEFAULT_FEEDS)).split(",")
    if feed.strip()
]


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


STATE = {"seen": {}, "digest_items": [], "last_digest": {}}
FEED_STATS = {"last_scan": None, "feeds": [], "fetched": 0}
TRANSLATION_CACHE = {}


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


def get_entry_image_url(entry):
    media_content = entry.get("media_content") or []
    for media in media_content:
        url = media.get("url")
        media_type = media.get("type", "")
        if url and ("image" in media_type or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I)):
            return url

    media_thumbnail = entry.get("media_thumbnail") or []
    for media in media_thumbnail:
        if media.get("url"):
            return media["url"]

    enclosures = entry.get("enclosures") or []
    for enclosure in enclosures:
        url = enclosure.get("href") or enclosure.get("url")
        enc_type = enclosure.get("type", "")
        if url and ("image" in enc_type or re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.I)):
            return url

    html_source = entry.get("summary", "") or entry.get("description", "")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_source, re.I)
    if match:
        return html.unescape(match.group(1))
    return None


def translate_title(title):
    if not TRANSLATE_TITLES or not DEEPL_API_KEY:
        return title
    if title in TRANSLATION_CACHE:
        return TRANSLATION_CACHE[title]

    try:
        response = requests.post(
            DEEPL_API_URL,
            data={
                "auth_key": DEEPL_API_KEY,
                "text": title,
                "target_lang": "PL",
            },
            timeout=10,
        )
        response.raise_for_status()
        translated = response.json()["translations"][0]["text"]
        translated = normalize_text(translated)
        TRANSLATION_CACHE[title] = translated or title
        return TRANSLATION_CACHE[title]
    except Exception as exc:
        print(f"Błąd tłumaczenia tytułu DeepL: {exc}")
        return title


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
    global FEED_STATS
    items = []
    feed_stats = []
    headers = {"User-Agent": "CryptoNewsDeskBot/1.0"}
    for feed_url in NEWS_FEEDS:
        try:
            response = requests.get(feed_url, headers=headers, timeout=15)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            source = parsed.feed.get("title", feed_url)
            entry_count = len(parsed.entries)
            feed_stats.append({"url": feed_url, "source": source, "entries": entry_count, "status": "ok"})
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
                    "image_url": get_entry_image_url(entry),
                    "score": score,
                    "categories": categories,
                    "timestamp": time.time(),
                })
        except Exception as exc:
            feed_stats.append({"url": feed_url, "source": feed_url, "entries": 0, "status": f"error: {exc}"})
            print(f"Błąd pobierania feedu {feed_url}: {exc}")
    FEED_STATS = {
        "last_scan": datetime.datetime.now(TZ_POLAND).strftime("%Y-%m-%d %H:%M:%S"),
        "feeds": feed_stats,
        "fetched": len(items),
    }
    return sorted(items, key=lambda item: item["score"], reverse=True)


def remember_for_digest(item):
    digest_items = STATE.setdefault("digest_items", [])
    if not any(existing.get("id") == item["id"] for existing in digest_items):
        digest_items.append(item)
        STATE["digest_items"] = digest_items[-200:]


async def publish_alert(channel, item):
    link = item["link"]
    if not link.startswith(("http://", "https://")):
        link = None
    display_title = await asyncio.to_thread(translate_title, item["title"])
    embed = discord.Embed(
        title=display_title[:256],
        url=link,
        description=impact_summary(item["title"], item["categories"], item["score"]),
        color=discord.Color.red() if item["score"] >= 85 else discord.Color.orange()
    )
    if display_title != item["title"]:
        embed.add_field(name="Oryginalny tytuł", value=item["title"][:1024], inline=False)
    embed.add_field(name="Źródło", value=item["source"][:1024], inline=True)
    image_url = item.get("image_url")
    if image_url and image_url.startswith(("http://", "https://")):
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Crypto News Desk | Europe/Warsaw | {datetime.datetime.now(TZ_POLAND).strftime('%Y-%m-%d %H:%M')}")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        text = (
            f"**{item['title']}**\n"
            f"{impact_summary(item['title'], item['categories'], item['score'])}\n"
            f"Źródło: {item['source']}\n{item['link']}"
        )
        await channel.send(text[:1900])


def get_channel_access_error(channel):
    if not channel:
        return "Nie znaleziono kanału newsowego. Sprawdź `NEWS_CHANNEL_ID`."
    if not getattr(channel, "guild", None):
        return None

    member = channel.guild.get_member(bot.user.id) if bot.user else None
    if not member:
        return "Bot nie jest członkiem serwera, na którym znajduje się kanał newsowy."

    permissions = channel.permissions_for(member)
    missing = []
    if not permissions.view_channel:
        missing.append("View Channel")
    if not permissions.send_messages:
        missing.append("Send Messages")
    if not permissions.embed_links:
        missing.append("Embed Links")

    if missing:
        return "Bot nie ma wymaganych uprawnień na kanale newsowym: " + ", ".join(missing) + "."
    return None


async def run_feed_scan(publish=True, publish_all=False, max_publish=MAX_PUBLISH_PER_SCAN, channel_override=None):
    if channel_override:
        channel = channel_override
    elif NEWS_CHANNEL_ID:
        channel = bot.get_channel(NEWS_CHANNEL_ID)
    else:
        channel = None

    if not channel:
        print("Brak NEWS_CHANNEL_ID. Pomijam publikację newsów.")
        return {"fetched": 0, "new": 0, "published": 0, "reason": "Nie znaleziono kanału do publikacji. Ustaw NEWS_CHANNEL_ID albo użyj komendy na kanale, gdzie bot ma pisać."}

    access_error = get_channel_access_error(channel)
    if access_error:
        print(f"Problem dostępu do kanału {getattr(channel, 'id', 'unknown')}: {access_error}")
        return {"fetched": 0, "new": 0, "published": 0, "reason": access_error}
    cleanup_state()
    fetched_items = await asyncio.to_thread(fetch_feed_items)
    new_items = []
    published = 0
    failed = 0
    errors = []
    for item in fetched_items:
        if item["id"] in STATE.get("seen", {}):
            continue
        STATE.setdefault("seen", {})[item["id"]] = time.time()
        remember_for_digest(item)
        new_items.append(item)
        if publish and published < max_publish and (publish_all or item["score"] >= ALERT_SCORE_THRESHOLD):
            try:
                await publish_alert(channel, item)
                published += 1
            except Exception as exc:
                failed += 1
                error_text = f"{item['title'][:80]} -> {type(exc).__name__}: {exc}"
                errors.append(error_text)
                print(f"Błąd publikacji alertu: {error_text}")
            await asyncio.sleep(1)
    save_state()
    return {
        "fetched": len(fetched_items),
        "new": len(new_items),
        "published": published,
        "failed": failed,
        "errors": errors[:3],
        "max_publish": max_publish,
        "threshold": ALERT_SCORE_THRESHOLD,
        "reason": None,
    }


def build_digest_items():
    since = time.time() - 12 * 60 * 60
    items = [item for item in STATE.get("digest_items", []) if item.get("timestamp", 0) >= since]
    return sorted(items, key=lambda item: item["score"], reverse=True)[:8]


async def publish_digest(channel):
    access_error = get_channel_access_error(channel)
    if access_error:
        raise PermissionError(access_error)

    items = build_digest_items()
    if not items:
        await channel.send("Brak istotnych newsów krypto do digestu.")
        return
    description_lines = []
    for index, item in enumerate(items, 1):
        categories = ", ".join(item["categories"])
        media_flag = " | image" if item.get("image_url") else ""
        description_lines.append(
            f"**{index}. [{item['title']}]({item['link']})**\n"
            f"`{item['score']}/100` | {categories} | {item['source']}{media_flag}"
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


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    print(f"Błąd komendy slash news bota: {error}")
    message = "Wystąpił błąd komendy. Szczegóły są w logach Render."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as exc:
        print(f"Nie udało się wysłać komunikatu błędu Discord: {exc}")


@tasks.loop(minutes=FEED_POLL_MINUTES)
async def feed_scan_loop():
    await run_feed_scan(publish=True)


@tasks.loop(minutes=1)
async def digest_loop():
    if not NEWS_CHANNEL_ID:
        return
    now = datetime.datetime.now(TZ_POLAND)
    if now.hour not in DIGEST_HOURS:
        return
    key = now.strftime("%Y-%m-%d-%H")
    if STATE.get("last_digest", {}).get("key") == key:
        return
    channel = bot.get_channel(NEWS_CHANNEL_ID)
    if not channel:
        print(f"Nie znaleziono kanału digestu NEWS_CHANNEL_ID={NEWS_CHANNEL_ID}.")
        return
    try:
        await publish_digest(channel)
        STATE["last_digest"] = {"key": key, "timestamp": time.time()}
        save_state()
        print(f"Digest automatyczny opublikowany dla okna {key}.")
    except Exception as exc:
        print(f"Błąd automatycznego digestu dla okna {key}: {exc}")


@bot.tree.command(name="news_scan", description="Ręcznie skanuje źródła newsów i publikuje ważne alerty.")
@discord.app_commands.describe(publish_all="Testowo publikuje wszystkie nowe wpisy, ignorując próg score.")
async def slash_news_scan(interaction: discord.Interaction, publish_all: bool = False):
    await interaction.response.defer(ephemeral=True, thinking=True)
    result = await run_feed_scan(publish=True, publish_all=publish_all, channel_override=interaction.channel)
    if result.get("reason"):
        await interaction.followup.send(content=result["reason"], ephemeral=True)
        return
    await interaction.followup.send(
        content=(
            f"Przeskanowano źródła.\n"
            f"Pobrane wpisy: `{result['fetched']}`\n"
            f"Nowe wpisy: `{result['new']}`\n"
            f"Opublikowane alerty: `{result['published']}`\n"
            f"Nieudane publikacje: `{result['failed']}`\n"
            f"Limit publikacji na skan: `{result['max_publish']}`\n"
            f"Próg alertu: `{result['threshold']}`"
            + (("\n\nBłędy:\n" + "\n".join(f"- {e}" for e in result["errors"])) if result["errors"] else "")
        ),
        ephemeral=True,
    )


@bot.tree.command(name="news_status", description="Pokazuje diagnostykę feedów i pamięci news bota.")
async def slash_news_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    channel = bot.get_channel(NEWS_CHANNEL_ID) if NEWS_CHANNEL_ID else None
    access_error = get_channel_access_error(channel) if NEWS_CHANNEL_ID else "Brak NEWS_CHANNEL_ID."
    current_channel_error = get_channel_access_error(interaction.channel)
    feed_lines = []
    for feed in FEED_STATS.get("feeds", [])[:8]:
        feed_lines.append(f"- `{feed['entries']}` wpisów | {feed['status']} | {feed['source']}")
    if not feed_lines:
        feed_lines.append("- Brak danych z ostatniego skanu. Użyj `/news_scan` albo poczekaj na automatyczny skan.")
    message = (
        f"NEWS_CHANNEL_ID: `{NEWS_CHANNEL_ID}`\n"
        f"Próg alertu: `{ALERT_SCORE_THRESHOLD}`\n"
        f"Feedów: `{len(NEWS_FEEDS)}`\n"
        f"Pobrane wpisy ostatnio: `{FEED_STATS.get('fetched', 0)}`\n"
        f"Widziane wpisy w pamięci: `{len(STATE.get('seen', {}))}`\n"
        f"Ostatni skan: `{FEED_STATS.get('last_scan')}`\n\n"
        f"Dostęp do NEWS_CHANNEL_ID: `{access_error or 'OK'}`\n"
        f"Dostęp do tego kanału: `{current_channel_error or 'OK'}`\n\n"
        + "\n".join(feed_lines)
    )
    await interaction.followup.send(content=message[:1900], ephemeral=True)


@bot.tree.command(name="news_reset", description="Czyści pamięć widzianych newsów bota.")
async def slash_news_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    STATE["seen"] = {}
    STATE["digest_items"] = []
    save_state()
    await interaction.followup.send(content="Pamięć widzianych newsów została wyczyszczona. Uruchom `/news_scan` ponownie.", ephemeral=True)


@bot.tree.command(name="news_digest", description="Publikuje reczny digest newsow krypto.")
async def slash_news_digest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    channel = interaction.channel
    access_error = get_channel_access_error(channel)
    if access_error:
        await interaction.followup.send(content=access_error, ephemeral=True)
        return
    try:
        await publish_digest(channel)
        await interaction.followup.send(content="Digest opublikowany.", ephemeral=True)
    except Exception as exc:
        print(f"Blad publikacji digestu: {exc}")
        await interaction.followup.send(content=f"Nie udalo sie opublikowac digestu: {exc}", ephemeral=True)


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
