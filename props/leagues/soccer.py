"""Soccer: an odds board only, over the competitions Underdog posts lines for.

Like college football (cfb.py) the board has no game logs: its rows are Underdog's lines,
keyed by the player's name, with each book's prices, the fair chance and EV. Underdog lists
every competition under one sport ("FIFA") and names a game home team first ("ROM vs RMA").

The books keep each competition apart, so COMPETITIONS gives each one's place at every book
(None where the book doesn't have it), and books.fetch asks a book once per competition it
carries. The stat maps are shared. Most books post soccer props as one-way ladders ("2+ Shots
on Target", "Anytime Goalscorer"); Pinnacle's "To Score" and Novig's and Kalshi's markets are
two-way. Novig has only the Premier League and MLS (the other league names it was asked for
were refused), and Kalshi's player series are mostly goalscorers, with the Champions League's
the widest set.
"""
import datetime as dt

KEY, NAME, PAGE = "soccer", "Soccer", "soccer.html"
ODDS_ONLY = True   # no game logs: rows are Underdog's lines, with prices, fair and EV only

UNDERDOG_SPORT = "FIFA"
UNDERDOG_STATS = {   # full-match stats; first and last scorers and first-half goals are left out
    "period_1_2_goals": "goals",
    "period_1_2_assists": "assists",
    "period_1_2_goals_assists": "goals_assists",
    "period_1_2_shots_on_target": "shots_on_target",
    "period_1_2_shots_attempted": "shots",
    "period_1_2_saves": "saves",
    "period_1_2_fouls_against": "fouls",
    "period_1_2_fouls_for": "fouls_drawn",
    "period_1_2_cards": "cards",
}
TEAM_ALIASES: dict[str, str] = {}

# each competition's place at each book; the league-wide settings below are the defaults
COMPETITIONS = [
    {"COMPETITION": "Premier League", "DRAFTKINGS_LEAGUE": 40253, "PINNACLE_LEAGUE": 1980, "FANDUEL_COMPETITION": 10932509,
     "KAMBI_PATH": "football/england/premier_league", "NOVIG_LEAGUE": "EPL",
     "KALSHI_GAME": "KXEPLGAME", "KALSHI_STATS": {"KXEPLGOAL": "goals", "KXEPLANYGOAL": "goals",
                                                  "KXEPLSOA": "goals_assists", "KXEPLAST": "assists"},
     "BOVADA_PATH": "soccer/europe/england/premier-league",
     "BETANO_PATH": "/sport/soccer/england/premier-league/1/",
     "POLYMARKET_SPORT": "epl"},
    {"COMPETITION": "Champions League", "DRAFTKINGS_LEAGUE": 40685, "PINNACLE_LEAGUE": 2627, "FANDUEL_COMPETITION": 228,
     "KAMBI_PATH": "football/champions_league",
     "KALSHI_GAME": "KXUCLGAME", "KALSHI_STATS": {"KXUCLGOAL": "goals", "KXUCLSOA": "goals_assists",
                                                  "KXUCLAST": "assists", "KXUCLSOT": "shots_on_target",
                                                  "KXUCLSHOT": "shots", "KXUCLSAVE": "saves"},
     "BETANO_PATH": "/sport/soccer/competitions/champions-league/188566/",
     "POLYMARKET_SPORT": "ucl"},
    {"COMPETITION": "Europa League", "DRAFTKINGS_LEAGUE": 41410, "PINNACLE_LEAGUE": 2630, "FANDUEL_COMPETITION": 2005,
     "KAMBI_PATH": "football/europa_league", "BETANO_PATH": "/sport/soccer/competitions/europa-league/188567/",
     "POLYMARKET_SPORT": "uel"},
    {"COMPETITION": "La Liga", "DRAFTKINGS_LEAGUE": 40031, "PINNACLE_LEAGUE": 2196, "FANDUEL_COMPETITION": 117,
     "KAMBI_PATH": "football/spain/la_liga",
     "KALSHI_GAME": "KXLALIGAGAME", "KALSHI_STATS": {"KXLALIGAGOAL": "goals"},
     "BOVADA_PATH": "soccer/europe/spain/la-liga", "BETANO_PATH": "/sport/soccer/spain/laliga/5/",
     "POLYMARKET_SPORT": "lal"},
    {"COMPETITION": "Serie A", "DRAFTKINGS_LEAGUE": 40030, "PINNACLE_LEAGUE": 2436, "FANDUEL_COMPETITION": 81,
     "KAMBI_PATH": "football/italy/serie_a",
     "KALSHI_GAME": "KXSERIEAGAME", "KALSHI_STATS": {"KXSERIEAGOAL": "goals"},
     "BOVADA_PATH": "soccer/europe/italy/serie-a", "BETANO_PATH": "/sport/soccer/italy/serie-a/1635/",
     "POLYMARKET_SPORT": "sea"},
    {"COMPETITION": "Bundesliga", "DRAFTKINGS_LEAGUE": 40481, "PINNACLE_LEAGUE": 1842, "FANDUEL_COMPETITION": 59,
     "KAMBI_PATH": "football/germany/bundesliga",
     "KALSHI_GAME": "KXBUNDESLIGAGAME", "KALSHI_STATS": {"KXBUNDESLIGAGOAL": "goals"},
     "BOVADA_PATH": "soccer/europe/germany/1-bundesliga",
     "BETANO_PATH": "/sport/soccer/germany/bundesliga/216/",
     "POLYMARKET_SPORT": "bun"},
    {"COMPETITION": "Ligue 1", "DRAFTKINGS_LEAGUE": 40032, "PINNACLE_LEAGUE": 2036, "FANDUEL_COMPETITION": 55,
     "KAMBI_PATH": "football/france/ligue_1",
     "KALSHI_GAME": "KXLIGUE1GAME", "KALSHI_STATS": {"KXLIGUE1GOAL": "goals"},
     "BOVADA_PATH": "soccer/europe/france/ligue-1", "BETANO_PATH": "/sport/soccer/france/ligue-1/215/",
     "POLYMARKET_SPORT": "fl1"},
    {"COMPETITION": "MLS", "DRAFTKINGS_LEAGUE": 89345, "PINNACLE_LEAGUE": 2663, "FANDUEL_COMPETITION": 141,
     "KAMBI_PATH": "football/usa/mls", "NOVIG_LEAGUE": "MLS",
     "KALSHI_GAME": "KXMLSGAME", "KALSHI_STATS": {"KXMLSGOAL": "goals"},
     "BOVADA_PATH": "soccer/north-america/united-states/mls",
     "BETANO_PATH": "/sport/soccer/north-america/mls/17103/",
     "POLYMARKET_SPORT": "mls"},
]

PINNACLE_LEAGUE = None
PINNACLE_STATS = {"To Score": "goals"}   # "Erling Haaland To Score": Yes and No
FANDUEL_PAGE = {"page": "SPORT", "eventTypeId": "1"}   # every competition's games, each picked out by its id
FANDUEL_COMPETITION = None
FANDUEL_TABS = ["popular", "shots"]   # the scorers, assists and the shorter shot ladders; the longer shot ladders
FANDUEL_STATS: dict[str, str] = {}    # it posts no two-way soccer props
FANDUEL_LADDERS = {   # "Player To Have 2 Or More Shots On Target", "Anytime Goalscorer", "To Score Or Assist"
    "goal": "goals", "goals": "goals", "assist": "assists", "score or assist": "goals_assists",
    "shots on target": "shots_on_target", "shots": "shots", "fouls": "fouls",
}
DRAFTKINGS_LEAGUE = None
DRAFTKINGS_CATEGORIES = ["Goalscorer Props", "Player Shots", "Player Assists", "Player Props"]
DRAFTKINGS_STATS: dict[str, str] = {}   # it posts no two-way soccer props
DRAFTKINGS_LADDERS = {   # "Cole Palmer Shots" with "1+", "2+", ...
    "Player Shots on Target": "shots_on_target", "Player Shots": "shots", "Player Assists": "assists",
    "Player to Score or Assist": "goals_assists", "Goalkeeper Saves": "saves",
    "Player Fouls Committed": "fouls", "Player Fouls Won": "fouls_drawn",
}
# fields, a runner per player: the "Goalscorer" subcategory holds "Anytime Goalscorer", "2+ Goals" and
# "1st Goalscorer"; "To Receive a Card" holds "Player to Receive a Card"
DRAFTKINGS_FIELDS = {"Goalscorer": "goals", "Anytime Goalscorer": "goals", "N+ Goals": "goals", "Nst Goalscorer": None,
                     "To Receive a Card": "cards", "Player to Receive a Card": "cards"}
KAMBI_PATH = None
KAMBI_STATS = {
    "To Score": "goals", "To score at least N goals": "goals", "To give an assist": "assists",
    "To score or give an assist": "goals_assists", "Player's shots on target": "shots_on_target",
    "Player's shots": "shots", "Player's fouls conceded": "fouls",
}
NOVIG_LEAGUE = None
NOVIG_STATS = {"PLAYER_GOALS": "goals", "SHOTS_ON_TARGET": "shots_on_target", "ASSISTS": "assists"}
KALSHI_GAME = None
KALSHI_STATS: dict[str, str] = {}
POLYMARKET_SPORT = None   # each competition's code on Polymarket ("epl")
POLYMARKET_STATS = {   # "Granit Xhaka: 2+ goals", "Cole Palmer: Anytime Goalscorer"
    "soccer_anytime_goalscorer": "goals", "soccer_player_goals": "goals", "soccer_player_assists": "assists",
    "soccer_player_goals_plus_assists": "goals_assists", "soccer_player_shots": "shots",
    "soccer_player_shots_on_target": "shots_on_target", "soccer_player_goalkeeper_saves": "saves",
}
BOVADA_PATH = None
BOVADA_STATS = {   # "Shots - Aaron Hickey (BRE)" with "2+ Shots"; the field markets with a runner per player
    "Shots": "shots", "Shots On Target": "shots_on_target", "Anytime Goal Scorer": "goals",
    "To Score N or More Goals": "goals", "To Assist a Goal": "assists", "To Assist N or more Goals": "assists",
    "To Score or Assist a Goal": "goals_assists",
}
MISE_LEAGUE = "125,25277,143,139,134,130,599"   # every competition's tag: it lists them all in one request
MISE_STATS = {"GOALSCORER_WITH_NO_GOAL_ANYTIME": "goals", "GOALSCORER_TWO_OR_MORE": "goals"}
BETANO_PATH = None
BETANO_TAB = 3   # a game's "Player Markets" tab
BETANO_STATS = {
    "Goal scorer": "goals", "Multi-Goalscorer": "goals", "Player to Score or Give an Assist": "goals_assists",
    "Assists": "assists", "Shots on Target": "shots_on_target", "Shots": "shots", "Goalkeeper Saves": "saves",
    "Fouls Committed": "fouls", "Fouls Won": "fouls_drawn",
}
BETANO_FIRST: dict[str, str] = {}

PROPS = {
    "goals": ("Goals", "G"),
    "assists": ("Assists", "A"),
    "goals_assists": ("Goals + Assists", "G+A"),
    "shots_on_target": ("Shots on Target", "SOT"),
    "shots": ("Shots", "SH"),
    "saves": ("Saves", "SV"),
    "fouls": ("Fouls Committed", "FC"),
    "fouls_drawn": ("Fouls Drawn", "FD"),
    "cards": ("Cards", "CRD"),
}
TABS = {"goals": "Goals", "assists": "Ast", "goals_assists": "G+A", "shots_on_target": "SOT", "shots": "Shots",
        "saves": "Saves", "fouls": "Fouls", "fouls_drawn": "Drawn", "cards": "Cards"}
LABELS: dict = {}
CLOCK: list[str] = []
POSITIONS: dict = {}
KINDS: dict = {}
GROUPS: dict = {}
REST = False
SLATE_DAYS = 6


def season_id(day: dt.date) -> int:
    """The season a date falls in, the European way: August to May ("20262027")."""
    start = day.year if day.month >= 7 else day.year - 1
    return start * 10000 + start + 1


def season_label(season: int) -> str:
    return f"{str(season)[:4]}-{str(season)[6:]}"

ANYTIME_STATS = {"goals"}   # an anytime goal: a book's 2+ goals rung is never its price (books.prices)
