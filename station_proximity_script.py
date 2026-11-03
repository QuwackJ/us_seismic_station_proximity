# Distance between seismic stations and roads, railways, waterways


# Imports
import argparse # how to use the script in the command line
import logging # gives messages to track progress and explain why something failed
import sys # for exiting the program with an error code
from pathlib import Path # how to represent file locations for Mac and Windows

import pandas as pd
import geopandas as gpd

from download_data import (
    REGION_CONFIG_PATH, 
    STATE_POLYGONS_PATH, 
    STATIONS_CACHE_PATH, 
    load_region_config,
    gpkg_local_paths
)


# Set up logger
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


# Constants
STATE_ABBR_FIELD = 'STUSPS'

STATIONS_CACHE_PATH = 'data/stations_cache/all_us_stations.csv' # stations are downloaded once and stored here
NSF_SAGE_URL = 'https://service.earthscope.org/fdsnws/station/1/query' # where to get all seismic station data

RECENT_END_CUTOFF = pd.Timestamp('2023-01-01') # stations that ended after this date count as recently active
RECENT_START_CUTOFF = pd.Timestamp('2020-01-01') # stations that started after this date count as recently active
MIN_RECENT_ACTIVE_DAYS = 365.25 * 3 # recently active stations must have run for at least 3 years

LAYER_NAMES = {
    'roads': 'gis_osm_roads_free',
    'railways': 'gis_osm_railways_free',
    'waterways': 'gis_osm_waterways_free'
}

EXCLUDE_ROADS = ['bridleway', 'cycleway', 'footway', 'steps'] # fclass to filter out for roads
EXCLUDE_RAILWAYS = ['drag_lift', 'chair_lift', 'cable_car', 'gondola', 'goods', 'other_lift'] # fclass to filter out for railways
EXCLUDE_WATERWAYS = ['drain'] # fclass to filter out for waterways

OSM_COLUMNS = ['fclass', 'name', 'geometry'] # columns to keep from OSM layers


# Making sure required data actually exists before doing any work
def require_file(path, hint): # if a file hasn't been downloaded, stop the program
    if not Path(path).exists():
        logger.error('Missing required file: %s', path)
        logger.error(hint)
        sys.exit(1)

def require_region_data(config, abbr): # if state polygons or station data are missing, stop the program and tell the user to run download_data.py
    require_file(STATE_POLYGONS_PATH, 'Run download_data.py')
    require_file(STATIONS_CACHE_PATH, 'Run download_data.py')

    for gpkg in gpkg_local_paths(config['name'], config['gpkg_urls']):
        require_file(gpkg, f'Run download_data.py for {abbr} and provide a link to download the .gpkg')


# Loading data
def load_stations_with_state(stations_path, boundaries_path, abbr_field): # turn each station's lat/long into a point and keep ones within state boundaries
    # read cached nationwide stations file
    stations = pd.read_csv(stations_path)

    # convert station lat/long to actual points
    stations_geo = gpd.GeoDataFrame(stations, geometry=gpd.points_from_xy(stations['longitude'], stations['latitude']), crs='EPSG:4326')

    # if there isn't an id column, make one with the network and station (ex: UW_1234)
    if 'id' not in stations_geo.columns:
        stations_geo['id'] = stations_geo['network'] + '_' + stations_geo['station']

    # load state polygons
    boundaries = gpd.read_file(boundaries_path)[[abbr_field, 'geometry']]

    # ensure stations and boundaries have the same CRS
    if stations_geo.crs != boundaries.crs:
        boundaries = boundaries.to_crs(stations_geo.crs)

    # for every station point, test if it falls within state polygon
    which_state = gpd.sjoin(stations_geo, boundaries, how='left', predicate='intersects')

    # renames state abbreviation column from TIGER to something simple
    which_state = which_state.rename(columns={abbr_field: 'state_within'})

    # clean up join (index_right has redundant information)
    which_state = which_state.drop(columns=['index_right'])

    return which_state

def filter_active_stations(stations): # only keep stations that are currently active or recently active
    # NSF SAGE uses years far in the future that pandas can't process to mean that a station is still active; get the year as raw text before parsing as date
    raw_end = stations['endtime'].astype(str)
    end_year = pd.to_numeric(raw_end.str.extract(r'(\d{4})')[0], errors='coerce')

    current_year = pd.Timestamp.now().year
    
    # normal datetime parsing for start/end that are within panda's range
    start = pd.to_datetime(stations['starttime'], errors='coerce')
    end = pd.to_datetime(stations['endtime'], errors='coerce')
    today = pd.Timestamp.now().normalize()

    # check if end year is after current year OR if there is no end date (NSF SAGE leaves endtime blank to mean that the station is active)
    no_end_date = stations['endtime'].isna() | (stations['endtime'].astype(str).str.strip() == '')
    still_active = (end_year > current_year) | no_end_date

    duration_days = (end - start).dt.days

    # check if the station ended in the recent past (within the defined cutoff period)
    recently_active = (end < today) & (end > RECENT_END_CUTOFF) & (start > RECENT_START_CUTOFF) & (duration_days >= MIN_RECENT_ACTIVE_DAYS)

    # allows stations that are either still active or recently active
    keep = still_active | recently_active

    kept = stations[keep].copy()

    return kept

def read_osm_layers(gpkg_paths, bbox=None): # read just roads, railways, waterways from OSM gpkgs; also combine multiple gpkgs if provided; bbox should be large enough it doesn't cause program to miss actual nearest features
    # create buckets to hold each type of OSM layer (roads, railways, waterways) (ex: merged = {'roads': [], 'railways': [], 'waterways': []})
    merged = {key: [] for key in LAYER_NAMES}

    # loop over every gpkg file path for a state
    for path in gpkg_paths:
        # for each gpkg file, loop over the feature types (key is the short name like 'roads' and layer is the actual internal gpkg layer)
        for key, layer in LAYER_NAMES.items():
            # pick specific layer, use only necessary OSM columns, and restrict read to features inside bbox if provided
            osm_data = gpd.read_file(path, layer=layer, columns=OSM_COLUMNS, bbox=bbox)

            # append the read OSM data to the appropriate bucket
            merged[key].append(osm_data)

    # loop over each bucket (ex: 'roads') and turn them into one table (won't happen unless state has multiple gpkgs)
    out = {}

    for key, parts in merged.items():
        combined = pd.concat(parts, ignore_index=True)

        # convert the combined DataFrame to a GeoDataFrame with the correct geometry column and CRS
        out[key] = gpd.GeoDataFrame(combined, geometry='geometry', crs=parts[0].crs)

    return out['roads'], out['railways'], out['waterways']


# Doing the actual analysis
def nearest_join(stations_utm, target_data, dist_col, keep_cols, rename_map): # function that actually finds the single closest feature and its distance in meters
    # for every station, check distance to every feature in target_data and find the single closest one
    joined = gpd.sjoin_nearest(stations_utm, target_data, how='left', distance_col=dist_col)

    # sort by distance and keep only the closest feature for each station
    joined = joined.sort_values(dist_col).drop_duplicates(subset='id', keep='first')

    # keep only the necessary columns and rename them according to the provided mapping
    joined = joined[['id'] + keep_cols + [dist_col]].rename(columns=rename_map)

    return joined

def process_region(config, all_stations_states): # actually doing the analysis for a state
    # what we're processing
    name = config['name']
    logger.info('Processing %s...', name)

    # keep only the stations that belong to this state
    region_stations = all_stations_states[all_stations_states['state_within'].isin(config['abbrs'])].copy()

    # if no stations belong to this state, skip processing
    if region_stations.empty:
        logger.warning('No stations found for %s (abbrs = %s) -- skipping', name, config['abbrs'])
        return

    # creating a bounding box around just this state's stations then pad it by 0.5 degrees (~55 km) in all directions
    # 55 km pad added around the rectangle that encloses the state's station points
    # if state polygon is huge but the stations are clustered in a small area, the bounding box helps to load only relevant OSM data for that area
    min_x, min_y, max_x, max_y = region_stations.total_bounds

    buffer = 0.5

    bbox = (min_x - buffer, min_y - buffer, max_x + buffer, max_y + buffer)

    gpkg_paths = gpkg_local_paths(config['name'], config['gpkg_urls'])
    roads, railways, waterways = read_osm_layers(gpkg_paths, bbox=bbox)

    # reproject everything to target CRS so that distances can be measured in meters
    target_crs = config['target_crs']

    roads_m = roads.to_crs(target_crs)
    railways_m = railways.to_crs(target_crs)
    waterways_m = waterways.to_crs(target_crs)
    stations_m = region_stations.to_crs(target_crs)

    # filter out unwanted road, railway, and waterway classes
    roads_filtered = roads_m[~roads_m['fclass'].isin(EXCLUDE_ROADS)].copy()
    railways_filtered = railways_m[~railways_m['fclass'].isin(EXCLUDE_RAILWAYS)].copy()
    waterways_filtered = waterways_m[~waterways_m['fclass'].isin(EXCLUDE_WATERWAYS)].copy()

    # perform nearest join for each type of feature (road, railway, waterway)
    road_join = nearest_join(stations_m, roads_filtered, 'road_dist_m', ['fclass', 'name'], {'fclass': 'road_fclass', 'name': 'road_name'})
    railway_join = nearest_join(stations_m, railways_filtered, 'railway_dist_m', ['fclass', 'name'], {'fclass': 'railway_fclass', 'name': 'railway_name'})
    waterway_join = nearest_join(stations_m, waterways_filtered, 'waterway_dist_m', ['fclass', 'name'], {'fclass': 'waterway_fclass', 'name': 'waterway_name'})

    # put all results together into one CSV
    final = stations_m.copy()
    final = final.merge(road_join, on='id', how='left')
    final = final.merge(railway_join, on='id', how='left')
    final = final.merge(waterway_join, on='id', how='left')
    final = final.drop(columns=['geometry'])

    out_path = Path(config['output_csv'])
    final.to_csv(out_path, index=False)
    logger.info('Wrote output to %s', out_path)


# Making this behave like a script with command line options
def parse_args(): # defines command line options (ex: python station_proximity_script.py --state WA)
    parser = argparse.ArgumentParser(description="Compute nearest road/railway/waterway distance for one state's seismic stations")

    parser.add_argument('--state', type=str, default=None, help='State abbreviation to process (ex: WA, OR, CA, AK, or a new one like NV)')

    parser.add_argument('--list', action='store_true', help='List all configured states')

    return parser.parse_args()


# Actually running everything
def main():
    # read whatever was typed on the command line
    args = parse_args()

    require_file(REGION_CONFIG_PATH, 'Run download_data.py')

    # load current contents of regions_config.json
    regions = load_region_config(REGION_CONFIG_PATH)

    # if the user requested to list all configured states, print them and exit
    if args.list:
        print('Configured states:', ', '.join(sorted(regions)) or 'None')
        return

    # figure out which state was requested by the user
    abbr = args.state

    if not abbr:
        # if no state was provided, prompt the user to enter one
        configured_list = ', '.join(sorted(regions))
        abbr = input(f'Which state do you want to analyze? (configured: {configured_list}) ').strip().upper()
    else:
        # otherwise, just normalize the input to uppercase
        abbr = abbr.strip().upper()

    # stop the program if the requested state is not configured
    if abbr not in regions:
        logger.error('%s is not configured yet', abbr)
        logger.error('Run download_data.py for %s and provide a link to download the .gpkg')
        sys.exit(1)

    config = regions[abbr]

    require_region_data(config, abbr)

    logger.info('Assigning stations with state via spatial join...')

    stations_and_states = load_stations_with_state(STATIONS_CACHE_PATH, STATE_POLYGONS_PATH, STATE_ABBR_FIELD)

    stations_and_states = filter_active_stations(stations_and_states)

    # run the full distance analysis for the state
    try:
        process_region(config, stations_and_states)
    except Exception:
        logger.exception('Error processing region %s', config['name'])


if __name__ == "__main__":
    main()