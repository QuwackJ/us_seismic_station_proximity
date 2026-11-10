# Download all necessary data for analysis


# Imports
import argparse # how to use the script in the command line
import io # used to read zipfiles without writing to disk first
from io import StringIO # used to read text data as if it were a file
import json # reads/writes regions_config.json
import logging # gives messages to track progress and explain why something failed
import shutil # streams extracted .gpkg out of zip and into destination file without writing to disk first
import time # used to add delays between requests to avoid rate limiting
import zipfile # used to extract .gpkg files from downloaded zip archives
import urllib3 # to handle errors due to flaky connection
from pathlib import Path # how to represent file locations for Mac and Windows
from urllib.parse import urlparse # check if URL ends with .zip
import requests # used to make HTTP requests to download data
import pandas as pd # parse and write station CSV data


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

GEOFABRIK_BASE_URL = 'https://download.geofabrik.de/north-america/us' # common URL prefix shared by all states' download pages

US_STATE_SLUGS = { # mapping of state abbreviations to their corresponding Geofabrik slugs
    'AL': 'alabama', 'AK': 'alaska', 'AZ': 'arizona', 'AR': 'arkansas',
    'CO': 'colorado', 'CT': 'connecticut', 'DE': 'delaware', 'DC': 'district-of-columbia',
    'FL': 'florida', 'GA': 'georgia', 'HI': 'hawaii', 'ID': 'idaho',
    'IL': 'illinois', 'IN': 'indiana', 'IA': 'iowa', 'KS': 'kansas',
    'KY': 'kentucky', 'LA': 'louisiana', 'ME': 'maine', 'MD': 'maryland',
    'MA': 'massachusetts', 'MI': 'michigan', 'MN': 'minnesota', 'MS': 'mississippi',
    'MO': 'missouri', 'MT': 'montana', 'NE': 'nebraska', 'NV': 'nevada',
    'NH': 'new-hampshire', 'NJ': 'new-jersey', 'NM': 'new-mexico', 'NY': 'new-york',
    'NC': 'north-carolina', 'ND': 'north-dakota', 'OH': 'ohio', 'OK': 'oklahoma',
    'OR': 'oregon', 'PA': 'pennsylvania', 'PR': 'puerto-rico', 'RI': 'rhode-island',
    'SC': 'south-carolina', 'SD': 'south-dakota', 'TN': 'tennessee', 'TX': 'texas',
    'UT': 'utah', 'VT': 'vermont', 'VA': 'virginia', 'WA': 'washington',
    'WV': 'west-virginia', 'WI': 'wisconsin', 'WY': 'wyoming', 'VI': 'virgin-islands',
    # california is split into norcal/socal on geofabrik -- handled separately, not in this dict
}

US_STATE_NAMES = { # mapping of state abbreviations to their corresponding full names
    abbr: slug.replace('-', ' ').title() for abbr, slug in US_STATE_SLUGS.items()
}

US_STATE_NAMES['CA'] = 'California'

CONNECT_TIMEOUT = 15 # seconds to establish a connection
READ_TIMEOUT = 120 # seconds allowed between successive chunks (not total transfer time)
MAX_ATTEMPTS = 3 # retries for large/flaky downloads (ex: Alaska, California)


# Given a state abbreviation, return the list of Geofabrik download URL(s)
def geofabrik_urls_for_state(abbr):
    abbr = abbr.strip().upper()

    # special case for California, which is split into norcal and socal on Geofabrik
    if abbr == 'CA':
        return [f'{GEOFABRIK_BASE_URL}/california/norcal-latest-free.gpkg.zip',
                f'{GEOFABRIK_BASE_URL}/california/socal-latest-free.gpkg.zip']

    slug = US_STATE_SLUGS.get(abbr)

    if not slug:
        return None

    return [f'{GEOFABRIK_BASE_URL}/{slug}-latest-free.gpkg.zip']


# Figure out whether to use guessed URL or user-provided URL for downloading the .gpkg file
def resolve_gpkg_urls(abbr, explicit_url=None, interactive=True):
    if explicit_url:
        return [url.strip() for url in explicit_url.split(',') if url.strip()]

    guessed = geofabrik_urls_for_state(abbr)

    if not interactive:
        return guessed or []

    if guessed:
        print('Found standard Geofabrik download URL(s) for this state:')

        for u in guessed:
            print(f'  {u}')

        use_guess = input('Use these? [y/n]: ').strip().lower()
        if use_guess in ('', 'y', 'yes'):
            return guessed

    raw = input('Enter URL(s) to the OSM Geofabrik .gpkg for this state (comma-separated if multiple): ').strip()
    return [url.strip() for url in raw.split(',') if url.strip()]


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

def gpkg_local_paths(name, urls): # build local file path where downloaded .gpkg should be saved based on region name
    base = name.lower().replace(' ', '_')

    if len(urls) <= 1:
        return [f'data/{base}.gpkg']

    # if there are multiple URLs, append an index to the local file path to avoid overwriting
    return [f'data/{base}_{i + 1}.gpkg' for i in range(len(urls))]

def prompt_new_region(abbr, gpkg_url_list=None): # how to save data for a new region that user wants to analyze
    print(f"\n'{abbr}' is not in {REGION_CONFIG_PATH} yet. Complete the following to add it.")

    name = input(f'Enter the full region name (e.g., "Arizona"): ').strip().title()

    gpkg_urls = resolve_gpkg_urls(abbr, explicit_url=gpkg_url_list)

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

    # make data folder to store
    path.parent.mkdir(parents=True, exist_ok=True)

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
    out_path = Path(out_path) # real destination
    part_path = out_path.with_suffix(out_path.suffix + '.part') # temporary file used during download
    last_err = None

    # retry loop that runs 3 times and logs each error
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            # check how many bytes of .part already exist and send HTTP Range header asking the server to continue from that byte offset
            resume_from = part_path.stat().st_size if part_path.exists() else 0
            headers = {'Range': f'bytes={resume_from}-'} if resume_from else {}

            # open the HTTP request but don't download the whole response immediately; pull data incrementally
            with requests.get(url, stream=True, headers=headers,timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as r:
                # if we are resuming and the server returns 416 (Range Not Satisfiable -- nothing left to resume), break the loop
                if resume_from and r.status_code == 416:
                    break

                # if we are resuming but the server does not return 206 (Partial Content), restart the download from scratch
                if resume_from and r.status_code != 206:
                    logger.warning('Server does not support resuming download for %s -- restarting download', url)
                    resume_from = 0
                    part_path.unlink(missing_ok=True)

                # report any error that happens
                r.raise_for_status()

                # if the server returns 206 (Partial Content), send the remaining bytes
                if r.status_code == 206:
                    content_range = r.headers.get('content-range', '')
                    total = int(content_range.split('/')[-1]) if '/' in content_range else None
                # if the server doesn't return 206, get the total content length from the content-length header
                else:
                    cl = r.headers.get('content-length')
                    total = int(cl) if cl else None

                # determine the file open mode based on whether we are resuming or starting fresh
                mode = 'ab' if resume_from else 'wb' # ab is append binary, wb is write binary
                written = resume_from

                # write the downloaded chunks to the output file
                with open(part_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)

                # if the download is incomplete, raise an error
                if total and written < total:
                    raise requests.exceptions.ConnectionError(f'Incomplete download: got {written} of {total} bytes')
            break
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            urllib3.exceptions.ProtocolError
        ) as e: # catch connection errors and timeouts, log them, and retry if possible
            last_err = e
            have = part_path.stat().st_size if part_path.exists() else 0
            logger.warning('Attempt %d/%d failed for %s: %s (%d bytes kept, will resume)', attempt, MAX_ATTEMPTS, url, e, have)

            if attempt < MAX_ATTEMPTS:
                time.sleep(5 * attempt)

            continue

    # if all attempts fail, raise the last error encountered
    else:
        raise last_err

    part_path.rename(out_path)


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
def add_new_gpkg(abbr, url_list=None):
    abbr = abbr.strip().upper()

    urls = resolve_gpkg_urls(abbr, explicit_url=url_list, interactive=False)

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
    parser.add_argument('--url', type=str, default=None, help='Optional comma-separated URL(s) to .gpkg file(s) for the new state; if omitted, standard Geofabrik URLs will be used; used with --new')
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
        urls = resolve_gpkg_urls(abbr, explicit_url=args.url)
        add_new_gpkg(abbr, ', '.join(urls))
        return

    # standard run
    download_state_polygons(force=args.force)
    download_stations(STATIONS_CACHE_PATH, force=args.force)

    regions = load_region_config(REGION_CONFIG_PATH)
    download_configured_gpkgs(regions, force=args.force)

    logger.info('Download complete. Run station_proximity_script.py --state <abbr> to analyze a region')


if __name__ == '__main__':
    main()