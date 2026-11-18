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
| `.github/workflows/backfill.yml` | fills in history back to 2023 every 6 hours, then turns itself off |
| `.github/workflows/apply-notes.yml` | saves review notes sent from the website |

## One-time setup

1. **Create a new repository** on GitHub named `PS-Seasonal-Verification`, Public, empty.
2. **Upload everything** from this folder, including the hidden `.github` folder (on a Mac,
   Cmd+Shift+. in Finder shows it). `.github/workflows` must contain `archive.yml`, `backfill.yml` and `apply-notes.yml`.
3. **Settings → Actions → General → Workflow permissions →** "Read and write permissions" → Save.
4. **Settings → General → Features →** make sure **Issues** is ticked (review notes use issues).
5. **Settings → Pages →** Branch `main`, folder `/ (root)` → Save.
6. **Actions → Archive monthly outlook and check fires and weather → Run workflow.** The first run
   downloads the PSA boundaries, every RAWS and ASOS/AWOS station in the country and 11 years of
   wildfire records, so it can take a while (expect 20–60 minutes). Later nightly runs are faster.
   To fill in earlier months, see "Filling in past months" below.
7. **History fills itself in:** the "Backfill history" workflow runs every 6 hours, loads past outlooks
   from the NIFC archive, and fills in weather and fire starts back to January 2023, then turns itself off.
   To start it right away: Actions → "Backfill history" → Run workflow. See "Filling in past months" below.

## Schedule

- **Every day at 03:00 UTC** (8 pm MST / 9 pm MDT the evening before): station weather, ERC and BI, the
  neighbor checks (the newest day only, after the one-time backfill), significant fires, fire growth and
  new starts, and the scores. Fire growth from an
  evening run is credited to the day that's ending.
- **Monthly outlook: the 1st at 20:00 UTC** (1 pm MST / 2 pm MDT), with **backups on the 2nd and 3rd** at
  the same time. The backups do nothing if the new month's outlook was already saved. If it still isn't
  in the service after the 3rd, that run fails (GitHub emails you); run the workflow by hand once it's
  posted. A correction NIFC makes later in the month is picked up the next time the workflow is run by hand.
- Scores are recomputed on every run.

Each GACC's "day" and "month" follow that GACC's time zone (set in its settings file).

## Filling in past months

| Data | How far back | How |
|---|---|---|
| Outlooks | 2023 onward (NIFC archive); older by email from NICC | workflow input **import_archive** (`all` or `2023-2026`) |
| Significant fires and normal | every year WFIGS has (about 2014 onward) | automatic; the fire history reaches back to cover the earliest outlook |
| New-fire starts per PSA per day | any date (WFIGS discovery dates) | **backfill_all** (once), or **backfill_from** |
| Fire-growth days | only from the first nightly run (needs a size check each night) | n/a |
| ASOS/AWOS weather | full history (IEM) | **backfill_all** (once), or **backfill_from** / **backfill_to** |
| RAWS weather, ERC, BI | as far back as FEMS serves daily summaries (try it; the run log shows days it couldn't get) | **backfill_all** (once), or **backfill_from** / **backfill_to** |

**Backfill workflow (`.github/workflows/backfill.yml`):** runs every 6 hours (01, 07, 13, 19 UTC) and
fills in history a little at a time, oldest first, back to **January 1, 2023** (change with the workflow's
**start** input):
- first run: loads past outlooks from the NIFC archive and recounts new-fire starts back to the start date;
- every run: about 4 months of station weather (RAWS, ERC, BI, ASOS/AWOS) (**days_per_run**, default 120),
  then commits and re-scores.

From January 2023 that's about 12 runs, so it's caught up in about 3 days. When nothing is left to fill,
the last run **turns the workflow off** by itself (Actions tab → "Backfill history" → **Enable workflow**
turns it back on, e.g. to go further back). It shares a queue with the nightly job, so they never run at
the same time. Days the services can't supply are tried twice, then skipped (`data/wx_backfill_log.csv`).
To do it all in one go instead, run the main workflow once with **backfill_all** ticked.

**After that, each nightly run only adds the newest day** (and fetches the two days before it again, since
some stations report late). If a night is missed, the next run fills the gap (up to 10 days back).

To fill a particular stretch instead, backfill a few months per run (e.g. 2026-01-01 to 2026-04-30, then 2026-05-01 to 2026-08-31, ...), since
each run is limited to 5½ hours. Every step skips work already done, so a run can be repeated safely.

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
a zip are saved under `data/raw/outlooks/<issue>/` as the record.

**The NIFC archive zips (2023 onward) hold only map images and PDFs**, so `scripts/read_outlook_maps.py`
reads the categories off the US outlook maps: it lines each map up with the PSA boundaries (fitted to the
map's own PSA and GACC lines; Alaska fitted as its own inset, Puerto Rico read from its island) and reads
the color inside each PSA (red = Above, green = Below, otherwise Normal). Checked against the November 2026
service it matched all 936 PSA-months. A map that doesn't line up is skipped, not guessed; a map drawn
without PSA lines uses the alignment of other maps with the same layout. The import log
(`data/archive_import_log.csv`) records the fit for every map. PSAs are read with today's boundaries, so
where a PSA was redrawn since, the category of most of its area is used. Issues already saved from the
live service are never overwritten. Imported outlooks are scored right away against the fire records,
and the fire history automatically reaches back far enough to give them a full normal (years with too
few WFIGS records, before about 2014, are left out of normal). For weather in those months, run the
workflow with **backfill_from** one year at a time (e.g. 2023-01-01 then 2024-01-01), if FEMS and IEM still
have it; IEM keeps full history.

Zips you have yourself (for example emailed older years): run
`python scripts/import_archive.py --zip file1.zip file2.zip` and commit the changed `data/` files.

## How a month is judged

Every PSA-month is judged several ways. Each gives Above, Normal or Below:

| Measure | What it looks at | Above / Below when |
|---|---|---|
| **Fires vs past years** (main) | significant fires (`sig_fire_acres`, 100 ac) discovered that month in the PSA | more than the top third of the same month in the previous `normal_years` (10) years, and at least `min_fires_for_above` (2) fires / fewer than the bottom third |
| **Fires, fair for quiet places** | the same count, judged by how unusual it is for that PSA's average (Poisson chance) | a count that high happens less than 1 time in 3 at that average / a count that low does. Lets quiet PSAs come out Below |
| **Acres burned** | acres of those fires | top / bottom third of past years |
| **Big incidents (ICS-209)** | wildfires that had an ICS-209 report, any size | top / bottom third of past years (left out if a GACC has no 209 records) |
| **Dry fuels (ERC)** | days the PSA's ERC reached its stations' 90th percentile (normally 10% of days) | `fuels_above_share` (20%) of days or more / `fuels_below_share` (3%) or fewer; needs `min_fuel_days` (15) days |
| **All measures together** | the four above combined (Above +1, Normal 0, Below -1, averaged) | average +1/3 or more / -1/3 or less; needs two measures |

Station ERC percentiles come from FEMS where it serves them, otherwise from each station's own saved
history (a year or more of days, `data/erc_percentiles_own.csv`); the backfill's last run re-checks every
month once that history is in. WFIGS fire records are complete enough from about 2014; earlier years are
left out of "normal" automatically. Where a PSA has had no significant fires in a month in at least a
third of past years, Below can't happen by the main measure; the site marks those PSAs.

**Outcome:** right (same category), one step off (Above or Below vs Normal), or wrong way (Above vs
Below). **Status:** in progress during the month, provisional for 30 days after (sizes settle, late
reports), then final.

**The Verification tab's report card** puts it in plain words: how often the outlook was right; how that
compares with always saying Normal and with repeating last month's result; a skill score (Gerrity: 0 = no
better than guessing, 1 = perfect, with a one-word rating); and what happened when the outlook said Above
or Below. Options: judge by any measure; only months when fires are expected (normally 1+ fire); and
count each PSA-month equally or by how many fires are normal there. The Heidke skill score is under
"More numbers".

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
