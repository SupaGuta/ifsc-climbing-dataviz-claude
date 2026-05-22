# This script will scrap all ids from seasons, seans_leagues, events, results and athletes from the ISFC API
# Everything is stored in a simple SQLite database for futher reference
# Once done, we'll use it to gather all the associated content

# Imported modules
from pathlib import Path
import sys
import sqlite3
import logging
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from assets.helpers import data_fetcher


# General config
LOG_FILE = BASE_DIR / "assets" / "logs" / "data_struct_fether_errors.log"
DB_FILE = BASE_DIR / "assets" / "databases" / "ifsc-data-struct-test.sqlite" 
# Seasons range IDs (checked manually)
START_ID = 0
END_ID = 3 # 2025


# Initiate log file
logging.basicConfig(filename=LOG_FILE, level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s')


# Initialize database
db_conn = sqlite3.connect(DB_FILE)
db_cur = db_conn.cursor()

# Drop then create database tables
db_cur.executescript('''
DROP TABLE IF EXISTS Seasons;
DROP TABLE IF EXISTS Season_Leagues;
DROP TABLE IF EXISTS Events;
DROP TABLE IF EXISTS Results;
DROP TABLE IF EXISTS Athletes;
                     
CREATE TABLE IF NOT EXISTS Seasons (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE
);
                     
CREATE TABLE IF NOT EXISTS Season_Leagues (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE,
    season_id INTEGER
);
                     
CREATE TABLE IF NOT EXISTS Events (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE,
    season_league_id INTEGER
);
                     
CREATE TABLE IF NOT EXISTS Results (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER,
    event_id INTEGER,
    UNIQUE (ifsc_id, event_id)
);
                     
CREATE TABLE IF NOT EXISTS Athletes (
    id INTEGER PRIMARY KEY,
    ifsc_id INTEGER UNIQUE
);
''')


# Gather data from seasons endpoint
# API : /seasons/id
# Fill tables: Seasons, Season_Leagues, Events
print("Started scraping seasons...")
data_list, failed_ids = data_fetcher.scrape_parallel("seasons", range(START_ID, END_ID+1))

# Parse data and save to database
seasons_count = 0
season_leagues_count = 0
events_count = 0

try:
    for data in data_list:
        # Seasons
        ifsc_id = data.get("ifsc_id", None)    
        db_cur.execute("INSERT OR IGNORE INTO Seasons (ifsc_id) VALUES ( ? )", ( ifsc_id, )) 
        row = db_cur.execute('SELECT id FROM Seasons WHERE ifsc_id = ? ', (ifsc_id, ))
        season_id = db_cur.fetchone()[0]
        seasons_count = seasons_count + 1

        # Season_Leagues
        season_leagues = data.get("leagues", None)
        for season_league in season_leagues:
            season_league_ifsc_id = int(season_league.get("url", None).strip('/').split('/')[3])
            db_cur.execute("INSERT OR IGNORE INTO Season_Leagues (ifsc_id, season_id) VALUES ( ?, ? )", (season_league_ifsc_id, season_id))
            db_cur.execute("SELECT id FROM Season_Leagues WHERE ifsc_id = ?", (season_league_ifsc_id, ))  
            season_league_id = db_cur.fetchone()[0]
            season_leagues_count = season_leagues_count + 1

        # Events
        events = data.get("events", None) 
        for event in events:
            event_ifsc_id = event.get("event_id", None)
            db_cur.execute("INSERT OR IGNORE INTO Events (ifsc_id, season_league_id) VALUES ( ?, ? )", (event_ifsc_id, season_league_id))
            events_count = events_count + 1
        
        # Commit all season info to database
        db_conn.commit()

    # Output
    print(f"Scraped {seasons_count} seasons, {season_leagues_count} season leagues and {events_count} events.")

except Exception as e:
    logging.error(f"Error parsing data for '/seasons/{ifsc_id}': {e}")

# Gather data from events endpoint
# API : /events/id
# Fill table: Results
print("Started scraping events...")
events_ifsc_ids = [int(row[0]) for row in db_cur.execute("SELECT ifsc_id FROM Events")]
data_list, failed_ids = data_fetcher.scrape_parallel("events", events_ifsc_ids)

# Parse data and save to database
events_count = 0
results_count = 0

try:
    for data in data_list:    
        ifsc_id = data.get("ifsc_id", None) 
        db_cur.execute("SELECT id FROM Events WHERE ifsc_id = ?", (ifsc_id, ))
        row = db_cur.fetchone()
        if row:
            event_id = row[0]
        else:
            continue
        events_count = events_count + 1

        # Results
        d_cats = data.get("d_cats", None)
        for d_cat in d_cats:
            result_ifsc_id = d_cat.get("dcat_id", None)
            db_cur.execute("INSERT OR IGNORE INTO Results (ifsc_id, event_id) VALUES ( ?, ? )", (result_ifsc_id, event_id))
            results_count = results_count + 1
        
        # Commit all season info to database
        db_conn.commit()

    # Output
    print(f"Scraped {events_count} events and {results_count} results.")

except Exception as e:
    logging.error(f"Error parsing data for '/events/{ifsc_id}': {e}")


# Gather data from results endpoint
# API : /events/id/result/id
# Fill table: Athletes

# Get event ids to scrape results
events_count = 0
results_count = 0
athletes_count = 0

events = db_cur.execute("SELECT id, ifsc_id FROM Events ORDER BY id ASC").fetchall()
for event_id, event_ifsc_id in events:
    print("Started scraping results for event", event_ifsc_id, "...")
    results_ifsc_ids = [int(row[0]) for row in db_cur.execute("SELECT ifsc_id FROM Results WHERE event_id = ?", (event_id, ))]

    data_list, failed_ids = data_fetcher.scrape_parallel("events/"+str(event_ifsc_id)+"/result", results_ifsc_ids)
    events_count = events_count + 1

    try:
        # Parse data and save to database
        for data in data_list: 
            ifsc_id = data.get("ifsc", None)
            results_count = results_count + 1

            # Athletes
            rankings = data.get("ranking", None)
            if not rankings:
                continue

            for ranking in rankings:
                athlete_ifsc_id = ranking.get("athlete_id", None)
                db_cur.execute("INSERT OR IGNORE INTO Athletes (ifsc_id) VALUES ( ? )", (athlete_ifsc_id, ))
                if db_cur.rowcount == 1:
                    athletes_count = athletes_count + 1
            
            # Commit all season info to database
            db_conn.commit()

    except Exception as e:
        logging.error(f"Error parsing data for '/events/{event_ifsc_id}/result/{ifsc_id}': {e}")

    # Output
    print(f"Scraped {events_count} events, {results_count} results and {athletes_count} athletes.")


# Close database handle
db_cur.close()