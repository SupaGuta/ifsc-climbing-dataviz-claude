# Imported modules
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import logging
from queue import Queue


# General config 
API_BASE_URL =  "https://ifsc.results.info/api/v1"
HEADERS = {
    'X-Csrf-Token': 'as3PvBDSeMFu_fMVW8Qtp6VDdrJrsHsaToMcOCCK-fkVlwUyCXfeDGXOpFrUv1IBnYdUZYgY-uSW-SoIrWN8bQ',
    'Referer': 'https://ifsc.results.info',
    'Cookie': 'session_id=_verticallife_resultservice_session=fOdV4TuFGIzU315JSVh5TyUd2PeLcdwC9iy6lpNWDBtpjvUhnQCdDZgR90CO57VRc4OMyowGSrzA%2BczbyKPyCMIMa1yr5%2BojTEzGP2fQei8s6v4tNmyueStVlYL46gBo8HXYC%2Fx0yrvRSAuR2rWU4UPnqa%2FrG66wvDOmBBh86GzbWj2ZBfEnOCnxY1gI1PSKYu%2BW4SZ%2FKPR%2FOyL70oWRWCM3pytRBaRPn%2FKlEksHjM%2B2XlkzNRGQi7lFDDeElvUDTRj5aHR2cXkTl0JOFgRY%2B7LWq5vRH6WKwHCmO2%2BEDZxdtMhFqC7aruunhQ%3D%3D--rHK2FeAgP%2BAcKiNE--TvOXDr4r0Rxitjml2KEIFA%3D%3D',
}
logger = logging.getLogger(__name__)


# Fetching API data
def fetch_data(path, data_id, data_queue, failed_queue):
    url = API_BASE_URL + path
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=120)
        if response.status_code == 200:
            data = response.json()
            data["ifsc_id"] = data_id # Add current data ID for future reference
            data_queue.put(data)
        else:
            logging.error(f"Failed to fetch '{path}': Status {response.status_code}, Reason: {response.reason}")
            failed_queue.put(data_id)
    
    except Exception as e:
        logging.error(f"Error fetching data for '{path}': {e}")
        failed_queue.put(data_id)


# Retry if fetching failed
def retry_failed_info(endpoint, failed_ids, max_retries=2, delay=2):
    retry_results = []
    failed_queue = Queue()

    for retry_count in range(max_retries):
        print(f"Retry attempt {retry_count + 1} for {len(failed_ids)} failed items.")
        retry_futures = []
        data_queue = Queue()
        
        with ThreadPoolExecutor(max_workers=20) as executor:            
            retry_futures = {}
            for data_id in failed_ids:
                path = parse_api_path(endpoint, data_id)
                retry_futures[executor.submit(fetch_data, path, data_id, data_queue, failed_queue)] = path

            failed_ids = []  # Reset failed_ids list for next retry
        
            for future in as_completed(retry_futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"Error during retry for: {e}")
        
        # Collect results from queues
        while not data_queue.empty():
            retry_results.append(data_queue.get())
        
        while not failed_queue.empty():
            failed_ids.append(failed_queue.get())
        
        if not failed_ids:
            break  # Exit loop if no more failed IDs
        
        time.sleep(delay)  # Wait between retries to avoid overloading the server
    
    if failed_ids:
        print(f"Final failed items after {max_retries} retries: {failed_ids}")
    
    return retry_results, failed_ids


# Start thread workers
def scrape_parallel(endpoint, data_ids, max_workers=25):
    info = []
    failed_ids = []
    data_queue = Queue()
    failed_queue = Queue()
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit tasks for each ID futures
        futures = {}
        for data_id in data_ids:
            path = parse_api_path(endpoint, data_id)
            futures[executor.submit(fetch_data, path, data_id, data_queue, failed_queue)] = path
        
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Error during scraping: {e}")
    
    # Collect results from queues
    while not data_queue.empty():
        info.append(data_queue.get())
    
    while not failed_queue.empty():
        failed_ids.append(failed_queue.get())
    
    # Retry for failed athlete IDs
    if failed_ids:
        retry_results, failed_ids = retry_failed_info(endpoint, failed_ids)
        info.extend(retry_results)  # Add successful retries

    return info, failed_ids


# Parse API path
def parse_api_path(endpoint, data_id):
    path = f"/{endpoint}/{data_id}"
    return path