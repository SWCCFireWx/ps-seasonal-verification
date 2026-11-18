# PS-Seasonal-Verification

Verification of the National Significant Wildland Fire Potential Outlook (monthly and extended).

Archives the monthly National Significant Wildland Fire Potential Outlook (Month 1 to Month 4) for
every PSA, split by GACC, and checks each month against what happened: significant fires compared
with the same month in past years, plus RAWS and ASOS/AWOS weather (rain, temperature, ERC, BI). The
weather is checked station by station against neighboring stations, so a bad sensor doesn't drive the numbers.

The website has a GACC bar at the top: **National** (every GACC together) followed by each GACC. Tabs:
Monthly summary, Outlook archive, Map, Verification, Weather & station checks, Fires, Export, How it works.

This is a separate site from the 7-Day verification site; the two don't share files.

## What's where

| Path | What it is |
|---|---|
| `index.html` | the website (GitHub Pages) |
| `config/gaccs.json` | list of GACCs, their PSAs and PSA names (updated automatically) |
| `config/gaccs/<GACC>.json` | that GACC's settings (significant fire size, normal years, neighbor-check limits, ERC fuel model) |
| `data/<GACC>/outlooks.csv` | outlook archive (issue month × Month 1–4 × PSA: Above / Normal / Below) |
| `data/<GACC>/verification.csv` | every outlook scored: fires, normal range, observed category, outcome, status |
| `data/<GACC>/sig_fires.csv` | each significant fire (discovery date, PSA, acres, cause, location), all years kept |
| `data/<GACC>/fire_counts.csv` | significant fires and acres per PSA per month (past years give "normal") |
| `data/<GACC>/climatology.csv` | normal for this month and the next three, per PSA |
| `data/<GACC>/psa_monthly.csv` | PSA weather by month: rain, % of normal, temperature vs normal, ERC, BI, flagged stations |
| `data/<GACC>/station_monthly.csv` | each station's month, with neighbor comparisons and month flags |
| `data/<GACC>/psa_daily.csv` | PSA daily medians (good stations only) |
| `data/<GACC>/station_daily.csv` | each station's last 62 days, with neighbor values and flags |
| `data/<GACC>/qc_flags.csv` | every station-day the neighbor checks flagged |
| `data/<GACC>/fires.csv`, `starts.csv` | nightly fire-growth days (100+ ac in a day) and new starts per PSA per day |
| `data/<GACC>/review_notes.csv` | review notes added on the site |
| `data/raw/outlooks/<month>/` | the four NIFC outlook maps and the raw service pull for each issue |
| `data/wx_raw/<date>.json.gz` | every station's daily values (kept so days can be re-checked) |
| `data/fire_history/<year>.csv` | national wildfire records (100+ ac) by year, from WFIGS |
| `data/archive_import_log.csv` | what was read from each NIFC archive zip |
| `scripts/` | archiver, archive importer, fire history, station weather and checks, scoring, notes saver |
| `.github/workflows/archive.yml` | nightly job |
| `.github/workflows/apply-notes.yml` | saves review notes sent from the website |

## One-time setup

1. **Create a new repository** on GitHub named `PS-Seasonal-Verification`, Public, empty.
2. **Upload everything** from this folder, including the hidden `.github` folder (on a Mac,
   Cmd+Shift+. in Finder shows it). `.github/workflows` must contain `archive.yml` and `apply-notes.yml`.
3. **Settings → Actions → General → Workflow permissions →** "Read and write permissions" → Save.
4. **Settings → General → Features →** make sure **Issues** is ticked (review notes use issues).
5. **Settings → Pages →** Branch `main`, folder `/ (root)` → Save.
6. **Actions → Archive monthly outlook and check fires and weather → Run workflow.** The first run
   downloads the PSA boundaries, every RAWS and ASOS/AWOS station in the country and 11 years of
   wildfire records, so it can take a while (expect 20–60 minutes). Later nightly runs are faster.
   To get a full first month of weather, run it once with **backfill_from** set to the 1st of the
   month (FEMS and IEM still have recent days).
7. **Load past outlooks:** run the workflow again with **import_archive** set to `all` (or a range such
   as `2023-2025`). See "Past outlooks" below.

## Schedule

Nightly at about 3 am Mountain time, plus an afternoon run on the 1st–8th of each month so a new
outlook is saved the day it appears. Every step skips work that's already done. Each GACC's "day" and
"month" follow that GACC's time zone (set in its settings file).

## The outlook

Month 1 is the issue month, Month 2 the next month, Months 3 and 4 the extended outlook; each covers
the whole calendar month. The PSA categories come from the NPSG map service
`Outlooks_Monthly_Extended` (the same product as the NIFC maps), so nothing is read off a picture.
The four NIFC images (`month1_outlook.png` … `month4_outlook.png`) are saved beside each issue as
the official record and shown on the Outlook archive tab. A new issue is saved the night it shows up
in the service; a later correction to the same issue replaces it and is logged in
`data/outlook_log.csv`.

## Each month's four outlooks

Every issuance covers four months, so every month is forecast four times. The November 2026 issue
covers Nov, Dec, Jan and Feb; the December issue covers Dec, Jan, Feb and Mar; and so on. February
2027 is forecast as Month 4 (issued November), Month 3 (December), Month 2 (January) and Month 1
(February). The Outlook archive tab shows each issuance's four months; the Monthly summary and a PSA's
history show all four outlooks made for one month side by side; Verification scores each outlook month
(1–4) separately or together.

## Past outlooks (NIFC archive)

NIFC keeps one zip per issuance, 2023 onward, under "National Significant Wildland Fire Potential
Archive" on https://www.nifc.gov/nicc/predictive-services/outlooks; earlier years are available by
email from nicc.predictiveservices@firenet.gov. `scripts/import_archive.py` (the workflow's
**import_archive** input) downloads them and reads, in order of preference:

- shapefiles with a PSA code and an Above/Normal/Below field (used directly);
- shapefiles or KML/KMZ of Above/Below areas without PSA codes (each PSA gets the category covering most
  of it; geographic or Web Mercator coordinates only);
- CSV with PSA and category columns.

Which file is Month 1–4 comes from its name (month1, M2, Aug, August, 202508, …). Map images and PDFs in
a zip are saved under `data/raw/outlooks/<issue>/` as the record; if a zip has only images, that issue
can't be scored and the import log (`data/archive_import_log.csv`) says so. Issues already saved from the
live service are never overwritten. Imported outlooks are scored right away against the fire records,
and the fire history automatically reaches back far enough to give them a full normal (years with too
few WFIGS records, before about 2014, are left out of normal). For weather in those months, run the
workflow with **backfill_from** one year at a time (e.g. 2023-01-01 then 2024-01-01), if FEMS and IEM still
have it; IEM keeps full history.

Zips you have yourself (for example emailed older years): run
`python scripts/import_archive.py --zip file1.zip file2.zip` and commit the changed `data/` files.

## How a month is judged (counts decide, acres shown beside)

- **Significant fire:** a wildfire that reached `sig_fire_acres` (default 100), counted in the PSA where it
  started and the month (GACC time zone) it was discovered, at its current size. Source: NIFC WFIGS
  incident records (all years).
- **Normal:** the same calendar month in the previous `normal_years` (default 10) years.
- **Above** if the count is above the top third of those years and at least `min_fires_for_above`
  (default 2); **Below** if under the bottom third; **Normal** otherwise. The same is worked out from
  acres burned and shown beside it (the Verification tab can switch to judging by acres for comparison).
- Where a PSA has had no significant fires in that month in at least a third of past years, the bottom
  third is 0 and **Below can't happen**; the site marks those PSAs.
- **Outcome:** Correct, One category off (Above/Below vs Normal), or Opposite (Above vs Below).
- **Status:** in progress during the month, provisional for 30 days after (sizes settle, late reports),
  then final.
- **Scores:** percent correct, Heidke skill score (vs chance), the score an always-Normal forecast would
  get, and for Above and Below: how many observed months the outlook caught, how many outlooks verified,
  and issued ÷ observed.

WFIGS records before about 2020 are less complete in some areas, which can make early "normal" years
look quieter than they were. Lower `normal_years` in a GACC's settings if that's a problem.

## Weather and the neighbor checks

Every night, for every station inside a PSA:

- **RAWS** (FEMS): daily max temperature, min RH, max wind and gust, rain total; daily max **ERC** and
  **BI** (observed values, the GACC's NFDRS 2016 fuel model); and each station's 80th/90th/97th ERC
  percentiles (refreshed every 90 days).
- **ASOS/AWOS** (Iowa Environmental Mesonet daily summaries): the same weather plus the NCEI normal high
  and normal rain for each date. Turn off per GACC with `"use_asos": false`.

Each station's values are compared with its nearest neighbors (settings under `qc` in each GACC's file):

| Check | Default rule | Flag |
|---|---|---|
| Max temp too warm / too cold | more than `temp_flag_f` (8 °F), or 3 robust standard deviations, from the median of the nearest `max_neighbors` (8) within `neighbor_km` (80 km) and `max_elev_diff_ft` (3,000 ft), each adjusted to the station's elevation at `lapse_f_per_1000ft` (3.5 °F) | hot / cold |
| Rain no neighbor saw | `rain_flag_in` (0.5") or more when no neighbor got more than `dry_neighbor_in` (0.05") | rain_alone |
| Far wetter than neighbors | 1" or more and over 4 times the wettest neighbor | wet |
| Missed rain | nothing when the neighbors' median was `missed_rain_in` (0.5") or more and 60% of them got at least half that | missed |
| Impossible values | rain below 0 or above `max_daily_rain_in` (12"), temps outside −60 to 135 °F | bad_rain / bad_temp |

A value is only checked when at least `min_neighbors` (3) neighbors reported. Flagged values are left
out of PSA numbers but kept, with the reason, in `qc_flags.csv` and on the site. Over a month, a station
hot (or cold) on `persistent_days` (5) or more days is marked **runs hot** (**runs cold**), and one whose
rain total is `month_ratio` (3) times its neighbors' (or a third of it) **reads wet** (**reads dry**);
those stations are left out of that month's PSA numbers too.

PSA rain = median of good stations' monthly totals (stations with rain reported on at least 80% of the
days). Rain and temperature vs normal use ASOS/AWOS sites only (RAWS have no NCEI normals). RAWS rain
gauges aren't heated, so winter snow is undercounted.

To re-check every saved day after changing the `qc` settings, run the workflow with **rebuild** checked.

## Review notes

Anyone can add a short reason beside a PSA-month (on the Monthly summary or the Verification tab's
"Outlooks that missed"). **Save notes** opens a GitHub issue; signing in with a free account and clicking
**Submit new issue** saves them to `data/<GACC>/review_notes.csv` within a few minutes. Notes appear in
the monthly summary, PSA history and exports.

## Exporting

The **Export** tab creates an Excel workbook (verification, PSA weather, significant fires, station
months, flagged station-days, outlooks, review notes) or one CSV (one row per PSA per month per outlook,
with fires and weather beside it) for any range of months. The Monthly summary prints or saves as a PDF.

## Data sources (public, no login)

- Outlook categories: NPSG `Outlooks_Monthly_Extended` map service (fsapps.nwcg.gov); maps: NIFC
  `nifc.gov/nicc-files/predictive/outlooks/month1_outlook.png` … `month4_outlook.png`.
- Fires: NIFC WFIGS Incident Locations (all years; current and year-to-date for nightly growth and starts).
- RAWS daily weather, ERC, BI and ERC percentiles: FEMS Climatology API (`wxMinMax`, `nfdrMinMax`,
  `percentileLevels`).
- ASOS/AWOS daily summaries and normals: Iowa Environmental Mesonet (`daily.py`).
- PSA boundaries: the outlook service itself (falls back to the 7-day service).
- Map background: Esri Light Gray / Dark Gray Canvas tiles.

## Settings (config/gaccs/&lt;GACC&gt;.json)

| Key | Default | Meaning |
|---|---|---|
| `sig_fire_acres` | 100 | size a wildfire must reach to count as significant |
| `normal_years` | 10 | past years that make up "normal" |
| `min_fires_for_above` | 2 | fewest significant fires that can count as Above |
| `erc_fuel_model` | Y | NFDRS 2016 fuel model for ERC and BI |
| `use_asos` | true | include ASOS/AWOS stations |
| `fire_min_acres` | 100 | nightly growth days: growth of at least this in one day |
| `qc` | see above | neighbor-check limits |

Edit a file on GitHub and commit; the next nightly run uses it (run **rebuild** to re-check past days).
