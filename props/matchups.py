"""Which of a book's games are on the board, for leagues whose books list far more games than Underdog has lines for.

College football has a hundred games a week and soccer a dozen leagues, but Underdog posts lines
for a few dozen of them, and a book asked game by game (FanDuel, Betano, Bovada...) would make a
request for every one. An odds-only league's build gives the books its board's games
(`BOARD_GAMES`: each one's start and the words of its teams' names), and a book asks only for a
game that starts within START_GAP of one of them and shares a word of a team's name with it.
Books name teams their own way ("Penn State" and "Penn State Nittany Lions", "Real Betis" and
"Real Betis Seville"), so one shared word is enough: a game let through by mistake costs a
request, not a wrong price, since prices still find their rows by player.
"""
import datetime as dt
import re
import unicodedata

START_GAP = dt.timedelta(minutes=20)

# words too common in team names to tell two games apart
_GENERIC = {
    "fc", "cf", "afc", "ac", "as", "sc", "ssc", "us", "cd", "rc", "sv", "fk", "sk", "bc", "vfb", "vfl", "ud", "ca",
    "club", "de", "del", "la", "le", "the", "and", "at", "vs", "of", "state", "university", "st", "city", "united",
    "real", "sporting", "athletic", "atletico", "calcio", "football", "olympique", "stade", "saint", "san",
    "north", "south", "east", "west", "central", "northern", "southern", "eastern", "western", "new", "tech",
}


def words(text: str) -> frozenset[str]:
    """The words of a matchup's team names that can tell it from others ("Penn State @ Michigan" -> penn, michigan)."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return frozenset(w for w in re.split(r"[^a-z0-9]+", text) if len(w) >= 3 and w not in _GENERIC)


def _when(start) -> dt.datetime:
    if isinstance(start, (int, float)):   # milliseconds since the epoch
        return dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc)
    if isinstance(start, str):
        return dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    return start


def board_games(lines: list[dict]) -> list[tuple[dt.datetime, frozenset[str]]]:
    """The board's games from its Underdog lines: each one's start and its teams' words."""
    seen = {}
    for line in lines:
        seen.setdefault(line["game"], (_when(line["start"]), words(line.get("matchup") or line["game"].split("|")[-1])))
    return list(seen.values())


def on_board(league, start, *names: str) -> bool:
    """Whether a book's game, starting at `start` and named by `names`, may be one of the board's.
    Always true for a league that doesn't say which games are on its board."""
    games = getattr(league, "BOARD_GAMES", None)
    if games is None:
        return True
    when, mine = _when(start), frozenset().union(*(words(name) for name in names))
    return any(abs(when - begins) <= START_GAP and mine & theirs for begins, theirs in games)


def tag(props: list[dict], start: int, game) -> None:
    """Mark the props from `start` on with the game they're for ([start, "Everton vs Chelsea"]), so an
    odds board can give a player Underdog has no line for a row in his game. A parse calls it as it
    moves from one game's market to the next."""
    if game:
        for prop in props[start:]:
            prop["game"] = game


def game_of(start, name: str | None) -> list | None:
    """A prop's game as a book names it, [start, "Everton vs Chelsea"], or None when the book's answer
    lacks either (a game with no start or no team names can't be told apart from the others)."""
    name = (name or "").strip()
    return [start, name] if start and words(name) else None   # " @ ": a book's names were empty
