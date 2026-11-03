# Download all necessary data for analysis


# Imports
import argparse # how to use the script in the command line
import io
from io import StringIO
import json
import logging # gives messages to track progress and explain why something failed
import shutil
import time
import zipfile
from pathlib import Path # how to represent file locations for Mac and Windows
from urllib.parse import urlparse
import requests
import pandas as pd


# Set up logger
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


# Constants
REGION_CONFIG_PATH = 'regions_config.json' # where per-state settings (gpkg urls, CRS, output filename) are stored; must be edited to add a new state

DEFAULT_REGIONS = { # default settings for regions_config.json
    'WA': {
        'name': 'Washington',
        'abbrs': ['WA'],
        'gpkg_urls': ['https://download.geofabrik.de/north-america/us/washington-latest-free.gpkg.zip'],
        'target_crs': 'EPSG:5070',
        'output_csv': 'output/wa_station_road_rail_water_dist.csv'
    },
    'OR': {
        'name': 'Oregon',
        'abbrs': ['OR'],
        'gpkg_urls': ['https://download.geofabrik.de/north-america/us/oregon-latest-free.gpkg.zip'],
        'target_crs': 'EPSG:5070',
        'output_csv': 'output/or_station_road_rail_water_dist.csv'
    },
    'CA': {
        'name': 'California',
        'abbrs': ['CA'],
        'gpkg_urls': ['https://download.geofabrik.de/north-america/us/california/norcal-latest-free.gpkg.zip', 'https://download.geofabrik.de/north-america/us/california/socal-latest-free.gpkg.zip'],
        'target_crs': 'EPSG:5070',
        'output_csv': 'output/ca_station_road_rail_water_dist.csv'
    },
    'AK': {
        'name': 'Alaska',
        'abbrs': ['AK'],
        'gpkg_urls': ['https://download.geofabrik.de/north-america/us/alaska-latest-free.gpkg.zip'],
        'target_crs': 'EPSG:3338',
        'output_csv': 'output/ak_station_road_rail_water_dist.csv'
    }
}

TIGER_STATE_ZIP_URL = 'https://www2.census.gov/geo/tiger/TIGER2026/STATE/tl_2026_us_state.zip' # where to get state polygons (TIGER)
STATE_POLYGONS_PATH = 'data/tl_2026_us_state/tl_2026_us_state.shp' # state polygons are downloaded once and stored here

NSF_SAGE_URL = 'https://service.earthscope.org/fdsnws/station/1/query' # where to get all seismic station data
STATIONS_CACHE_PATH = 'data/stations_cache/all_us_stations.csv' # stations are downloaded once and stored here

CONNECT_TIMEOUT = 15 # seconds to establish a connection
READ_TIMEOUT = 120 # seconds allowed between successive chunks (not total transfer time)
MAX_ATTEMPTS = 3 # retries for large/flaky downloads (ex: Alaska, California)


# Setting up regions_config.json
def load_region_config(path): # load regions_config.json if it already exists OR create it with DEFAULT_REGIONS data
    p = Path(path)

    # runs if regions_config.json already exists
    if p.exists():
        with open(p) as f:
            return json.load(f)

    logger.info('No %s found -- creating it from built-in defaults', path)

    # write the built-in states to regions_config.json
    save_region_config(path, DEFAULT_REGIONS)

    return dict(DEFAULT_REGIONS)

def save_region_config(path, regions): # write the current configuration to regions_config.json
    with open(path, 'w') as f:
        json.dump(regions, f, indent=2)

def guess_target_crs(abbr): # provide a reasonable CRS for a given state; Alaska has its own projection
    return 'EPSG:3338' if abbr == 'AK' else 'EPSG:5070'

def gpkg_local_paths(name, urls):
    base = name.lower().replace(' ', '_')

    if len(urls) <= 1:
        return [f'data/{base}.gpkg']

    return [f'data/{base}_{i + 1}.gpkg' for i in range(len(urls))]

def prompt_new_region(abbr, gpkg_url_list=None): # how to save data for a new region that user wants to analyze
    print(f"\n'{abbr}' is not in {REGION_CONFIG_PATH} yet. Complete the following to add it.")

    name = input(f'Enter the full region name (e.g., "Arizona"): ').strip().title()

    # if the user provided .gpkgs, split them by commas (if there are multiple); otherwise, ask user to provide .gpkgs
    if gpkg_url_list:
        gpkg_urls = [url.strip() for url in gpkg_url_list.split(',')]
    else:
        raw = input('Enter URL(s) to the OSM Geofabrik .gpkg for this state (comma-separated if multiple): ').strip()
        gpkg_urls = [url.strip() for url in raw.split(',')]

    # provide a crs for the new region (or use default)
    default_crs = guess_target_crs(abbr)
    crs = input(f'Enter the target CRS for this state (default: {default_crs}): ').strip() or default_crs

    # provide a name for the output CSV (or use default)
    default_output_csv = f'output/{abbr.lower()}_station_road_rail_water_dist.csv'
    output_csv = input(f'Enter the output CSV path for this state (default: {default_output_csv}): ').strip() or default_output_csv

    # put everything together to be loaded into regions_config.json
    config = {
        'name': name or abbr.lower(),
        'abbrs': [abbr],
        'gpkg_urls': gpkg_urls,
        'target_crs': crs,
        'output_csv': output_csv
    }

    # reload current regions_config.json and add new region to it
    regions = load_region_config(REGION_CONFIG_PATH)
    regions[abbr] = config
    save_region_config(REGION_CONFIG_PATH, regions)

    # message to confirm save
    logger.info('Added new region configuration for %s', abbr)

    return config


# Downloading stations
def download_stations(cache_path, force=False): # get every seismic station in the US from NSF SAGE and store it in cache
    path = Path(cache_path)

    # check if seismic stations have been downloaded before and use it
    if path.exists() and not force:
        logger.info('Using cached stations at %s', path)
        return path

    # actually download if cache doesn't exist
    logger.info('Downloading stations...')

    # bounding box for entire US; also details for collecting stations from NSF SAGE
    params = {
        'minlongitude': -180,
        'maxlongitude': -65,
        'minlatitude': 18,
        'maxlatitude': 72,
        'level': 'station',
        'format': 'text',
        'nodata': '404'
    }

    # make the request to NSF SAGE for seismic stations
    response = requests.get(NSF_SAGE_URL, params=params, timeout=120)
    response.raise_for_status() # if an error code happened, stop the script

    # raw station data as text; clean whitespace and turn into a list
    lines = response.text.strip().splitlines()

    # if NSF SAGE sent nothing, stop the script
    if not lines:
        raise ValueError('Station query returned no data -- check params')

    # get rid of # from header line
    lines[0] = lines[0].lstrip('#')

    # convert into an actual CSV
    all_stations = pd.read_csv(StringIO('\n'.join(lines)), sep='|')

    # normalize column names to lowercase and strip whitespace
    all_stations.columns = [col.strip().lower() for col in all_stations.columns]

    all_stations.to_csv(path, index=False)

    logger.info('Saved %d stations (nationwide) to %s', len(all_stations), path)

    return path


# Downloading state polygons
def download_state_polygons(shp_path=STATE_POLYGONS_PATH, url=TIGER_STATE_ZIP_URL, force=False):
    shp_path = Path(shp_path)

    # if the state polygons already exist and force is not set, use them
    if shp_path.exists() and not force:
        logger.info('State polygons already exist at %s -- skipping', shp_path)
        return shp_path

    out_dir = shp_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # otherwise, download the state polygons from TIGER
    logger.info('Downloading state polygons from TIGER at %s', url)

    # make the request to TIGER for the zip file
    response = requests.get(url, timeout=180)
    response.raise_for_status() # if an error code happened, stop the script

    # extract the zip file
    with zipfile.ZipFile(io.BytesIO(response.content), 'r') as z:
        z.extractall(out_dir)

    logger.info('Saved state polygons to %s', shp_path)

    return shp_path


# How to download really large files
def _stream_to_path(url, out_path):
    last_err = None

    # retry loop that runs 3 times and logs each error
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # open the HTTP request but don't download the whole response immediately; pull data incrementally
            with requests.get(url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
                # report any error that happens
                r.raise_for_status()

                # get the total size of the file from the response headers
                total = int(r.headers.get('content-length', 0))
                written = 0

                # write the downloaded chunks to the output file
                with open(out_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)

                # if the download is incomplete, raise an error
                if total and written < total:
                    raise requests.exceptions.ConnectionError(f'Incomplete download: got {written} of {total} bytes')

            # if the download is successful, return from the function
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e: # catch connection errors and timeouts, log them, and retry if possible
            last_err = e
            logger.warning('Attempt %d/%d failed for %s: %s', attempt, MAX_ATTEMPTS, url, e)

            if attempt < MAX_ATTEMPTS:
                time.sleep(5 * attempt)

    # if all attempts fail, raise the last error encountered
    raise last_err


# General function for downloading a file
def download_file(url, dest_path, overwrite=False):
    dest_path = Path(dest_path)

    # if the destination file already exists and overwrite is False, skip the download
    if dest_path.exists() and not overwrite:
        logger.info('%s already exists -- skipping download', dest_path)
        return dest_path

    # ensures the folder data exists
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # check if the URL points to a zip file
    is_zip = urlparse(url).path.lower().endswith('.zip')

    # if the URL points to a zip file, make a temporary path for it
    if is_zip:
        logger.info('Downloading (zip) %s', url)
        temp_zip = dest_path.with_suffix(dest_path.suffix + '.download.zip')

        # download the zip in chunks
        try:
            _stream_to_path(url, temp_zip)

            # only extract files ending in .gpkg
            with zipfile.ZipFile(temp_zip) as z:
                gpkg_files = [f for f in z.namelist() if f.lower().endswith('.gpkg')]

                # if there are no files with .gpkg, raise an error
                if not gpkg_files:
                    raise ValueError(f'No .gpkg files found in the zip from {url}')

                # extract the first .gpkg file found
                with z.open(gpkg_files[0]) as src, open(dest_path, 'wb') as out:
                    shutil.copyfileobj(src, out)
        finally:
            # delete the temporary zip file
            temp_zip.unlink(missing_ok=True)
    else:
        # if the URL does not point to a zip file, download it directly
        logger.info('Downloading %s', url)
        _stream_to_path(url, dest_path)

    logger.info('Saved %s to %s', url, dest_path)

    return dest_path


# Download all configured .gpkg files for the given regions
def download_configured_gpkgs(regions, force=False):
    # go through every state in regions_config.json
    for abbr, config in regions.items():
        # get the list of .gpkg URLs for this state
        urls = config.get('gpkg_urls', [])

        # if there are no URLs, skip this state
        if not urls:
            continue

        # get the local paths for the .gpkg files
        paths = gpkg_local_paths(config['name'], urls)

        # download each .gpkg file from the URL to the corresponding local path
        for url, path in zip(urls, paths):
            try:
                download_file(url, path, overwrite=force)
            except Exception as e:
                logger.error('%s: failed to download %s -- %s', abbr, url, e)


# Adding a new gpkg OR replacing gpkg(s) for an existing state in regions_config.json
def add_new_gpkg(abbr, url_list):
    abbr = abbr.strip().upper()

    # cleans the list of URLs
    urls = [url.strip() for url in url_list.split(',') if url.strip()]

    # if no valid URLs are provided, raise an error
    if not urls:
        raise ValueError('No valid URLs provided for downloading .gpkg files')

    regions = load_region_config(REGION_CONFIG_PATH)

    # if the state is already configured, update its .gpkg URLs
    if abbr in regions:
        config = regions[abbr]

        # overwrite the existing .gpkg URLs with the new ones
        config['gpkg_urls'] = urls
        save_region_config(REGION_CONFIG_PATH, regions)

        # download the new .gpkg files to the local paths
        out_paths = gpkg_local_paths(config['name'], urls)
        for url, path in zip(urls, out_paths):
            download_file(url, path, overwrite=True)

        logger.info('Updated %s in gpkg_urls in %s and (re)downloaded to: %s', abbr, REGION_CONFIG_PATH, ', '.join(out_paths))
    else:
        # if the state is not configured, prompt for new region details
        logger.info('%s is not configured yet -- let us finish setting it up', abbr)

        config = prompt_new_region(abbr, ', '.join(urls))

        out_paths = gpkg_local_paths(config['name'], config['gpkg_urls'])
        for url, path in zip(config['gpkg_urls'], out_paths):
            download_file(url, path, overwrite=True)


# Making this behave like a script with command line options
def parse_args():
    parser = argparse.ArgumentParser(description='Download necessary data for seismic station analysis')

    parser.add_argument('--new', action='store_true', help='Add a new OSM .gpkg by providing a download URL')
    parser.add_argument('--state', type=str, default=None, help='State abbreviation (ex: CA) for adding a new state; used with --new')
    parser.add_argument('--url', type=str, default=None, help='Comma-separated URL(s) to .gpkg file(s) for the new state; used with --new')
    parser.add_argument('--force', action='store_true', help='Force re-download of state polygons, stations, and state OSM gpkgs')
    parser.add_argument('--list', action='store_true', help='List all configured states in regions_config.json and exit')

    return parser.parse_args()


# Actually running everything
def main():
    args = parse_args()

    if args.list:
        regions = load_region_config(REGION_CONFIG_PATH)
        print('Configured states:', ', '.join(sorted(regions)) or 'None')
        return

    if args.new:
        abbr = args.state or input('State abbreviation for this gpkg (ex: NV): ').strip().upper()
        url = args.url or input('Comma-separated URL(s) to download the OSM .gpkg file(s): ').strip()
        add_new_gpkg(abbr, url)
        return

    # standard run
    download_state_polygons(force=args.force)
    download_stations(STATIONS_CACHE_PATH, force=args.force)

    regions = load_region_config(REGION_CONFIG_PATH)
    download_configured_gpkgs(regions, force=args.force)

    logger.info('Download complete. Run station_proximity_script.py --state <abbr> to analyze a region')


if __name__ == '__main__':
    main()