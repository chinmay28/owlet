# Unofficial Python API for the Owlet Smart Baby Monitor 
![Python package](https://github.com/BastianPoe/owlet_api/workflows/Python%20package/badge.svg) [![Coverage Status](https://coveralls.io/repos/github/BastianPoe/owlet_api/badge.svg?branch=master)](https://coveralls.io/github/BastianPoe/owlet_api?branch=master) [![PyPI version](https://badge.fury.io/py/owlet-api.svg)](https://badge.fury.io/py/owlet-api)

This is an unofficial python API for retrieving data from the [Owlet Smart Sock](https://www.owletcare.com). The Owlet Smart Sock is a baby monitoring system that tries to prevent the [Sudden infant death syndrome](https://en.wikipedia.org/wiki/Sudden_infant_death_syndrome) by monitoring the baby's heartbeat as well as blood oxygen level via pulse oximetry and warning parents if abnormalities are detected.

The Owlet Smart Sock sends data to the [Ayla Networks](https://www.aylanetworks.com) cloud service. You can access it via the [Ayla API](https://developer.aylanetworks.com/apibrowser/). The meaning of many attributes is not yet known (to me), but some are more obvious. See here for an example:

```
TIMESTAMP;DSN;AGE_MONTHS_OLD;ALRTS_DISABLED;ALRT_SNS_BLE;ALRT_SNS_YLW;APP_ACTIVE;AVERAGE_DATA;BABY_NAME;BASE_STATION_ON;BATT_LEVEL;BIRTHDATE;BLE_MAC_ID;BLE_RSSI;CHARGE_STATUS;CRIT_BATT_ALRT;CRIT_OX_ALRT;DEVICE_PING;DISABLE_LOGGED_DATA;ELEVATION;GENDER;HEART_RATE;HIGH_HR_ALRT;LATITUDE;LIVE_DATA_STREAM;LOCAL_BLE_MAC_ID;LOGGED_DATA_CACHE;LONGITUDE;LOW_BATT_ALRT;LOW_BATT_PRCNT;LOW_HR_ALRT;LOW_INTEG_READ;LOW_OX_ALRT;LOW_PA_ALRT;MOVEMENT;NURSERY_MODE;oem_base_version;oem_sock_version;ON_BOARDING;OTA_ERROR;OTA_STATUS;OXYGEN_LEVEL;PREMATURE;SHARE_DATA;SOCK_CONNECTION;SOCK_DISCON_ALRT;SOCK_DIS_APP_PREF;SOCK_DIS_NEST_PREF;SOCK_OFF;SOCK_REC_PLACED;
1546552539.567462;AC000W00REDACTED;None;None;1;1;1;None;Little Baby;1;81;20190115;EEFE7EREDACTED;0;0;0;0;0;None;None;M;89;0;None;None;E711E4REDACTED;https://ads-field.aylanetworks.com/apiv1/devices/REDACTED/properties/LOGGED_DATA_CACHE/datapoints/REDACTED.json;None;0;None;0;0;0;0;0;0;M2_2_0_0_a078;B2_0_19_0_f331;0;0;0;99;None;None;1;0;None;None;0;0;
```

## Requirements
* Python >= 3.5
* requests
* python-dateutil
* argparse

## Usage
The easiest way to access data from the Owlet is via our command line interface (CLI). 

### Command Line Interface
Here is the build-in help:
```
usage: owlet [-h] [--device DEVICE] [--stream ATTRIBUTES] [--timeout TIMEOUT]
             email password {token,devices,attributes,stream,download}
             [{token,devices,attributes,stream,download} ...]
owlet: error: the following arguments are required: email, password, actions
```

Obtain an authentication token:
```
$ owlet email@email.org password token
Token: 402aba28d94a4493a106a6REDACTED
```

Obtain a listing of all devices in your account:
```
owlet email@email.org password devices
AC000W00REDACTED Online  18.7667 4.1833
```

List all attributes of all devices in your account:
```
$ owlet email@email.org password attributes
AGE_MONTHS_OLD      Age (Months)          None                 None
ALRTS_DISABLED      Disable Alerts        None                 None
ALRT_SNS_BLE        Alert Sense Ble       2018-05-09 20:54:11+00:00 1
ALRT_SNS_YLW        Alert Sense Yellow    2018-05-09 20:54:42+00:00 1
APP_ACTIVE          App Active            2019-01-10 18:50:40+00:00 0
...
```

Contiously poll the service for new data and output in CSV format:
```
$ owlet email@email.org password stream
TIMESTAMP;DSN;AGE_MONTHS_OLD;ALRTS_DISABLED;ALRT_SNS_BLE;ALRT_SNS_YLW;APP_ACTIVE;AVERAGE_DATA;BABY_NAME;BASE_STATION_ON;BATT_LEVEL;BIRTHDATE;BLE_MAC_ID;BLE_RSSI;CHARGE_STATUS;CRIT_BATT_ALRT;CRIT_OX_ALRT;DEVICE_PING;DISABLE_LOGGED_DATA;ELEVATION;GENDER;HEART_RATE;HIGH_HR_ALRT;LATITUDE;LIVE_DATA_STREAM;LOCAL_BLE_MAC_ID;LOGGED_DATA_CACHE;LONGITUDE;LOW_BATT_ALRT;LOW_BATT_PRCNT;LOW_HR_ALRT;LOW_INTEG_READ;LOW_OX_ALRT;LOW_PA_ALRT;MOVEMENT;NURSERY_MODE;oem_base_version;oem_sock_version;ON_BOARDING;OTA_ERROR;OTA_STATUS;OXYGEN_LEVEL;PREMATURE;SHARE_DATA;SOCK_CONNECTION;SOCK_DISCON_ALRT;SOCK_DIS_APP_PREF;SOCK_DIS_NEST_PREF;SOCK_OFF;SOCK_REC_PLACED;
```

Download the `LOGGED_DATA_CACHE` (of currently unknown format):
```
owlet email@email.org password download
��8;������������M=���N@����:���R>����7����9���W:���X<��Z>���\H���_K����@����B����
...
```

### Python
You can take the [CLI implementation](owlet_api/cli.py) as reference. A basic example:
```
# Import Owlet API
from owlet_api.owletapi import OwletAPI

# Instantiate and login
api = OwletAPI('email@email.org', 'password')
api.login()

# Iterate over all devices
for device in api.get_devices():
    # Update the attributes of this device
    device.update()
    
    # Enable data streaming for this device
    device.reactivate()
    
    # Print out all properties
    for name, myproperty in device.get_properties().items():
        print("%-19s %-21s %-20s %s" % (myproperty.name, myproperty.display_name, myproperty.last_update, myproperty.value))
    
```

## Publishing to HomeAPI (systemd services)
Two systemd units store the Owlet Smart Sock data in a [HomeAPI](https://github.com/chinmay28/HomeAPI) server on your local network. They are meant to run on a Raspberry Pi that sits on the same LAN as HomeAPI.

| Unit | What it does |
| ---- | ------------ |
| `owlet-homeapi.service` | `owlet-homeapi`, a daemon that polls the sock every 2 seconds and writes one HomeAPI entry per reading |
| `owlet-summarize.timer` | runs `owlet-homeapi-summarize` once a day, which rolls the completed days up into daily and weekly summaries and then deletes the per-reading entries |

Together they keep HomeAPI small: the raw per-second series only ever covers today, everything older is kept as summaries.

### One line install
```bash
curl -fsSL https://raw.githubusercontent.com/chinmay28/owlet/master/deploy/install.sh | sudo OWLET_EMAIL=you@example.org OWLET_PASSWORD='your-password' HOMEAPI_URL=http://homeapi.local:9999 bash
```

That single command installs the prerequisites (`git`, `python3`, `python3-venv`), creates a dedicated `owlet` system user, installs this package into a virtualenv under `/opt/owlet-homeapi`, writes `/etc/owlet-homeapi/owlet-homeapi.env`, starts the `owlet-homeapi` collector and enables the `owlet-summarize.timer` roll up. Readings start flowing into HomeAPI every 2 seconds, and the first roll up runs at 00:30 the next night.

Re-running the **same** command upgrades an existing install in place and keeps your configuration - values already in the environment file are preserved, only the variables you pass on the command line are updated.

You can also install without credentials and fill them in afterwards:

```bash
curl -fsSL https://raw.githubusercontent.com/chinmay28/owlet/master/deploy/install.sh | sudo bash
sudo nano /etc/owlet-homeapi/owlet-homeapi.env   # add OWLET_EMAIL and OWLET_PASSWORD
sudo systemctl start owlet-homeapi
```

Common operations after install:

```bash
systemctl status owlet-homeapi              # collector status
journalctl -u owlet-homeapi -f              # live logs
sudo systemctl restart owlet-homeapi        # restart, e.g. after editing the config

systemctl list-timers owlet-summarize.timer # when the next roll up runs
journalctl -u owlet-summarize -f            # roll up logs
sudo systemctl start owlet-summarize.service   # roll up right now
```

To remove it again (add `--purge` to also drop the config and the service user):

```bash
curl -fsSL https://raw.githubusercontent.com/chinmay28/owlet/master/deploy/uninstall.sh | sudo bash
```

### Configuration
All settings live in `/etc/owlet-homeapi/owlet-homeapi.env` (mode `0640`, readable by the service user only). Every one of them can also be passed to the installer, as shown above. See [`deploy/owlet-homeapi.env.example`](deploy/owlet-homeapi.env.example) for an annotated copy.

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `OWLET_EMAIL` | - | Owlet account email address (required) |
| `OWLET_PASSWORD` | - | Owlet account password (required) |
| `OWLET_POLL_INTERVAL` | `2` | Seconds between readings |
| `OWLET_REACTIVATE_INTERVAL` | `10` | Seconds between re-arming the Owlet data stream |
| `OWLET_DEVICE` | all devices | Publish only this device serial number (DSN) |
| `OWLET_ATTRIBUTES` | all attributes | Comma separated list of attributes to publish |
| `OWLET_PUBLISH_MODE` | `history` | `history` writes one entry per reading, `latest` keeps a single continuously updated entry per device, `both` does the two of them |
| `OWLET_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `HOMEAPI_URL` | `http://localhost:9999` | Base URL of the HomeAPI server |
| `HOMEAPI_CATEGORY` | `owlet` | HomeAPI category for the created entries |
| `HOMEAPI_KEY_PREFIX` | `owlet` | Prefix of the HomeAPI entry key |
| `HOMEAPI_TIMEOUT` | `10` | HTTP timeout for HomeAPI requests, in seconds |
| `HOMEAPI_SUMMARY_CATEGORY` | `owlet_summary` | HomeAPI category the summaries are written to |
| `HOMEAPI_SUMMARY_KEY_PREFIX` | `owlet_summary` | Prefix of the summary entry keys |
| `OWLET_SUMMARY_METRICS` | the four vitals below | Comma separated list of metrics to summarize |
| `OWLET_RAW_RETENTION_DAYS` | `0` | Extra days of per-reading entries to keep after they were summarized |

The installer itself understands a few more variables: `OWLET_REPO`, `OWLET_REF` (branch, tag or commit to deploy, default `master`), `OWLET_PREFIX` (default `/opt/owlet-homeapi`), `OWLET_USER` (default `owlet`) and `OWLET_SUMMARY_SCHEDULE` (the timer's `OnCalendar` expression, default `*-*-* 00:30:00`).

### What ends up in HomeAPI
| Entry | Category | Key | Written by |
| ----- | -------- | --- | ---------- |
| One reading | `owlet` | `owlet_<DSN>_<epoch>` | collector, `history` and `both` mode |
| Always current reading | `owlet` | `owlet_<DSN>` | collector, `latest` and `both` mode |
| One day | `owlet_summary` | `owlet_summary_<DSN>_<YYYY-MM-DD>` | roll up |
| One ISO week | `owlet_summary` | `owlet_summary_<DSN>_<YYYY>-W<WW>` | roll up |

In the default `history` mode the collector creates one entry per reading, which at a 2 second interval is roughly 43000 entries per device per day. The daily roll up turns those into two summary entries and deletes them again, so the raw series never grows beyond a day (plus `OWLET_RAW_RETENTION_DAYS`). A raw reading looks like this:

```bash
$ curl -s "http://homeapi.local:9999/api/entries?category=owlet"
```
```json
{
  "dsn": "AC000W00REDACTED",
  "connection_status": "Online",
  "timestamp": 1546552539.567,
  "collected_at": "2019-01-03T21:55:39.567Z",
  "baby_name": "Little Baby",
  "vitals": {
    "heart_rate": 136,
    "oxygen_level": 96,
    "movement": 1,
    "battery_level": 81,
    "charge_status": 0,
    "sock_connection": 1,
    "base_station_on": 1
  },
  "alerts": {
    "critical_battery": 0,
    "critical_oxygen": 0,
    "high_heart_rate": 0,
    "low_battery": 0,
    "low_heart_rate": 0,
    "low_oxygen": 0,
    "sock_disconnected": 0
  },
  "attributes": {
    "HEART_RATE": 136,
    "OXYGEN_LEVEL": 96,
    "...": "every other attribute of the device"
  }
}
```

### The daily roll up
`owlet-summarize.timer` fires `owlet-homeapi-summarize` once a day at 00:30 (`Persistent=true`, so a run missed while the Pi was off is caught up at boot). For every **completed** day it finds raw readings for - today is never touched - it

1. reads the readings of that day out of HomeAPI, page by page,
2. writes the daily summary entry: overall and per-hour statistics for each metric,
3. rebuilds the summary of the ISO week that day belongs to from the daily summaries of that week,
4. and only then deletes the raw entries it just summarized.

Step 4 is strictly last, and it is skipped for that day if either summary could not be written, so a HomeAPI hiccup costs you a night of raw data at worst, never the data itself. If a delete fails halfway through, the next run re-summarizes what is left but keeps the more complete summary it already wrote, and finishes the clean up.

Each metric is summarized as sample count, minimum, maximum, range, sum, average, median, 10th and 90th percentile, plus a histogram with fixed bins (5 bpm for the heart rate, 1% for the oxygen level), for the whole day and for each hour of it. Daily medians and percentiles are computed from the samples themselves; the weekly ones are derived from the summed histograms, since the samples are gone by then, and are therefore accurate to one bin width. Counts, sums, averages and extremes stay exact at every level.

```bash
$ curl -s "http://homeapi.local:9999/api/entries/owlet_summary_AC000W00REDACTED_2026-07-21"
```
```json
{
  "dsn": "AC000W00REDACTED",
  "period": "day",
  "date": "2026-07-21",
  "week": "2026-W30",
  "samples": 43200,
  "hours_with_data": 24,
  "first_sample_at": "2026-07-21T00:00:01+0200",
  "last_sample_at": "2026-07-21T23:59:59+0200",
  "median_from": "samples",
  "metrics": {
    "heart_rate": {
      "count": 43200, "min": 110, "max": 159, "range": 49,
      "sum": 5824800, "avg": 134.833, "median": 135.5,
      "p10": 114.9, "p90": 156.1,
      "histogram": {"110": 4320, "115": 4320, "120": 5760, "125": 2880}
    },
    "oxygen_level": { "...": "same shape" }
  },
  "hours": {
    "00": {
      "samples": 1800,
      "metrics": {"heart_rate": {"count": 1800, "min": 110, "...": "..."}}
    },
    "...": "one entry per hour of the day"
  },
  "alerts": {
    "low_oxygen": {"active_samples": 24, "share": 0.00056},
    "low_battery": {"active_samples": 0, "share": 0.0}
  }
}
```

The weekly entry has the same shape at week granularity, plus a `daily` section with one compact digest (count, range, average, median) per day of that week, so a week of data is one request:

```bash
$ curl -s "http://homeapi.local:9999/api/entries/owlet_summary_AC000W00REDACTED_2026-W30"
```

A full day summary is around 25 KB of JSON, well inside HomeAPI's 100000 character limit for an entry value; if a pathological day would exceed it, the hourly histograms and then the hourly percentiles are dropped before the summary is written, and the roll up says so in the log.

Deleting a day of readings is one `DELETE` request per entry - about 43000 of them - so expect the nightly run to take a few minutes on a Pi. `TimeoutStartSec=2h` in the unit gives it room.

### Running it by hand
Both commands are normal console scripts that take their configuration from the environment, so they can be run outside of systemd:

```bash
# Validate the configuration and exit
OWLET_EMAIL=you@example.org OWLET_PASSWORD='...' owlet-homeapi --check-config

# Publish exactly one reading and exit
OWLET_EMAIL=you@example.org OWLET_PASSWORD='...' HOMEAPI_URL=http://homeapi.local:9999 owlet-homeapi --once

# Show what the roll up would do, without writing or deleting anything
HOMEAPI_URL=http://homeapi.local:9999 owlet-homeapi-summarize --dry-run --json

# Summarize one specific day, keeping its raw readings
HOMEAPI_URL=http://homeapi.local:9999 owlet-homeapi-summarize --date 2026-07-21 --keep-raw
```

On an installed system the environment file is the source of truth, so the easiest way to run them by hand with exactly the service's configuration is `systemd-run`:

```bash
sudo systemd-run --pty --uid=owlet \
  --property=EnvironmentFile=/etc/owlet-homeapi/owlet-homeapi.env \
  /opt/owlet-homeapi/venv/bin/owlet-homeapi-summarize --dry-run
```

Reference unit files are available in [`deploy/`](deploy); the installer fills in their `@USER@`, `@PREFIX@`, `@CONFIG@` and `@SCHEDULE@` placeholders.

## What are the properties for a device ?
| Attribute           | Human Readable        | Example value  | Interpretation  | 
| ------------------- | --------------------- | -------------- | ----------
| AGE_MONTHS_OLD      | Age (Months)          | None           | Unknown
| ALRTS_DISABLED      | Disable Alerts        | None           | Unknown
| ALRT_SNS_BLE        | Alert Sense Ble       | 1              | BLE Alert Enabled?
| ALRT_SNS_YLW        | Alert Sense Yellow    | 1              | Yellow Alert Enabled?
| APP_ACTIVE          | App Active            | 0              | Flag set by the App (or this library) to enable data streaming |
| AVERAGE_DATA        | Average Data          | None           | Unknown
| BABY_NAME           | Baby's Name           | Little Baby    | Baby's name as set in the App |
| BASE_STATION_ON     | Base Station On       | 1              | Is base station enabled?
| BATT_LEVEL          | Battery Level (%)     | 95             | Battery Level of the sock
| BIRTHDATE           | Birthdate             | 20190115       | Baby's Birthdate
| BLE_MAC_ID          | Sock BLE Id           | EEFE7EREDACTED | BLE MAC of Sock
| BLE_RSSI            | BLE RSSI              | 0              | Unknown
| CHARGE_STATUS       | Charge Status         | 0              | Is sock charging?
| CRIT_BATT_ALRT      | Crit. Battery Alert   | 0              | Battery Critical Alert
| CRIT_OX_ALRT        | Crit. Oxygen Alert    | 0              | Oxygen Critical Alert
| DEVICE_PING         | Device Ping           | 0              | Unknown
| DISABLE_LOGGED_DATA | Disable Logged Data   | None           | Unknown
| ELEVATION           | Elevation             | None           | Unknown
| GENDER              | Gender                | M              | Baby's Gender
| HEART_RATE          | Heart Rate            | 136            | Baby's Heart Rate
| HIGH_HR_ALRT        | High HR Alert         | 0              | High Heart Rate Alert
| LATITUDE            | Latitude              | None           | Unknown
| LIVE_DATA_STREAM    | Live Data Stream      | None           | Unknown
| LOCAL_BLE_MAC_ID    | Base BLE Mac Id       | E711E4REDACTED | BLE MAC of base station
| LOGGED_DATA_CACHE   | Logged Data Cache     | [https://....json](https://ads-field.aylanetworks.com/apiv1/devices/REDACTED/properties/LOGGED_DATA_CACHE/datapoints/REDACTED.json) | URL of logged data (format unknown)
| LONGITUDE           | Longitude             | None           | Unknown
| LOW_BATT_ALRT       | Low Battery Alert     | 0              | Low Battery Alert
| LOW_BATT_PRCNT      | Low Batt. Percent     | None           | Unknown
| LOW_HR_ALRT         | Low HR Alert          | 0              | Low Heart Rate Alert
| LOW_INTEG_READ      | Low Integrity Read    | 0              | Unknown
| LOW_OX_ALRT         | Low Oxygen Alert      | 0              | Low Oxygen Alert
| LOW_PA_ALRT         | Low Pa Alert          | 0              | Unknown
| MOVEMENT            | Baby Movement         | 1              | Is baby moving?
| NURSERY_MODE        | Nursery Mode          | 0              | Unknown
| oem_base_version    | oem_base_version      | M2_2_0_0_a078  | Unknown
| oem_sock_version    | oem_sock_version      | B2_0_19_0_f331 | Unknown
| ON_BOARDING         | On Boarding           | 0              | Unknown
| OTA_ERROR           | OTA Error             | 0              | Unknown
| OTA_STATUS          | OTA Status            | 0              | Unknown
| OXYGEN_LEVEL        | Oxygen Level          | 96             | Baby's Oxygen Level
| PREMATURE           | Premature             | None           | Unknown
| SHARE_DATA          | Share Data            | None           | Unknown
| SOCK_CONNECTION     | Sock Connection       | 1              | Connection to sock available
| SOCK_DISCON_ALRT    | Sock Disconnect Alert | 0              | Sock disconnected alert
| SOCK_DIS_APP_PREF   | Sock Dis. App Pref.   | None           | Unknown
| SOCK_DIS_NEST_PREF  | Sock Dis. Nest Pref.  | None           | Unknown
| SOCK_OFF            | Sock Off              | 0              | Unknown
| SOCK_REC_PLACED     | Sock Recently Placed  | 0              | Unknown

## Acknowledgements
Several others have implemented APIs for the Owlet Smart Sock. The following inspired me when writing this code:
* https://github.com/angel12/pyowlet
* https://github.com/craigjmidwinter/pyowlet
* https://github.com/mbevand/owlet_monitor

Thank you very much for your work and for open sourcing it!
