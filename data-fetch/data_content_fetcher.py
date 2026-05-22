# This script will scrap all ids from seasons, seans_leagues, events, results and athletes from the ISFC API
# Everything is stored in a simple SQLite database for futher reference
# Once done, we'll use it to gather all the associated content

# Import modules
from pathlib import Path
import sys
import sqlite3
import logging
import re
from itertools import groupby
# Import helpers
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from assets.helpers import data_fetcher
from assets.helpers import event_location


# General config
LOG_FILE = BASE_DIR / "assets" / "logs" / "data_content_fether_errors.log"
DB_STRUCT = BASE_DIR / "assets" / "databases" / "ifsc-data-struct.sqlite"  
DB_CONTENT = BASE_DIR / "assets" / "databases" / "ifsc-data-content.sqlite"
DATA_TO_FETCH = {
    "seasons" : False,
    "season_leagues" : False,
    "events" : True,
    "results" : True,
    "athletes" : True 
}


# Initiate log file
logging.basicConfig(filename=LOG_FILE, level=logging.ERROR, format='%(asctime)s %(levelname)s: %(message)s')

# Initialize databases
db_struct_conn = sqlite3.connect(DB_STRUCT)
db_struct_cur = db_struct_conn.cursor()

db_content_conn = sqlite3.connect(DB_CONTENT)
db_content_cur = db_content_conn.cursor()


# Gather data from seasons endpoint
# API : /seasons/id
# Fill tables: Seasons, Leagues

if DATA_TO_FETCH["seasons"]:

    # Drop and create new tables
    db_content_cur.executescript('''
    DROP TABLE IF EXISTS Seasons;
    DROP TABLE IF EXISTS Leagues;
                        
    CREATE TABLE IF NOT EXISTS Seasons (
        id INTEGER PRIMARY KEY,
        year INTEGER UNIQUE,
        ifsc_id INTEGER UNIQUE
    );
                        
    CREATE TABLE IF NOT EXISTS Leagues (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE
    );
    ''')

    # Start scraping data from API
    print("Started scraping seasons...")
    # Retrieve valid seasons' ifsc_ids from struct database then start scraping
    seasons_ifsc_ids = [int(row[0]) for row in db_struct_cur.execute("SELECT ifsc_id FROM Seasons")]
    data_list, failed_ids = data_fetcher.scrape_parallel("seasons", seasons_ifsc_ids)

    # Parse data and save to database
    seasons_count = 0
    leagues_count = 0
    errors_count = 0
    
    for data in data_list:
        try:
            # Get current season_league id from IFSC 
            season_ifsc_id = data.get("ifsc_id", None)

            # Parse seasons data
            season_year = data.get("name", None) 
            db_content_cur.execute("INSERT OR IGNORE INTO Seasons (year, ifsc_id) VALUES ( ?, ? )", (season_year, season_ifsc_id)) 
            row = db_content_cur.execute('SELECT id FROM Seasons WHERE ifsc_id = ? ', (season_ifsc_id, ))
            season_id = db_content_cur.fetchone()[0]
            seasons_count = seasons_count + 1

            # Parse leagues data
            leagues = data.get("leagues", None)
            for league in leagues:
                league_name = league.get("name", None)
                db_content_cur.execute("INSERT OR IGNORE INTO Leagues (name) VALUES ( ? )", (league_name, )) 
                if db_content_cur.rowcount == 1:  
                    leagues_count = leagues_count + 1 
            
            # Commit all info to database
            db_content_conn.commit()

        except Exception as e:
            logging.error(f"Error parsing data from '/seasons/{season_ifsc_id}': {e}")
            errors_count = errors_count + 1
            continue

    # Output
    print(f"Scraped {seasons_count} seasons and {leagues_count} leagues ({errors_count} errors).")


# Gather data from season_leagues endpoint
# API : /season_leagues/id
# Fill tables: Disciplines, Categories, Events (partially: season_id, league_id_ ifsc_id)

if DATA_TO_FETCH["season_leagues"]:

    # Drop and create new tables
    db_content_cur.executescript('''
    DROP TABLE IF EXISTS Disciplines;
    DROP TABLE IF EXISTS Categories;
    DROP TABLE IF EXISTS Events;
                        
    CREATE TABLE IF NOT EXISTS Disciplines (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE
    );
                        
    CREATE TABLE IF NOT EXISTS Categories (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        gender INT
    );
                        
    CREATE TABLE IF NOT EXISTS Events (
        id INTEGER PRIMARY KEY,
        season_id INTEGER,
        league_id INTEGER,
        ifsc_id INTEGER UNIQUE,
        name TEXT,
        city TEXT,
        country TEXT,
        date_start DATE,
        date_end DATE,
        is_paraclimbing BOOLEAN
    );
    ''')

    # Start scraping data from API
    print("Started scraping season_leagues...")
    # Retrieve valid season_leagues' ifsc_ids from struct database then start scraping
    season_leagues_ifsc_ids = [int(row[0]) for row in db_struct_cur.execute("SELECT ifsc_id FROM Season_Leagues")]
    data_list, failed_ids = data_fetcher.scrape_parallel("season_leagues", season_leagues_ifsc_ids)

    # Parse data and save to database
    season_leagues_count = 0
    disciplines_count = 0
    categories_count = 0
    events_count = 0
    errors_count = 0

    for data in data_list:
        try:
            # Get current season_league id from IFSC       
            season_leagues_ifsc_id = data.get("ifsc_id", None)

            # Parse disciplines and categories (name and gender)
            dicipline_categories = data.get("d_cats", None)
            for d_cat in dicipline_categories:
                d_cat_name = d_cat.get("name", None)

                # Discipline and category name
                parts = d_cat_name.strip().split(maxsplit=1)
                discipline_name = parts[0].lower()
                category_name = parts[1] or ""

                # Gender (int)
                gender_match = re.search(r"\b(?P<g>men|male|women|female)\b", category_name, re.IGNORECASE)
                category_gender = None
                if gender_match:
                    gender_str = gender_match.group("g").lower()
                    category_gender = 0 if gender_str in ("men", "male") else 1

                # Add dsicipline
                db_content_cur.execute("INSERT OR IGNORE INTO Disciplines (name) VALUES ( ? )", (discipline_name, )) 
                if db_content_cur.rowcount == 1:  
                    disciplines_count = disciplines_count + 1 

                # Add category
                db_content_cur.execute("INSERT OR IGNORE INTO Categories (name, gender) VALUES ( ?, ? )", (category_name, category_gender)) 
                if db_content_cur.rowcount == 1:  
                    categories_count = categories_count + 1

            # Get season and league ids from the corresponding table
            season_year = data.get("season", None)         
            row = db_content_cur.execute('SELECT id FROM Seasons WHERE year = ? ', (season_year, ))
            season_id = db_content_cur.fetchone()[0]

            league_name = data.get("league", None)         
            row = db_content_cur.execute('SELECT id FROM Leagues WHERE name = ? ', (league_name, ))
            league_id = db_content_cur.fetchone()[0]

            # Parse events form season_leagues endpoint
            events = data.get("events", None)
            for event in events:
                event_ifsc_id = event.get("event_id", None)

                # Add event to database
                db_content_cur.execute("INSERT OR IGNORE INTO Events (season_id, league_id, ifsc_id) VALUES ( ?, ?, ? )", (season_id, league_id, event_ifsc_id)) 
                if db_content_cur.rowcount == 1:  
                    events_count = events_count + 1 
            
            # Commit all info to database
            db_content_conn.commit()

        except Exception as e:
            logging.error(f"Error parsing data from '/season_leagues/{season_leagues_ifsc_id}': {e}")
            errors_count = errors_count + 1
            continue

    # Output
    print(f"Scraped {disciplines_count} disciplmines, {categories_count} categories and {events_count} events ({errors_count} errors).")


# Gather data from events endpoint
# API : /events/id
# Fill tables: Events, Competitions

if DATA_TO_FETCH["events"]:

    # Drop and create new tables
    db_content_cur.executescript('''
    DROP TABLE IF EXISTS Competitions;
                        
    CREATE TABLE IF NOT EXISTS Competitions (
        id INTEGER PRIMARY KEY,
        event_id INTEGER,
        discipline_id INTEGER,
        category_id INTEGER,
        ifsc_id INTEGER,
        UNIQUE (event_id, ifsc_id)
    );
    ''')

    # For testing purpose only     
    # db_content_cur.execute('''UPDATE Events SET city=?, country=?''', (None, None))
    
    # Start scraping data from API
    print("Started scraping events...")
    # Retrieve valid events' ifsc_ids from struct database then start scraping
    events_ifsc_ids = [int(row[0]) for row in db_struct_cur.execute("SELECT ifsc_id FROM Events")]
    data_list, failed_ids = data_fetcher.scrape_parallel("events", events_ifsc_ids)

    # Parse data and save to database
    events_count = 0
    competitions_count = 0
    errors_count = 0

    # Store cities with no country and city / country dict
    event_cities = {} # id / city
    event_city_country = {} # city / country
    
    for data in data_list:
        try:
            # Get current event id from IFSC 
            event_ifsc_id = data.get("ifsc_id", None)

            # Get current event in content table
            row = db_content_cur.execute('SELECT id FROM Events WHERE ifsc_id = ? ', (event_ifsc_id, ))
            event_id = db_content_cur.fetchone()[0]

            # Parse event data
            event_name = data.get("name", None)
            event_city, event_country = event_location.parse_city_country(event_name)
            if not event_city:
                event_city = data.get("location", None)
                if event_city:
                    event_city = event_city.strip().split(",")[0]
            if not event_country:
                event_country = data.get("country", None)

            # Store city / country data for further update
            if event_city and not event_country:
                event_cities[event_id] = event_city
            if event_city and event_country:
                event_city_country[event_city] = event_country

            event_date_start = data.get("local_start_date", None)
            event_date_end = data.get("local_end_date", None)
            event_is_paraclimbing = data.get("is_paraclimbing_event", None)
            
            # Update current event data
            db_content_cur.execute('''UPDATE Events 
                                   SET name=?, city=?, country=?, date_start=?, date_end=?, is_paraclimbing=? 
                                   WHERE id=?''', 
                                   (event_name, event_city, event_country, event_date_start, event_date_end, 
                                    event_is_paraclimbing, event_id))
            events_count = events_count + 1
            
            # Parse competitions data
            competitions = data.get("d_cats", None)
            for comp in competitions:
                discipline_name = comp.get("discipline_kind", None)       
                row = db_content_cur.execute('SELECT id FROM Disciplines WHERE name = ? ', (discipline_name, ))
                discipline_id = db_content_cur.fetchone()[0]
                
                category_name = comp.get("category_name", None)
                # Hack for IFSC cat error in event 1462
                if category_name == "AL1":
                    category_name = "Men AL1"  

                row = db_content_cur.execute('SELECT id FROM Categories WHERE name = ? ', (category_name, ))
                category_id = db_content_cur.fetchone()[0]

                comp_ifsc_id = comp.get("dcat_id", None)

                db_content_cur.execute('''INSERT OR IGNORE INTO Competitions 
                                       (event_id, discipline_id, category_id, ifsc_id) 
                                       VALUES ( ?, ?, ?, ?)''', 
                                       (event_id, discipline_id, category_id, comp_ifsc_id)) 
                if db_content_cur.rowcount == 1:  
                    competitions_count = competitions_count + 1 
            
            # Commit all info to database
            db_content_conn.commit()

        except Exception as e:
            logging.error(f"Error parsing data from '/events/{event_ifsc_id}': {e}")
            errors_count = errors_count + 1
            continue
    
    # Search country for cities with no country and update in db
    for event_id, event_city in event_cities.items():
        event_country = event_city_country.get(event_city, None)
        if event_country:
            db_content_cur.execute('''UPDATE Events SET country=? WHERE id=?''', (event_country, event_id))
    
    # Commit all info to database
    db_content_conn.commit()

    # Output
    print(f"Scraped {events_count} events and {competitions_count} competitions ({errors_count} errors).")


# Gather data from results endpoint
# API : /events/id/results/id
# Fill tables: Results, Athletes (IDs only)

if DATA_TO_FETCH["results"]:

    # Drop and create new tables
    db_content_cur.executescript('''
    DROP TABLE IF EXISTS Results;
    DROP TABLE IF EXISTS Athletes;
                        
    CREATE TABLE IF NOT EXISTS Results (
        id INTEGER PRIMARY KEY,
        competition_id INTEGER,
        athlete_id INTEGER,
        rank INTEGER,
        UNIQUE (competition_id, athlete_id)
    );
                        
    CREATE TABLE IF NOT EXISTS Athletes (
        id INTEGER PRIMARY KEY,
        ifsc_id INTEGER UNIQUE,
        firstname TEXT,
        lastname TEXT,
        gender INTEGER,
        height INTEGER,
        arm_span INTEGER,
        birthday DATE,
        city TEXT,
        country TEXT,
        photo_url TEXT,
        is_paraclimbing BOOLEAN
    );
    ''')

    # Parse data and save to database
    events_count = 0
    competitions_count = 0
    competitions_total = 0
    athletes_count = 0
    errors_count = 0    

    # Start scraping data from API
    # Retrieve valid everesultsnts' event_ids and ifsc_ids from struct database then start scraping
    comps_ifsc_ids = [row for row in db_struct_cur.execute("SELECT Events.ifsc_id, Results.ifsc_id FROM Results JOIN Events " \
    "ON Results.event_id = Events.id ORDER BY Events.ifsc_id ASC")]
    for event_ifsc_id, group in groupby(comps_ifsc_ids, key=lambda r: r[0]):
        results_ifsc_ids = [ifsc_id for _, ifsc_id in group]
        competitions_total += len(results_ifsc_ids)
        events_count += 1
        
        print("Started scraping results for event", event_ifsc_id, "...")
        data_list, failed_ids = data_fetcher.scrape_parallel("events/"+str(event_ifsc_id)+"/result", results_ifsc_ids)
  
        for data in data_list:
            try:
                # Get current results id from IFSC 
                results_ifsc_id = data.get("ifsc_id", None)

                # Get current competition in content table
                comp_id = db_content_cur.execute('SELECT Competitions.id FROM Competitions JOIN Events ' \
                'ON Competitions.event_id = Events.id ' \
                'WHERE Events.ifsc_id = ? AND Competitions.ifsc_id = ? ', (event_ifsc_id, results_ifsc_id)).fetchone()[0]

                # Parse reults data
                ranking = data.get("ranking", None)
                for athlete in ranking:
                    athlete_ifsc_id = athlete.get("athlete_id", None)
                    athlete_rank = athlete.get("rank", None)

                    # Add athlete and retrieve athlete id
                    db_content_cur.execute("INSERT OR IGNORE INTO Athletes (ifsc_id) VALUES ( ? )", (athlete_ifsc_id, )) 
                    if db_content_cur.rowcount == 1: 
                        athletes_count = athletes_count + 1  
                    athlete_id = db_content_cur.execute('SELECT id FROM Athletes WHERE ifsc_id = ?', (athlete_ifsc_id, )) .fetchone()[0]

                    # Add result
                    db_content_cur.execute("INSERT OR IGNORE INTO Results (competition_id, athlete_id, rank) " \
                    "VALUES ( ?, ?, ? )", (comp_id, athlete_id, athlete_rank)) 
    
                competitions_count = competitions_count + 1
                
                # Commit all info to database
                db_content_conn.commit()

            except Exception as e:
                logging.error(f"Error parsing data from '/events/{event_ifsc_id}/result/{results_ifsc_id}': {e}")
                errors_count = errors_count + 1
                continue
        
        # Commit all info to database
        db_content_conn.commit() 
        
    # Output
    print(f"Scraped {events_count} events, {competitions_count}/{competitions_total} competitions and {athletes_count} athletes ({errors_count} errors).")


# Close database handles
db_struct_conn.close()
db_content_conn.close()