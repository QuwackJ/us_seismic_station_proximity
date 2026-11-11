# US Seismic Stations and Proximity to Roads, Railways, and Waterways

Created by Maya Rao for the [Pacific Northwest Seismic Network (PNSN)](https://pnsn.org/)

## Purpose of the Repo

This repo contains two files, `download_data.py` and `station_proximity_script.py`, that collect seismic station data from the entire United States (US), polygons of state boundaries, and OpenStreetMap (OSM) GeoPackages of each state's roads, railways, and waterways. Using these files, you can find the closest road, railway, and waterway to any seismic station in a given state and the distance between them in meters. As a default, this repo can perform this analysis on Washington, Oregon, California, and Alaska. It is possible to analyze other states based on user input. This is useful for planning the locations of future seimsic stations to place them in areas that have less cultural noise.

## How to Use the Repo

### Downloading Data

This repo utilizes data from [NSF SAGE](https://www.iris.edu/hq/sage), [US Census TIGER](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html), and [OSM Geofabrik](https://download.geofabrik.de/north-america/us.html). Since many of the required files are too large to be hosted on GitHub, you will have to use `download_data.py` to acquire the data on your local machine.

In your command line, run `git clone https://github.com/QuwackJ/us_seismic_station_proximity.git` to make a copy of this repo on your computer.

Then, set up a virtual environment with `python -m venv <name of virtual environment>` and activate it (Mac: `source venv/bin/activate`, Windows: `venv\Scripts\activate`).

Next, run `pip install -r requirements.txt`. This allows you to install the required Python libraries without running into version conflicts.

Finally, you can run the following commands to download the data:
* `python download_data.py --force`: downloads all data on US seismic stations, state boundaries, and OSM GeoPackages for Washington, Oregon, California, and Alaska. This process takes a lot of time due to the large file sizes
* `python download_data.py --list`: list all of the states that have been configured for analysis
* `python download_data.py --new --state <abbreviation>`: add a new state to the list of configured states and its data. Please provide the state's abbreviation (ex: NV for Nevada) 
    * ` --url <comma-separated list if multiple>`: *optional* to provide your own URL(s) for the state's OSM GeoPackage. You can use [this link](https://download.geofabrik.de/north-america/us.html) to find a state and provide its .gpkg.zip download link. If a state is split into parts (ex: California is split into Northern and Southern California), please provide the URLs as a comma-separated list *without* spaces.

All downloaded data will be stored in a folder called `data`.

### Seismic Station Analysis

Now that all of the data has been downloaded, you can start running analyses on seismic stations in the state of your choice.

To see which states can be analyzed, use `python station_proximity_script.py --list`.

To run the analysis, use `python station_proximity_script.py --state <abbreviation>`. The resulting CSVs will be stored in a folder called `output`.

The output CSVs track the distance from a station to its nearest road, railway, and waterway as well as the type (denoted with `_fclass`). Here is [what each type means according to OSM Geofabrik](https://download.geofabrik.de/osm-data-in-gis-formats-free.pdf):
* Roads
   * `motorway`:
   * `trunk`:
   * `primary`:
   * `secondary`:
   * `tertiary`:
   * `unclassified`:
   * `residential`:
   * `living_street`:
   * `pedestrian`:
   * `busway`:
   * `_link`:
   * `service`:
   * `track`:
   * `unknown`:
* Railways
   * `rail`:
   * `light_rail`:
   * `subway`:
   * `tram`:
   * `monorail`:
   * `narrow_gauge`:
   * `miniature`:
   * `funicular`:
   * `rack`:
* Waterways
   * `river`:
   * `stream`:
   * `canal`:
   * `drain`:

## AI Disclosure

The code in this repo was generated with Microsoft Copilot and Claude. Access to these models was provided by the University of Washington.
