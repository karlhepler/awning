# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Reference

```bash
# CLI commands
nix run . -- open|close|stop|toggle|status|info

# Weather automation
nix run .#automation -- --dry-run
nix run .#automation -- --env-file=/path/to/.env

# Development shell
nix develop
python3 awning.py open
python3 awning_automation.py --dry-run

# Deploy to Orange Pi (USER ONLY — requires interactive password)
./deploy.sh
```

## 🚨 Git Workflow — Commit Directly to Main (NO branches, NO PRs)

**This repo commits DIRECTLY to `main`. Always. No exceptions, ever.**

- ✅ Commit straight to `main` and push to `origin/main`.
- ❌ NEVER create a branch. ❌ NEVER open a pull request. ❌ NEVER use the `karlhepler/` branch-naming convention here.
- This overrides any global guidance about draft PRs, branch prefixes, or PR descriptions — none of that applies to this repository.
- Workflow for every change: commit to `main` → `git push` → tell the user "ready to deploy" (the user runs `./deploy.sh`).

## Project Overview

Bond Bridge awning controller - sends HTTP commands to control a motorized awning via the Bond Local API v2. Written in Python 3 with Nix flakes for reproducible dependency management.

## Architecture

**Core Domain (`awning_controller.py`):**
- `BondAwningController` class - Core business logic for awning control
- `create_controller_from_env()` - Factory function to create controller from env
- Custom exceptions: `ConfigurationError`, `BondAPIError`
- No dependencies on CLI libraries - can be used independently
- All methods raise exceptions instead of printing/exiting

**CLI Interface (`awning.py`):**
- `AwningCLI` class - Handles user-facing command execution
- Uses `rich` library for colorful output
- Catches exceptions from controller and displays user-friendly error messages

**Weather Automation (`awning_automation.py`):**
- Automatically opens/closes awning based on weather and sun conditions
- Uses Open-Meteo API (free, no API key) for weather data. Makes a **second, independent** Open-Meteo request for ECMWF + ICON direct irradiance (`fetch_crosscheck_irradiance`) used only to overturn a false "not sunny" from the primary `best_match` feed; it is skipped on most runs and fails open.
- Cross-checks live RainViewer NEXRAD radar (free, no API key) as an independent rain signal; decodes radar tiles with Pillow (PIL). The PIL import is lazy and fails open, so a missing Pillow never crashes the automation — radar simply disables and the Open-Meteo signals continue to guard.
- Uses pvlib for solar position calculations (NREL SPA algorithm)
- Imports `awning_controller` for awning control

**Configuration Loading:**
- `.env` file loaded from current working directory first, then script directory
- This allows `nix run . -- <command>` to work correctly from the project directory

**Command Flow:**
1. CLI parses arguments (simple manual parsing, no argparse)
2. Create controller via `create_controller_from_env()`
3. CLI calls controller method
4. Controller sends HTTP request to Bond API
5. CLI catches exceptions and displays formatted output

**Bond API:**
- Base URL: `http://{BOND_HOST}/v2/devices/{DEVICE_ID}`
- Authentication via `BOND-Token` header
- Actions: `Open`, `Close`, `Stop`, `ToggleOpen`
- State endpoint returns `{"open": 1}` or `{"open": 0}`

## Weather Automation

**Irradiance smoothing (applies to the sunny gate's GHI and DNI inputs):**

The primary `best_match` feed resolves to GFS/HRRR here, and its 15-minute radiation series is **not physically continuous**. On 2026-08-16 its GHI read `130 → 20 → 760 → 170` W/m² across four consecutive slots while ECMWF held `701/708/710/707` and ICON held `500/521/541/555` over the same slots. GHI cannot change 38× in fifteen minutes under any real sky, so a single instantaneous sample of that series is noise, not a measurement.

The sunny gate therefore reads the **median of the trailing hour** (`_SMOOTHING_SLOTS` = 4) for GHI and DNI, exposed by `fetch_weather()` as `ghi_smoothed` / `dni_smoothed`. Median, not max — max would amplify the bogus 760 W/m² spike exactly as badly, just in the other direction. The fields ride on the request's **existing** `minutely_15=` block, so this costs no extra HTTP call and adds no `models=` key (which would break Layer 1's UV arm — see the cross-model rescue below).

**Fails open**: an absent, short, or all-null series falls back to the instantaneous `current` value, so missing data never hardens the gate. **Scoped to the sunny gate only** — the rain gate, including the `RADAR_VETO_DNI_WM2` clear-sky veto, deliberately keeps reading the instantaneous `dni`; that veto was calibrated across three separate false-close incidents, and a smoothed (lower) DNI would make it harder to trigger and re-open the radar-clutter closes it exists to prevent. Every run logs an `Irradiance smoothing:` line showing raw → smoothed for both fields.

Measured over 14 days (420 daytime 15-min slots), smoothing plus the Tier-2 rescue below take the gate from **349 sunny slots / 33 state flips** to **365 slots / 17 flips** — more open time and half the churn. The two changes are complementary: the rescue recovers stuck-closed time, smoothing removes the flapping.

**Decision Logic (ALL 7 conditions must be met to open):**
1. **Sunny**: multi-layer model — ALL three layers must be true (GHI and DNI here are the *smoothed* values above):
   - **Model layer**: GHI >= `MIN_GHI_WM2` (default 400 W/m²) OR (UV >= `MIN_UV_INDEX` (default 4.0) AND total cloud cover < `MAX_CLOUD_COVER_PCT`) OR DNI >= `MIN_DNI_DIRECT_WM2` (default 450 W/m²). The **direct-beam bypass** exists because GHI and UV are both measured on a *horizontal* plane and are therefore suppressed by the sin(altitude) projection at low sun elevation — at a 22.5° sun altitude a horizontal surface collects only ~38% of the beam, so on a cloudless summer morning GHI physically cannot reach 400 W/m² until roughly 09:10 no matter how strong the sun is. The window is vertical and takes a low morning sun nearly head-on, so GHI measures the wrong plane for this application. DNI is measured normal to the sun and is not projection-suppressed, so it recognizes morning sun that horizontal irradiance cannot see. Mirrors the existing `MIN_DNI_CIRRUS_WM2` guard on the overcast ceiling, which already lets direct irradiance override a cloud-derived verdict. Added after the 2026-08-13 incident where the awning stayed shut at 08:30 on a clear 78°F morning with DNI=498 W/m² and 0% cloud, and had to be opened by hand; the 450 default sits above that morning's 08:15 reading of DNI=445 (so it does not open earlier than intended) and far above the DNI 0–50 range of a bright-overcast morning, which is what keeps diffuse light from defeating it. The **UV arm is gated by cloud cover** (reusing `MAX_CLOUD_COVER_PCT`, not a new threshold) — added after the 2026-08-21 incident, where UV read 7.5 and kept the awning open under a 96% low-cloud deck (METAR: overcast at 1500 ft) while GHI from the *same feed* read 173 W/m², a ~4x disagreement. Checked over 60 days against Open-Meteo's own `uv_index_clear_sky` field: `uv_index` sits within 5% of the clear-sky value on 31% of heavy-cloud hours (total cloud >= 90%), so it is not reliably cloud-attenuated on its own.
   - **Consistency layer**: DNI >= `MIN_DNI_WM2` (default 50 W/m²) OR total cloud cover < `MAX_CLOUD_COVER_PCT` (default 80%)
   - **Overcast ceiling**: max(cloud_cover_low, cloud_cover_mid, cloud_cover_high) < threshold (default 95%) OR DNI >= `MIN_DNI_CIRRUS_WM2` (default 30 W/m²). The DNI guard bypasses the ceiling when direct irradiance proves the sun is reaching the ground — added after the 2026-05-12 incident where Open-Meteo's `cloud_cover_high` field hallucinated 100% on a clear day with DNI=905 W/m². `cloud_cover_low` was added to the max() after the 2026-08-21 incident: a solid low deck (mid=0%, high=0%) left the ceiling blind to the commonest kind of overcast, and the awning stayed open for hours under a sky METAR reported as OVC015 (overcast at 1500 ft).
   - **Cross-model rescue** (`SUNNY_CROSSCHECK_ENABLED`, default on): consulted ONLY when all three layers above return "not sunny". Fetches DNI from **ECMWF (`ecmwf_ifs025`) and ICON (`icon_seamless`)** — two models independent of the primary feed — and overturns the verdict when **both** report DNI >= `MIN_DNI_DIRECT_WM2`. Implemented in `fetch_crosscheck_irradiance()`.

     **Why it exists.** All three layers above read GHI, UV, DNI and cloud from a *single* Open-Meteo `best_match` response, which resolves to **GFS/HRRR** at this location. On **2026-08-14** GFS reported DNI 0 W/m² and `cloud_cover_high` 100% from 08:30 onward on a cloudless 82°F morning; the awning opened at 08:30, closed at 08:45, and stayed shut all morning. Same timestamp at 10:00 — GFS: DNI 0, cloud 100%; ECMWF: DNI 574, cloud 56%; ICON: DNI 754, cloud 17%. The UV index (a CAMS product, unaffected) meanwhile ramped smoothly 1.0 → 4.2 with no dip, independently confirming clear sky.

     **The design principle this encodes:** the `MIN_DNI_CIRRUS_WM2` guard on the line above was added for *exactly* this shape of bug (hallucinated `cloud_cover_high`), yet it did not fire — it needs DNI >= 30 to bypass the ceiling, and this time **DNI itself was the corrupted field**. A guard that reads a primary field can be disarmed by that field going bad. So the rescue's inputs are entirely independent of the primary feed: it shares no field, and its 15-minute slot selection rebuilds "now" from its own response's `utc_offset_seconds` rather than trusting the primary payload's `current.time`.

     **Why a second HTTP request** rather than adding `models=` to the existing call — both verified against the live API: (a) the `current=` block **silently ignores** multi-model requests, returning one unsuffixed set rather than both; (b) requesting an explicit non-GFS model returns `uv_index: null`, since UV is a CAMS product ECMWF/ICON do not carry, which would break the Layer 1 UV arm outright. `minutely_15=` *does* support multi-model and returns suffixed keys (`direct_normal_irradiance_ecmwf_ifs025`). So `fetch_weather()`'s params must stay exactly as they are — a test asserts `"models" not in params`.

     **Why BOTH models** rather than either: sized empirically over 14 days of history, requiring both fires on only **3–4% of daylight hours**, every one a genuinely sunny hour where GFS undershot. Threshold sensitivity is flat between 350 and 500 W/m², so it reuses `MIN_DNI_DIRECT_WM2` (450) rather than introducing a fourth DNI threshold.

     **Two tiers.** The rescue has a second, strictly narrower tier added after the 2026-08-16 incident:

     - **Tier 1 — full rescue** (bar `MIN_DNI_DIRECT_WM2`): overrides all three layers. The 2026-08-14 case, where the primary feed's entire radiation block collapsed.
     - **Tier 2 — consistency rescue** (bar `MIN_DNI_WM2`, 50): satisfies **only Layer 2**, and only when Layers 1 and 3 already pass on their own. Layer 3 is *not* bypassed — a test pins this, and it is the main way this tier could regress into the full rescue.

     **Why Tier 2 exists.** On **2026-08-16** the awning closed five runs in a row (12:15–13:15) on a broken-cloud afternoon the operator wanted shade for. Only Layer 2 failed: primary DNI 0 W/m² and cloud 100%, while ECMWF read 335 and ICON 192 — both 4–7× the `MIN_DNI_WM2` bar of 50, both far below the 400 bar Tier 1 compares against. UV held 6.2–7.3 throughout, independently confirming strong sun. The rescue was *holding* evidence that flatly contradicted `DNI=0` and discarding it, because it asked the wrong question:

     | question | correct bar |
     |---|---|
     | "Is there enough direct beam to call it sunny on its own?" | 400 — Layer 1 |
     | "Is the primary feed's claim of DNI=0 believable?" | 50 — Layer 2's own bar |

     Reusing `MIN_DNI_DIRECT_WM2` for both conflated them. Tier 2 asks the models **the failing layer's own question at the failing layer's own bar**, so it introduces no fourth DNI threshold. `MIN_DNI_WM2` now does double duty (Layer 2 bar *and* Tier 2 rescue bar), the same way `MIN_DNI_DIRECT_WM2` already does.

     **Sizing, measured over 92 days.** Only **28 daytime hours** have Layer 1 passing while Layer 2 fails. Rescuing at bar 50 opens 15 dry hours and 4 hours within ±1h of rain — and all 4 of those had UV 4.2–7.8, i.e. genuine sun beside a scattered shower. Genuinely dark rainy hours are rejected outright (2026-06-19 10:00: ECMWF 6, ICON 2). The rain gate is independent of the sunny verdict and still closes on observed wetness regardless, so Tier 2 cannot open the awning into falling rain.

     **The 92-day sizing above answered the wrong question.** It counted how many of Tier 2's 28 rescuable hours were *rainy* versus *dry*, on the reasoning that rain was the only failure mode worth guarding against. On **2026-08-21** the same near-identical fixture as the 2026-08-16 incident recurred (GHI 180, UV 7.5, DNI 0, cloud low 96%/mid 0%/high 0%, ECMWF 350, ICON 469) under a sky METAR reported as solidly overcast (OVC015) — dry, not rainy, and Tier 2 opened the awning into it anyway. The question that mattered was never "is it raining," it was **"is it sunny,"** and nothing in the original sizing checked that. No available signal, including a real METAR observation, separates the two days at decision time — the difference is what the sky did next (08-16's deck broke 18 minutes later; 08-21's never did). This is a policy call, and the operator chose to lean toward closing: solid-overcast afternoons shaped like 08-16 now stay closed, in exchange for correctly closing on 08-21's numbers. Implemented upstream of Tier 2 itself — see the Model layer's UV cloud gate and the Overcast ceiling's `cloud_cover_low` above — both of which now make the *original* 08-16 fixture fail Layer 1 and Layer 3, not just Layer 2, so Tier 2's own precondition no longer holds for it. `TestSolidOvercastRegression20260821` pins the real 12:15/12:30/12:45/13:00 cron readings from that day so this cannot be walked back silently; `TestConsistencyRescue` keeps a distinct `BROKEN_CLOUD_WEATHER` fixture (GHI carries Layer 1, no cloud layer >= 95%) to prove Tier 2 still fires for a genuine broken-cloud afternoon.

     **Rescue-only and fail-open.** Neither tier can flip `sunny` True → False, so neither can cause a close that would not already happen — a monotonicity test pins this. Any fetch error, timeout, null value, missing slot, or slot older than 60 minutes returns "no rescue" and reverts to primary-feed-only behavior. It skips the request entirely when the primary already says sunny, when coordinates are absent, or when the daytime/altitude/azimuth gates are closed — so most runs, including every nighttime run, make no extra request. Every run logs a `Sun crosscheck:` line, and the `Decision:` string names the rescue when it engages. Note it does **not** write to the `_attribution` out-param: that is consumed positionally by `build_close_reason()`, whose first branch is the rain branch, so a sunny string there would make an unrelated close announce a sun message.
   - **Observed-ceiling veto** (`METAR_STATION`, optional, off when unset): the mirror image of the cross-model rescue, and the only sunny-gate signal that looks at the *actual* sky. Runs LAST, after the three layers and both rescue tiers, and sets `sunny` False when the nearest airport's latest METAR reports a **broken (BKN), overcast (OVC) or obscured (VV) layer below `METAR_CEILING_FT`** (default 3000 ft, the standard aviation low-ceiling line). FEW/SCT layers and high cirrus (BKN250) never veto. Implemented in `fetch_metar()` (aviationweather.gov, no API key); config read by `get_sky_observation_config()`, kept out of `get_thresholds()` so its positional 16-tuple is untouched.

     **Why it exists — 2026-10-03.** Every other input to the sunny gate (GHI, UV, DNI, cloud, and both rescue models) is *forecast model output*. At 11:15 the primary feed read 15% cloud / DNI 645 / GHI 601 and the awning stayed open while KRDU (13.7 km away) had gone `BKN016 OVC250` at 11:04 (it was `SCT016` at 10:51) and KTTA (Sanford) reported `+RA` at 10:35. KJNX read `BKN013`. The operator saw "super cloudy and getting darker." Only ICON saw it (cloud 79%, DNI falling 391→251); ECMWF agreed with GFS. This is the opposite of the 08-14/08-16 incidents, where the model invented cloud that was not there — the rescue can only turn "not sunny" into "sunny", so nothing in the pipeline could catch a model that *missed* cloud. Radar guards rain only.

     **Close-only, and an observation outranks a model:** it overrides the cross-model rescue, but it has no code path that sets `sunny` True, so it can never cause an open. A test pins both.

     **Fail-open, like the radar and the crosscheck:** any error, empty reply, unknown station, a layer with a null base, or a report older than 90 minutes (`_METAR_MAX_AGE_MIN`) means no veto — exactly the forecast-only behavior. METARs arrive hourly (~:51) plus a SPECI whenever the sky changes, so a report can legitimately be ~60 minutes old. The fetch is skipped when the forecast already says not-sunny or the daytime/altitude/azimuth gates are closed, so most runs make no extra request.

     **Backtested against the RDU archive (Iowa Environmental Mesonet):** 08-14 clear morning (SCT/BKN at 15–25k ft) → no veto; 08-16 10:42–11:51 (OVC 1100–1600) → veto; 08-16 12:33+ broken cloud (SCT019 **BKN060**) → no veto; 08-21 (OVC 1500) → veto; 10-03 10:51 (SCT016) → no veto; 10-03 11:04 (BKN016) → veto, closing at the 11:15 run. **Only four days of evidence:** it supports any cutoff between 1,700 and 6,000 ft — raising `METAR_CEILING_FT` past 6000 would close the 08-16 afternoon the operator wanted open. RDU is 13.7 km away, so local sky can differ; that is the cost of a close-only check.

     Every run logs a `Sky observation:` line, and the `Decision:` string names `OBSERVED CEILING VETO` when it fires. **The Telegram close message names the veto** (`☁️ Awning closed: Low cloud deck — KRDU reports BKN016`) rather than "Not enough sun" with the forecast's healthy GHI/DNI — the same self-contradiction the rain attribution fixed for rain closes. It travels in its own `_sky_veto` out-param on `should_open_awning()`, NOT in `_attribution`, which `build_close_reason()` reads positionally as a *rain* signal. Rain still outranks it in the message priority. A composition-root test drives the real `main()` into a Telegram send, since a dry-run returns before the notification block.
2. **Calm**: BOTH mean wind < `WIND_SPEED_THRESHOLD_MPH` (default 15.0 mph) AND gusts < `WIND_GUST_THRESHOLD_MPH` (default 25 mph), each checked against the **worse of the model and the airport's METAR** (`evaluate_wind()`). Observed wind can only make the gate stricter: a missing, stale (> 60 min, `_METAR_WIND_MAX_AGE_MIN`) or unparseable (`VRB`) report reverts to model-only, and a null model gust is skipped rather than treated as windy. Needs `METAR_STATION` for the airport half; the model half (`wind_gusts_10m`, in the existing `current=` request, no `models=`) works without it.

   **Why — 2026-09-05.** The gate used to compare only the model's *mean* 10 m wind to 15 mph. At 15:09 KRDU recorded **25 mph sustained with a 44 mph gust** while the model said 9 mph mean / 10 mph gust, and the Pi log has the awning **open at 15:00 and 15:15 reporting ~6 mph**; it closed at 15:30 only because radar showed rain. Over six weeks of daytime KRDU reports (444) the model missed 5 of 5 sustained >= 15 mph and 7 of 7 gusts >= 25 mph (small sample). Replayed against 30 days of real decisions the new gate changes **5 of 838 opens (0.6%)**: the 09-05 15:15 miss, plus 4 runs on 09-25 where the model's own gust forecast was high. **The 25 mph default is not a rating**: the awning's own wind sensor is installer-programmed (Dooya sensors take 0.1–180 km/h, shown on the sensor's LCD) and Skyview does not publish its trigger speed — read it off the sensor and align this value. The Telegram close message names the source (`💨 … Too windy (gusts 44 mph (KRDU))`) rather than the model's calm mean, via its own `_wind_alert` out-param.
3. **No rain (multi-signal gate)**: the awning closes only on **observed wetness** or a forecast signal that is not vetoed by provably-clear sky conditions. Signals are divided into two categories:

   **Observed signals** (close on any positive reading; never suppressed by any veto):
   - Open-Meteo `precipitation` > 0 mm — current-slot actual precipitation
   - Open-Meteo `minutely_15` precipitation in the last ~30 min > 0 — recent actual precipitation lookback
   - **RainViewer NEXRAD radar** shows precipitation over the configured `LATITUDE`/`LONGITUDE` when at least 2 pixels in a 3×3 window around the target location are **wet**, where a pixel counts as wet only if its alpha channel is >= `_RADAR_MIN_ALPHA` (200). Two independent guards work together here:
     - **Alpha floor (`_RADAR_MIN_ALPHA` = 200).** RainViewer's color scheme 2 renders returns in two disjoint classes: real precipitation at alpha **exactly 255** on a saturated reflectivity ramp (light blue 136,221,238 → deep blue 0,71,104 → yellow → orange → red 193,0,0, channel spread 85–255), and sub-threshold clear-air returns on a fixed 25-entry **faded** ramp at alpha 20,25,30,…,180,190 in desaturated grey-khaki (99,97,89 → 222,208,151, channel spread only 10–71). Sampling six tiles over this location found 10,030 pixels at alpha 255 and 13,786 on the faded ramp, with **nothing between alpha 190 and 255** — RainViewer deliberately fades everything below its precipitation threshold. The 200 default sits in that empty gap. Added after the 2026-08-13 13:15 incident: three pixels at alpha 20/25/20, RGB ~(99,97,89), closed the awning on a cloudless 92°F afternoon (DNI 789 W/m², cloud_low 0%, precipitation 0.00 mm, precipitation_probability 0%) with no rain anywhere in the forecast. The old `alpha > 0` test could not distinguish a 10%-opacity ghost from a storm core. This also subsumes the 2026-06-24 clutter pixel, RGBA (158,147,117,110), which likewise sits on the faded ramp.
     - **Neighborhood count (`_RADAR_MIN_WET_PIXELS` = 2).** A lone wet pixel is still treated as noise; real precipitation cells light up many adjacent pixels and trivially exceed the threshold.
     
     At zoom 6 each pixel is ~2446 m at the equator (~1984 m at this latitude), so the 3×3 window spans roughly 6 km. This is a live radar observation, independent of the Open-Meteo forecast model, so it catches storms the hourly model has not yet ingested. The radar check fails open: any fetch/parse error (or a missing Pillow dependency) returns "no radar rain" so it can never wedge the awning closed. A single radar tile is sampled (no adjacent-tile lookup); see the in-code note for the tile-boundary caveat. Every run logs the max alpha seen in the window, so a future false reading can be diagnosed from the log alone.
     - **Clear-sky radar veto:** the radar signal is ignored when `DNI >= RADAR_VETO_DNI_WM2` (default 650 W/m²) AND `max(cloud_cover_low, cloud_cover_mid) < RADAR_VETO_CLOUD_PCT` (default 15%). The cloud condition looks ONLY at the rain-bearing low/mid layers, NOT total cloud cover — thin high cirrus does not produce rain but can push total cloud cover well above the ceiling while full sun still reaches the ground. NEXRAD operates in clear-air mode on hot, dry days and renders non-precipitation echoes (insect/bird biological scatter, ground clutter, anomalous propagation) as faint pixels; the neighborhood decode (multiple adjacent radar pixels required) substantially reduces lone-clutter false closes, and the clear-sky veto provides a further backstop when DNI and rain-bearing cloud cover both prove the sky is unambiguously clear. The veto suppresses ONLY the radar arm and ONLY when independent measurements prove the sky is clear — real rain falls from low/mid clouds (which lift `max(cloud_cover_low, cloud_cover_mid)` above the ceiling) and collapses DNI, so BOTH veto conditions fail during genuine rain and it cannot engage. The 650 W/m² DNI default sits well above the 486 W/m² reading from the 2026-06-23 incident for margin. Each veto is logged. Added after the 2026-06-24 clutter incident (radar pixel RGBA 158,147,117,110 closed the awning on a clear 80°F afternoon, DNI=790 W/m², 3% cloud); the rain-bearing-layer cloud condition was added later the same day after thin high cirrus (total cloud 22–52%, but low 2%/mid 0%, DNI 888 W/m²) defeated the original total-cloud-cover veto and re-closed the awning.

   **Forecast signals** (subject to the provably-clear forecast veto):
   - Open-Meteo `precipitation_probability` (current hour) >= `RAIN_PROBABILITY_THRESHOLD` (default 20%)
   - Open-Meteo `weather_code` is a drizzle/rain/snow/shower/thunderstorm WMO code
     - **Provably-clear forecast veto:** when actual `precipitation == 0` AND `max(cloud_cover_low, cloud_cover_mid) < RADAR_VETO_CLOUD_PCT` (default 15%), forecast signals are suppressed — rain cannot fall from air with no rain-bearing clouds regardless of what the NWP model predicts. Only observed wetness (actual precip, minutely_15 rain, or radar-confirmed precipitation not vetoed by the clear-sky radar veto) closes the gate in this state. Real rain always produces elevated low/mid cloud cover, so this veto cannot engage during genuine precipitation. Added after the 2026-06-27 incident where a forecast signal (precipitation_probability or weather_code) closed the awning at 11:00 while cloud_low=10%, cloud_mid=2%, precipitation=0.0 mm (bone dry, clear sky).

   A missing/null signal is treated as rain (bias toward closed). Each cron run logs a `Rain signals:` diagnostic line recording every signal's value and whether the forecast veto engaged. When the gate closes, the `Decision:` log message names the exact signal(s) that fired and their values — ending the whack-a-mole pattern where `Raining (0.0 mm/h)` provided no attribution.

   **The Telegram close notification carries the same attribution**: `🌧️ Awning closed: Rain signal — radar(NEXRAD)`. Only one of the five signals is `precipitation`, so the old message — which printed the precipitation value on every rain close regardless of which signal fired — announced `Rain starting (0.0 mm/h)` for radar- and probability-triggered closes. That is truthful about precipitation and reads as a self-contradiction, and it named nothing to investigate; the operator reported it repeatedly before the 2026-08-13 radar-clutter close. The attribution string is the same one `evaluate_rain_gate()` already builds for the log, forwarded to `main()` through the `_attribution` out-param on `should_open_awning()`. It cannot travel in the `conditions` dict, which must stay all-bool because `should_open = all(conditions.values())`. `build_close_reason()` falls back to the precipitation value when no attribution is supplied. A dry-run cannot exercise this path (dry-run returns before the notification block), so a composition-root test drives the real `main()` end to end and asserts the sent message names the signal.
4. **Above minimum temperature**: Temperature > `MIN_TEMPERATURE_F` (default 45°F; was 60°F prior to commit `24ebd12`)
5. **Daytime**: Between sunrise and sunset
6. **Sun high enough**: Altitude >= `MIN_SUN_ALTITUDE_DEG` (default 15°, pinned to 12° in the deployed `.env`). Unlike every other threshold in this section, this one had never been calibrated against real observation — it has exactly one commit in its history, the one that introduced it, justified only as "accounts for trees blocking the sun," no incident, no measurement. That went unnoticed for months because the sun-position math itself (`pvlib`, NREL SPA) is exact and already fully accounts for how the sun's real altitude at a given clock time shifts with the seasons — the threshold only needed calibrating once, in true-altitude terms, to then hold correctly year-round; nobody had done that yet.

   **2026-09-28 incident.** The operator manually opened the awning at 8:15 (sun visibly strong through the window) and again at 8:30 (even stronger) — the automation didn't open on its own until 8:45, once the cross-model rescue engaged. Comparing sun altitude at the same clock times on 2026-08-14 (the day the DNI-rescue thresholds above were tuned) versus today, computed directly with `pvlib`:

   | Time | Aug 14 altitude | Sep 28 altitude |
   |---|---|---|
   | 8:00 | 16.3° | 9.7° |
   | 8:15 | 19.4° | 12.7° (operator: should be open) |
   | 8:30 | 22.4° | 15.6° (operator: should be open) |

   On 08-14 the sun had already cleared the old 15° floor before 8:00, so the altitude gate was never the binding constraint that day — the tuning session that produced `MIN_DNI_DIRECT_WM2=400` never exercised this edge. By late September, a ~30-minute-later sunrise means the same 15° crossing happens around 8:28 instead — real seasonal drift in sunrise time, not a bug in the angle math. The operator's two data points (12.7° and 15.6°, both confirmed "should be open") are the first direct calibration this threshold has ever had; 12° was chosen to sit just under the earlier confirmed point (12.7°), the same margin convention used for the azimuth ceiling below, rather than extrapolating past what was actually observed (8:00's 9.7° reading is not confirmed either way).

   **This does not guarantee 8:15 opens on every similar morning.** At 8:15 today the sunny gate (condition 1) was *also* failing independently — the cross-model rescue is only queried once the altitude/azimuth/daytime gates are already open, so its 8:15 numbers were never collected (skipped) and it's not known whether it would have cleared 400 W/m² that early. If a future clear morning still closes at 8:15 with the sunny gate as the named blocker, that gate's own thresholds are next in line for this same direct-observation treatment — not a sign the altitude fix was wrong.
7. **Sun facing window**: Azimuth between `SUN_AZIMUTH_MIN_DEG` (default 60°) and `SUN_AZIMUTH_MAX_DEG` (default 249°). The arc describes a **southeast-facing** window that receives sun from sunrise until late afternoon (~4pm in mid-August). **Both bounds come from the operator's direct observation of when that window is actually in sun — never from geometry**, and both have needed correcting.

   - **Floor (90° → 60°, 2026-08-13).** The original hardcoded arc was 90°–260°, which describes a *south*-facing window. The 90° floor blocked real morning sun: the operator observed sun on the glass at azimuth 87.9°, and the floor would also have blocked summer-solstice 08:30 at azimuth 79.9° (verified by computing the code path's own output across both solstices). This half was correct and stands.
   - **Ceiling (260° → 215°, 2026-08-13 — WRONG).** The same commit dropped the ceiling to 215° on the reasoning that "the 260° ceiling admitted late-afternoon sun at azimuth 230° that never reaches this window at all." **That claim was untested and false.** Unlike the floor error it had no user-visible symptom driving it — it was a guess made while fixing a real bug next to it.
   - **Ceiling (215° → 249°, 2026-08-14).** On 2026-08-14 the operator watched the house shadow cross the back porch and identified **16:01** as the moment the awning should retract — azimuth **249.5°** (the 16:00 cron log recorded 249.3°). Sun was demonstrably still on the porch at 245°, past the 215° ceiling. That day the 215° ceiling closed the awning at **14:30** (azimuth 220.5°), giving up **~1.5 hours** of afternoon shade. 249° is still below the original 260°, so this is a correction toward the observed truth, not a blanket revert. `TestAzimuthCeilingObservedCalibration` pins all four bracketing cron slots (14:30/15:45/16:00/16:15) so the ceiling cannot be walked back silently again.

   **The ceiling is a proxy for time-of-day and drifts seasonally.** In Nov–Jan the sun never reaches azimuth 249° while still above `MIN_SUN_ALTITUDE_DEG`, so the altitude gate closes the awning instead (~15:15–15:45). That is the correct fail-direction for low winter sun and needs no separate handling.

   Both bounds are env-tunable, so the arc can be recalibrated on the device **without redeploying code**.

If ANY condition fails, the awning closes.

**Fail-safe (`_fail_safe_close()`):** if the automation cannot make a trustworthy decision it closes the awning. That now covers the weather API being unavailable **and any unexpected exception raised before a command has been sent** (previously only `WeatherAPIError` did, so an ordinary bug between "got the weather" and "told the awning what to do" left it wherever it was — open, in a storm). It **always sends Close** rather than only when Bond reports open: Bond's state drifts whenever the physical remote is used (one-way RF, see `OPEN_ON_OPEN.md`), and a duplicate Close at the limit switch is harmless. Telegram stays quiet when Bond already said closed, so a long outage does not send 96 identical messages a day. It is deliberately **not** triggered by `ConfigurationError` (a deploy-time typo) or by anything after the command is sent — a Telegram, log-cleanup or heartbeat failure must never undo a correct decision (`actuated` flag). It uses the same `--env-file` as the run. Tests: `TestFailSafeClose`.

**Each cron run acts immediately on the current conditions** — all conditions met opens the awning, any condition failing closes it, with no debounce or vote-counting between runs. (An earlier anti-flapping hysteresis that required two consecutive "open" votes was removed on 2026-06-24: Open-Meteo's irradiance/cloud data is hourly so it does not jitter between 15-min runs, rain-driven close is already immediate, and the RainViewer clear-sky veto removed the main flap source — so the debounce only added a ~30-minute open lag.)

**Data-source decisions (researched 2026-10-03, so they are not re-litigated blind):**
- *Why observations at all:* every sunny-gate input except the airport veto is Open-Meteo `best_match` (GFS/HRRR here). Against measured sunshine (USCRN Durham, 31 km away, 30 days, 242 daytime hours) the forecast called GHI >= 400 in 207 hours and the measurement was below 400 in 28 of them; the reverse error was 3 of 35. On 2026-10-03 at 11:45 the Open-Meteo models read best_match/HRRR 664, ECMWF 544 and ICON 351 W/m² (15-minute GHI, `historical-forecast` API), against 44.9 W/m² measured by ECONet REED at 11:55 — a 6% of clear-sky reading.
- *Best nearby measured sunshine: NC State ECONet* — REED (Reedy Creek field lab, 9.1 km, pyranometer, 1-minute data), then CAMP (14.6 km) and LAKE (15.3 km). Fields `sr`, `sr_clear_sky`, `sr_percent` are a ready-made "is the sun out" signal; the web page's JSON feed answers without a key (verified), and the documented CLOUDS API needs a free key (`accounts.climate.ncsu.edu`, 2,000 requests/month). **Not wired in yet** — waiting on the key so the cutoff can be checked against that station's own history. Calibration so far (USCRN Durham measured / pvlib clear-sky): hours the operator wanted closed read 26–37% of clear sky, hours wanted open read 63–104% (11 labelled hours, small sample). ECONet wind units are unverified, so ECONet wind gates nothing.
- *Airport:* KRDU (13.7 km, the only ASOS in range) via aviationweather.gov; AWOS stations are 27+ km. Another 5-minute KRDU feed exists at `api.weather.gov/stations/KRDU/observations` (10–16 min old, low cloud only) — not needed yet.
- *Rejected:* Synoptic (paid after a trial), Weather Underground / Ambient / Tempest (owner-only), CWOP citizen stations (activity and solar sensors unverifiable), USCRN live (31 km, ~55 min old; backtests only), GOES files (4–40 MB), IEM 1-minute ASOS (1.5 days late), Open-Meteo satellite (no North America). *Candidates to shadow-log, not trust yet:* nowCOAST 1 km radar and GOES visible point queries (they disagreed with IEM MRMS at the house, 21 vs 0).
- *The awning's own wind sensor* (Skyview 16×10 ft, Dooya motor): installer-programmed, 0.1–180 km/h on a Dooya sensor, trigger speed not published. Read it off the sensor.

**Logging:**
- Every run emits six diagnostic lines alongside `Conditions:`/`Decision:` — `RainViewer:` (radar pixel decode), `Rain signals:` (every rain signal's value and whether the forecast veto engaged), `Sun crosscheck:` (per-model DNI, the slot read, and whether a rescue engaged — naming which tier — / was skipped / was unavailable), `Sky observation:` (the airport's lowest BKN/OVC/VV layer, the report's age, and whether the observed-ceiling veto fired / was skipped / was unavailable / is disabled), `Wind observation:` (model and airport mean/gust, the worst of each against its limit), and `Irradiance smoothing:` (raw → smoothed GHI and DNI). Each is designed so a false reading can be diagnosed from the log alone.
- Daily log rotation in `logs/` directory as `awning-YYYY-MM-DD.log`
- Symlink at `~/awning.log` always points to today's log
- Auto-cleanup after 30 days (configurable via `LOG_RETENTION_DAYS`)
- View logs: `tail -f ~/awning.log`

**Heartbeat monitoring (optional, `HEARTBEAT_PING_URL`):** every run that completes the full fetch → decide → actuate → cleanup cycle without an exception pings this URL (`ping_heartbeat()` in `awning_automation.py`), fire-and-forget with no retries — a failed ping just means this run doesn't count, which is correct whether the network blipped or the far end is down. Nothing that lives *on* the Pi can detect the Pi itself being down, so this deliberately pushes the check off-box: point it at a dead-man's-switch service (e.g. healthchecks.io) that alerts when a check-in goes missing, rather than reacting to one that arrives. Added after the 2026-08-29 incident where the Pi was completely unreachable for 2 days 7.5 hours — cron never ran, so nothing in the process could have logged or alerted, and the operator only found out by noticing the awning open at the wrong time of day. It is not called on `--dry-run` (which returns before this point) or on any exception path — a heartbeat during a real outage would silence the alarm it exists to raise. See `.env.example` for setup.

## Deployment

**🚨 The user runs `./deploy.sh` — Claude must NEVER run it.** Deployment requires interactive sudo/password input on the remote Orange Pi that only the user can provide. Claude's responsibility ends at `git commit` + `git push`; the user handles the actual deploy from their own terminal. Attempting to invoke `deploy.sh` (directly, via `bash -x`, via a sub-agent, via SSH, etc.) WILL fail because Claude cannot supply the password, and the attempt wastes tool budget plus produces misleading error output. When code is pushed and ready, tell the user "ready to deploy" and stop — wait for them to run it.

**Checking logs or running other read-only commands on the Pi via SSH is fine for Claude to do directly — but it must go through `lan-run`, not plain `ssh`.** On this Mac, a macOS Local Network privacy restriction (stricter since macOS 26.7) silently blocks LAN/SSH connections made from a process running inside tmux — the exact environment Claude normally runs in here — with a misleading "No route to host" error that looks like a network problem, not a permissions one. `lan-run` (a shellapp from `~/.config/nixpkgs`, always on `PATH`) routes the command around that block. Use it for anything SSH-based that Claude runs itself: `lan-run ssh karlhepler@orangepi3-lts "tail -50 ~/awning.log"`. This is unrelated to the `deploy.sh` restriction above — that one is about Claude not having the interactive password, not about this network block; `lan-run` does not change or lift the `deploy.sh` rule. Full background: `~/.config/nixpkgs`'s `modules/system/lan-run.bash` and its own memory note under that project.

**Target:** Orange Pi 3 LTS running Debian (`karlhepler@orangepi3-lts`, or `karlhepler@$PI_HOST` when `PI_HOST` is set in `.env` — see below)

**`orangepi3-lts` is not mDNS.** The Pi has no `avahi-daemon` installed (confirmed via `systemctl is-enabled avahi-daemon` → `not-found`), so `orangepi3-lts.local` has never resolved. The bare `orangepi3-lts` name has only ever worked via the router's own DHCP/DNS hostname registration, and that registration can go stale after a router reboot or a long Pi outage — on 2026-08-31 the Pi was down for 2 days 7.5 hours, and even after it came back the name stayed unresolvable on the operator's Mac (a stuck macOS resolver cache; `dig @<router-ip>` resolved it fine the whole time, `ssh`/`ping` did not). Set `PI_HOST` in `.env` to the Pi's IP (ideally backed by a router DHCP reservation, same as `BOND_HOST`) to stop depending on that registration; `deploy.sh` reads it with a fallback to `orangepi3-lts` so nothing changes for a setup where the name already resolves.

**Deploy script (`deploy.sh`):**
1. Discovers Bond Bridge IP via mDNS (using `BOND_ID` from `.env`)
2. Sends Telegram notification (deploy start)
3. Creates Python venv on remote if needed
4. Installs dependencies via pip — **`deploy.sh` carries its own hardcoded package list** (`requests python-dotenv rich pvlib pandas pytz tenacity Pillow`); it does NOT read `requirements.txt`. 🚨 When adding a new runtime dependency you MUST add it to BOTH `requirements.txt` (for local/Nix dev) AND the pip-install line in `deploy.sh` (for the Pi), or the deploy will crash on import.
5. Copies scripts and `.env` to `~/.config/awning/`
6. Configures cron job (every 15 minutes)
7. Runs dry-run verification
8. Sends Telegram notification (deploy complete)

**Remote structure:**
- Scripts: `~/.config/awning/awning_automation.py`, `awning_controller.py`
- Config: `~/.config/awning/.env`
- Venv: `~/.config/awning/venv/`
- Logs: `~/.config/awning/logs/awning-YYYY-MM-DD.log`
- Symlink: `~/awning.log` -> today's log file

## Environment Variables

See `.env.example` for full documentation. Key variables:

**Required for CLI:**
- `BOND_TOKEN` - Bond Bridge auth token
- `BOND_HOST` - Bond Bridge IP address (set up DHCP reservation)
- `DEVICE_ID` - Device ID for the awning

**Required for automation:**
- `LATITUDE`, `LONGITUDE` - Location for weather/sun calculations
- `WIND_SPEED_THRESHOLD_MPH` - Max mean wind speed (mph) to open awning; no default, must be set
- `WIND_GUST_THRESHOLD_MPH` - Max gust (mph) to open awning, checked against the worse of the model and the airport METAR (default: 25). Not a rating — align it with your awning's own wind sensor setting (see condition 2)
- `MIN_SUN_ALTITUDE_DEG` - Min sun altitude (degrees above horizon); no default, must be set. **The deployed `.env` pins it to 12**, lowered from 15 on 2026-09-28 — see Decision Logic condition 6 above for the first-ever direct-observation calibration of this threshold

**Optional for automation (have defaults):**
- `MIN_GHI_WM2` - Min global horizontal irradiance W/m² for Layer 1 sunny gate (default: 400)
- `MIN_UV_INDEX` - Min UV Index for Layer 1 sunny gate (default: 4). Gated by `MAX_CLOUD_COVER_PCT` (not a new threshold) — a high UV reading only satisfies Layer 1 when total cloud cover is also below that bar, since `uv_index` alone is not reliably cloud-attenuated (see the Model layer note above; 2026-08-21 incident)
- `MIN_DNI_WM2` - Min direct normal irradiance W/m² for Layer 2 consistency check (default: 50). **Does double duty**: it is also the Tier-2 cross-model *consistency rescue* bar, so raising it tightens both
- `MAX_CLOUD_COVER_PCT` - Max total cloud cover % for Layer 2 consistency check (default: 80). **Does double duty as of 2026-08-21**: also gates the Layer 1 UV arm (see `MIN_UV_INDEX` above)
- `MIN_TEMPERATURE_F` - Min temperature °F to open awning (default: 45)
- `OVERCAST_THRESHOLD_PCT` - Layer 3 hard ceiling: max(cloud_cover_low, cloud_cover_mid, cloud_cover_high) must be below this % (default: 95). `cloud_cover_low` was added to the max() on 2026-08-21 — a solid low deck previously passed this ceiling outright because it only read mid/high
- `MIN_DNI_CIRRUS_WM2` - Layer 3 DNI guard: bypasses overcast ceiling when DNI >= this W/m² (default: 30)
- `RAIN_PROBABILITY_THRESHOLD` - Min Open-Meteo precipitation-probability % (current hour) that closes the rain gate (default: 20)
- `RADAR_VETO_DNI_WM2` - Clear-sky veto: ignore the RainViewer radar signal when DNI >= this W/m² AND cloud cover is below `RADAR_VETO_CLOUD_PCT` (default: 650)
- `RADAR_VETO_CLOUD_PCT` - Rain-bearing (max of low/mid layer) cloud cover % ceiling shared by both the radar veto and the forecast veto; high cirrus is excluded. Radar veto: suppresses a false radar rain signal when ALSO DNI >= `RADAR_VETO_DNI_WM2`. Forecast veto: suppresses forecast-only signals (probability, weather_code) when precipitation==0 (no DNI condition required) (default: 15)
- `MIN_DNI_DIRECT_WM2` - Layer 1 direct-beam bypass: DNI at or above this W/m² satisfies the model layer on its own, regardless of GHI and UV. Distinct from `MIN_DNI_WM2` (Layer 2 consistency) and `MIN_DNI_CIRRUS_WM2` (Layer 3 ceiling guard) — three separate DNI thresholds serving three different layers (default: 450). **This bar does double duty**: it is also the cross-model rescue bar, so lowering it loosens both the primary bypass and the rescue. **The deployed `.env` pins it to 400**, lowered on 2026-08-14 because at 450 the rescue still left a 15-minute gap at 08:45 (ECMWF read 433). Checked over 14 days of history, 400 clears the bypass on 19 extra 15-min slots, all at the edges of the day — and every one outside 08:15–08:45 is already blocked by the azimuth ceiling (evening sun sits at 271–284°, past the ceiling — which was 215° when this was measured and is 249° as of 2026-08-14; both block it) or by the sun-altitude gate. This supersedes the 2026-08-13 calibration note above, which chose 450 to keep an 08:15 reading of DNI=445 closed.
- `SUN_AZIMUTH_MIN_DEG` - Near edge of the sun-facing-window arc, in degrees. Below this the sun has not yet come round onto the window. Lower it if the awning is late to open on summer mornings (default: 60)
- `SUN_AZIMUTH_MAX_DEG` - Far edge of the sun-facing-window arc, in degrees. Above this the sun has passed off the window. Raise it if the awning closes while sun is still on the glass. Calibrated 2026-08-14 from the operator watching the house shadow reach the porch at 16:01 — see condition 7 above for why this bound has been wrong twice (default: 249)
- `SUNNY_CROSSCHECK_ENABLED` - Cross-model sun confirmation kill switch, governing **both** rescue tiers. When on, a "not sunny" verdict from the primary feed is overturned if ECMWF and ICON both report DNI >= `MIN_DNI_DIRECT_WM2` (Tier 1, overrides all layers), or if they both report DNI >= `MIN_DNI_WM2` while only Layer 2 is blocking (Tier 2, consistency rescue). Rescue-only and fail-open — see the sunny-gate section above for the 2026-08-14 and 2026-08-16 incidents. Accepts true/1/yes/on or false/0/no/off (default: true)
- `METAR_STATION` - ICAO code (3-4 characters) of the nearest airport, e.g. `KRDU`. Enables the observed-ceiling veto (see the sunny gate above, 2026-10-03 incident); unset or blank leaves it off. Close-only and fail-open. **The deployed `.env` must set it** — `deploy.sh` copies `.env` as-is, so this is an operator edit, not a code change (no default; off when unset)
- `METAR_CEILING_FT` - Cloud base in feet above ground below which a broken/overcast/obscured layer vetoes sunny (default: 3000, the aviation low-ceiling line). Backtested on four days only; must be > 0

**Optional:**
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - For notifications
- `LOG_RETENTION_DAYS` - Days to keep logs, integer >= 1 (default: 30). An invalid value (`30d`, `0`, a negative number) logs a warning and uses 30; it never raises and never deletes today's log
- `BOND_ID` - For mDNS discovery in deploy.sh
- `HEARTBEAT_PING_URL` - Dead-man's-switch URL (e.g. healthchecks.io) pinged after every successful run; alerts off-box when the automation stops running entirely (see Logging above)
- `PI_HOST` - Overrides the `orangepi3-lts` hostname `deploy.sh` SSHes to; not mDNS (avahi isn't installed on the Pi) — see Deployment above

## UI/UX Guidelines

- Emojis inline with text (not in separate table columns - avoids alignment issues)
- Color scheme: cyan (actions), green (success), red (errors), yellow (warnings)
- Controller returns raw data, CLI formats it for display
