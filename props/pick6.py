"""DraftKings Pick6 lines: the pick'em prices of the sportsbook's little brother.

Pick6 reads its board from a data endpoint on pick6.draftkings.com that answers
a plain request carrying the sportsbook's cookies: the sport page's React Router
payload (`_root.data?sport=...`) holds every pick card — a player, a stat and the
line — serialized in turbo-stream's flat indexed form, which this module decodes.

Pick6 never quotes a price for one side: an entry pays a fixed multiplier by the
number of picks (2 → 3x, 3 → 6x, 4 → 10x, 5 → 12x, 6 → 20x), and each pick its own
multiplier on top (`standingsMultiplier`: a gimme "more" at 0.5x pays half, a
longshot at 3.5x three and a half times). The board needs a price per side, so a
side is priced at the 3-pick rate split across its legs, 6 ** (1/3) ≈ 1.817
decimal, times that side's multiplier, the way Underdog's payout multipliers
price its picks. A line with only a "more" pick has no under.

Each league's `PICK6_STATS` maps the pick cards' stat titles ("Strikeouts
Thrown", "Hits + Runs + RBIs") to ours, and `PICK6_SPORT` says the sport's
code on pick6.draftkings.com ("NHL").
"""
import json
import re
from collections import Counter

from curl_cffi import requests as creq

from . import net
from .icons import icon

KEY, NAME, SHORT = "pick6", "DK Pick6", "Pick6"
WEIGHT = 0     # a pick'em price: never part of the fair chance
TARGET = False  # shown for comparison only, not a site in the EV toggle
LEAGUE_ATTR = "PICK6_SPORT"
ICON = icon("pick6.png")     # shown in place of its name in the EV toggle

SITE = "https://pick6.draftkings.com"
IMPERSONATE = "chrome146"
FLAT = round(6 ** (1 / 3), 3)   # the 3-pick 6x payout, per leg: 1.817
SIDES = {1: "over", 2: "under"}  # a selection's statLinePropositionId: "more" and "less"


def _decode(arr: list) -> dict:
    """turbo-stream's flat encoding: {"_<key index>": <value index>} objects refer
    to other positions in the array, so the tree is resolved by index."""
    def resolve(index, depth, seen):
        if depth > 60 or index in seen:
            return None
        value = arr[index]
        if isinstance(value, dict) and value and all(k.startswith("_") for k in value):
            return {resolve(int(k[1:]), depth + 1, seen | {index}): resolve(v, depth + 1, seen | {index})
                    for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, depth + 1, seen | {index}) for v in value]
        return value
    return resolve(0, 0, frozenset())


def fetch(league, now=None, until=None) -> dict:
    """The league's pick cards, as their payload's data."""
    def ask():   # a refusal or network failure is a ValueError, reported and moved past like any book's
        try:
            with net.site("pick6"):
                session = creq.Session(impersonate=IMPERSONATE, timeout=60)
                try:
                    session.get(f"{SITE}/?sport={league.PICK6_SPORT}", headers={"Accept": "text/html"})
                    r = session.get(f"{SITE}/_root.data?sport={league.PICK6_SPORT}&_routes=routes/_homeShared",
                                    headers={"Accept": "*/*"})
                finally:
                    session.close()
            r.raise_for_status()
        except creq.exceptions.RequestException as e:
            raise ValueError(f"DK Pick6: {e}") from e
        return json.loads(r.text)
    body = net.once(("pick6", league.PICK6_SPORT), ask)
    return _decode(body).get("routes/_homeShared", {}).get("data") or {}


def parse(payload: dict, league) -> tuple[list[dict], Counter]:
    """The league's pick lines, each side at the 3-pick rate times its multiplier, plus a
    count of the stats we don't map (by the card's title)."""
    titles = {re.sub(r"\s+", " ", key).strip().lower(): stat for key, stat in league.PICK6_STATS.items()}
    response = payload.get("pickCardsResponse") or {}
    entities = response.get("entityInfoByDkId") or {}
    market_names = {mid: market.get("name") for mid, market in (response.get("pickSixMarketById") or {}).items()}
    props, skipped = [], Counter()
    for card in (response.get("pickCardByPickableId") or {}).values():
        entity = next((entities.get(str(e.get("dkId"))) for e in card.get("entities") or []), None)
        player = (entity or {}).get("fullName") or (entity or {}).get("name")
        if not player:
            continue
        for market in card.get("activePickableMarkets") or []:
            if market.get("isPaused") or market.get("targetValue") is None:
                continue
            title = market_names.get(str(market.get("pickSixMarketId")), "")
            stat = titles.get(re.sub(r"\s+", " ", title).strip().lower())
            if not stat:
                if title:
                    skipped[title] += 1
                continue
            # each side at the per-leg rate times its own multiplier
            price = {SIDES[sel["statLinePropositionId"]]: round(FLAT * float(sel.get("standingsMultiplier") or 1), 3)
                     for sel in market.get("activeSelections") or [] if sel.get("statLinePropositionId") in SIDES}
            if "over" not in price:   # a "less"-only pick: the board's prices start from the over
                continue
            props.append({"player": player, "stat": stat, "line": float(market["targetValue"]),
                          "over": price["over"], "under": price.get("under")})
    return props, skipped
