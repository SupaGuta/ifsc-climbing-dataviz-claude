# The purpose of this script is simply to test IFSC's API endpoints
# and store the response as a json file for future reference

# Imported modules
from pathlib import Path
import requests
import json
import re
import traceback


# General config
BASE_DIR = Path(__file__).resolve().parent.parent
API_BASE_URL =  "https://ifsc.results.info/api/v1"
API_DATA_STRUCT_FOLDER = BASE_DIR / "assets" / "api-data-structures"
HEADERS = {
    'X-Csrf-Token': 'as3PvBDSeMFu_fMVW8Qtp6VDdrJrsHsaToMcOCCK-fkVlwUyCXfeDGXOpFrUv1IBnYdUZYgY-uSW-SoIrWN8bQ',
    'Referer': 'https://ifsc.results.info',
    'Cookie': 'session_id=_verticallife_resultservice_session=fOdV4TuFGIzU315JSVh5TyUd2PeLcdwC9iy6lpNWDBtpjvUhnQCdDZgR90CO57VRc4OMyowGSrzA%2BczbyKPyCMIMa1yr5%2BojTEzGP2fQei8s6v4tNmyueStVlYL46gBo8HXYC%2Fx0yrvRSAuR2rWU4UPnqa%2FrG66wvDOmBBh86GzbWj2ZBfEnOCnxY1gI1PSKYu%2BW4SZ%2FKPR%2FOyL70oWRWCM3pytRBaRPn%2FKlEksHjM%2B2XlkzNRGQi7lFDDeElvUDTRj5aHR2cXkTl0JOFgRY%2B7LWq5vRH6WKwHCmO2%2BEDZxdtMhFqC7aruunhQ%3D%3D--rHK2FeAgP%2BAcKiNE--TvOXDr4r0Rxitjml2KEIFA%3D%3D',
}
_NUMERIC_ID = re.compile(r"^\d+$")


# API fetch function
# (Needed headers found thanks to https://github.com/ChickenNungets/IFSC-data-analysis)
def fetch_data(path):
    url = API_BASE_URL + path

    try:
        response = requests.get(url, headers=HEADERS, timeout=120)
        if response.status_code == 200:
            
            data_struct_filename = request_to_filename(path)
            with open(API_DATA_STRUCT_FOLDER / data_struct_filename, 'w', encoding='utf-8') as f:
                json.dump(response.json(), f, ensure_ascii=False, indent=4)
            print("Succesfully stored data struct to file:", data_struct_filename)

        else:
            print("REQUEST ERROR")
            print(
                f"URL: {response.url}\n"
                f"Status: {response.status_code} {response.reason}"
            )
    
    except Exception as e:
        print("EXCEPTION ERROR")
        traceback.print_exc()


# Transofrms all the request params into a json filename
def request_to_filename(path):
    if path == '':
        base = "root"
    
    else:
        path = path.strip("/")

        parts = []
        for part in path.split("/"):
            if _NUMERIC_ID.match(part):
                parts.append("id")
            else:
                parts.append(part.lower())

        base = "-".join(parts)

    return base + ".json"


# Get and process user input
request_path = input("Request path (i.e.: /athletes/1364): ")

# Fetch data with given params
fetch_data(request_path)