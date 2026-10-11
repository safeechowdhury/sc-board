"""The leagues the board covers.

Each league is a module with the same surface, so the rest of the package
never asks which sport it is looking at:

    KEY, NAME, PAGE              "nfl", "NFL", "nfl.html"
    UNDERDOG_SPORT               Underdog's sport id
    UNDERDOG_STATS               Underdog stat -> game-log column
    TEAM_ALIASES                 Underdog team code -> ours, where they differ
    PINNACLE_LEAGUE              Pinnacle's league id
    PINNACLE_STATS               Pinnacle prop name ("Receiving Yards") -> game-log column
    FANDUEL_PAGE, FANDUEL_TABS   FanDuel's league page and the game tabs that carry player props
    FANDUEL_STATS                FanDuel market name after the player's ("Receiving Yds") -> game-log column
    FANDUEL_LADDERS              the stat in FanDuel's one-way ladders ("To Record 2+ Hits", "Alt Receptions") -> column
    DRAFTKINGS_LEAGUE            DraftKings' league id
    DRAFTKINGS_CATEGORIES        its prop categories to read ("Receiving Props")
    DRAFTKINGS_STATS             its two-way subcategory name ("Receptions O/U") -> game-log column
    DRAFTKINGS_LADDERS           its one-way "N+" subcategory name ("Receptions") -> game-log column
    DRAFTKINGS_FIELDS            optional: its field market name, count as N ("N+ Goals") -> game-log column
    KAMBI_PATH                   Kambi's sport/league path ("american_football/nfl")
    KAMBI_STATS                  its criterion, count as N ("N+ Receptions By The Player - Including Overtime") -> game-log column
    NOVIG_LEAGUE                 Novig's league ("NHL")
    NOVIG_STATS                  its market type ("SHOTS_ON_GOAL") -> game-log column
    BOVADA_PATH                  Bovada's sport/league path ("football/nfl")
    BOVADA_STATS                 its market name before " - Player (TEAM)", or with the count as N
                                 ("Player to record N+ Hits") -> game-log column
    MISE_LEAGUE                  Mise-o-jeu's league id (drilldown tag)
    MISE_STATS                   its market group code ("TOTAL_RECEPTIONS_OVER_UNDER_PLAYER") -> game-log column
    SCHEMA                       stored columns for one player-game
    PROPS                        stat -> (label, short label): the board's tabs, in order
    TABS                         stat -> the terse name on its tab, so a league's tabs fit on one line
    LABELS                       the same for columns that only appear on a player's page
    CLOCK                        stats stored in seconds and shown as m:ss
    POSITIONS                    position -> (label, kind, defense group)
    KINDS                        kind -> stats to offer ("picks"), list in the game log ("log"),
                                 chart small ("minis") and give a typical line ("typical"); optionally
                                 its own ACTIVE_GAMES ("active") and the stats its games add to the
                                 group's defense ranks ("dvp", default: the picks)
    GROUPS                       defense group -> (plural name, phrased as "facing" the team)
    REST                         whether days of rest between games mean anything
    SLATE_DAYS                   how many days of games the board lists
    ACTIVE_GAMES                 a player is expected to play if he appeared in this many of his team's last games
    TYPICAL_GAMES                games averaged for a typical line
    MIN_AVERAGE                  stat -> average below which a typical line is not worth a row

    season_id(day), previous_season(season), season_label(season)
    refresh(store, days), backfill(store, seasons)   -> progress messages
    samples(df)                  the games that count toward a prop
    lineups(df)                  the games a player counts as a teammate in
    headshots(index)             a photo URL per player, or None
    schedule(today)              upcoming games: date, start, away, home, state
    rosters(teams, today)        team -> player ids, or {} when the league has no roster source
"""
from . import mlb, nba, nfl, nhl

LEAGUES = {league.KEY: league for league in (nhl, nfl, nba, mlb)}
