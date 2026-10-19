"""College football: an odds board only, with no game logs behind it.

The board lists Underdog's lines and each book's prices at them, and the fair chance and EV
the way every board does, but no hit rates: there is no free, complete source of college
players' games to match them to. Its rows are Underdog's lines alone, one per player and
stat, keyed by the player's name.

The books post college props under the NFL's names ("Passing Yds", "Total Rushing Yards"),
so the NFL's maps serve here; each book's own college league is all that differs. Novig and
Kalshi post college games but no player props, so the sharp side of the fair chance is
Pinnacle, DraftKings and FanDuel, and a row is only "sure" with all three (books.CONFIDENT).
A week has a hundred games and Underdog lines a few dozen: the books asked game by game ask
only for those (matchups.py).
"""
import datetime as dt

from . import nfl

KEY, NAME, PAGE = "cfb", "CFB", "cfb.html"
ODDS_ONLY = True   # no game logs: rows are Underdog's lines, with prices, fair and EV only

UNDERDOG_SPORT = "CFB"
UNDERDOG_STATS = nfl.UNDERDOG_STATS   # the same stat ids
TEAM_ALIASES: dict[str, str] = {}

PINNACLE_LEAGUE = 880   # "NCAA"
PINNACLE_STATS = nfl.PINNACLE_STATS
FANDUEL_PAGE = "ncaaf"
FANDUEL_TABS = nfl.FANDUEL_TABS
# its college over/unders are sometimes named "Total Rushing Yards" where the NFL's are "Rushing Yds"
FANDUEL_STATS = {**nfl.FANDUEL_STATS, "Total Passing Yards": "pass_yds", "Total Rushing Yards": "rush_yds",
                 "Total Receiving Yards": "rec_yds"}
FANDUEL_LADDERS = nfl.FANDUEL_LADDERS
DRAFTKINGS_LEAGUE = 87637
DRAFTKINGS_CATEGORIES = nfl.DRAFTKINGS_CATEGORIES
DRAFTKINGS_FIELDS, DRAFTKINGS_LADDERS, DRAFTKINGS_STATS = nfl.DRAFTKINGS_FIELDS, nfl.DRAFTKINGS_LADDERS, nfl.DRAFTKINGS_STATS
KAMBI_PATH = "american_football/ncaaf"
KAMBI_STATS = nfl.KAMBI_STATS
NOVIG_LEAGUE = None     # its college games have no player props
NOVIG_STATS = nfl.NOVIG_STATS
PROPHETX_TOURNAMENT = 27653   # ProphetX's College Football
PROPHETX_STATS = nfl.PROPHETX_STATS   # the same subTypes
POLYMARKET_SPORT = "cfb"
POLYMARKET_STATS = {"anytime_touchdowns": "rush_rec_tds", "two_plus_touchdowns": "rush_rec_tds"}
KALSHI_GAME, KALSHI_TOTAL = "KXNCAAFGAME", "KXNCAAFTOTAL"
KALSHI_STATS: dict[str, str] = {}   # no player series
BOVADA_PATH = "football/college-football"
BOVADA_STATS = nfl.BOVADA_STATS
MISE_LEAGUE = "58"
MISE_STATS = nfl.MISE_STATS
BETANO_PATH = "/sport/football/north-america/ncaaf/184628r/"
BETANO_STATS, BETANO_FIRST = nfl.BETANO_STATS, nfl.BETANO_FIRST

PROPS = {key: label for key, label in nfl.PROPS.items() if key not in ("targets", "fumbles_lost")}
TABS = nfl.TABS
LABELS: dict = {}
FIXED_LINES = nfl.FIXED_LINES   # 2+ TDs: rush + rec TDs over 1.5
ANYTIME_STATS = nfl.ANYTIME_STATS
CLOCK: list[str] = []
POSITIONS: dict = {}
KINDS: dict = {}
GROUPS: dict = {}
REST = False
SLATE_DAYS = 7


def season_id(day: dt.date) -> int:
    """The season a date falls in: one starts in August and ends with January's bowls."""
    return day.year if day.month >= 7 else day.year - 1


def season_label(season: int) -> str:
    return str(season)
