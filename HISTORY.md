# The Awning Chronicles: A Story of Automation, Failure, and Enlightenment

## Act I: The Birth of an Idea

A motorized awning, controlled by a handheld remote, requiring human judgment to operate. Is it sunny? Is it windy? Is the sun even hitting the window? Every 15 minutes of decision fatigue that could—*should*—be automated.

**Commit c49efdf** was humble: a bash script. Auto-discovery via mDNS, basic commands, environment variables. The kind of quick hack that gets written on a Saturday afternoon. It worked. Barely.

But then came the **first pivot** (2a07e33). Bash wasn't going to cut it. The script needed to be *programmatic*, separable into domain logic and CLI presentation. Enter Python. Enter Nix flakes for reproducibility. The architecture that would define everything: `awning_controller.py` holds the truth, `awning.py` makes it pretty.

A principle emerged early: **the controller should never print, never exit. It raises exceptions.** The CLI catches them and displays what humans want to see. Clean separation. This decision paid dividends later.

---

## Act II: The Weather Intelligence

**Commit 153fb4e** was ambitious. Suddenly this wasn't just a remote control—it was a brain.

Five conditions to check before opening:
1. Is it sunny? (cloud cover)
2. Is it calm? (wind speed)
3. Is it dry? (precipitation)
4. Is it daytime? (sunrise/sunset)
5. Is the sun even facing the window? (azimuth 90°-180°)

Open-Meteo API for weather. The *pvlib* library for solar position calculations using the NREL Solar Position Algorithm—the same one NASA uses. For an awning.

The decision logic was simple: ALL conditions must be true to open. ANY failure closes. Fail-safe by design.

---

## Act III: The Deployment Saga (or, How Containers Betrayed Us)

With the brain built, it needed a home. An Orange Pi 3 LTS, the kind of $35 ARM board that lives in a closet and runs forever on 2 watts.

**Commit 3523da5** went full Docker. A Podman container. Professional. Reproducible. *Correct*.

Then reality hit:

**66b5436**: "Fix version check by disabling network" — Pasta (the network namespace tool) wasn't available on the server. First crack.

**21058b5**: "Fix cron environment for rootless Podman" — Cron doesn't know about XDG_RUNTIME_DIR. Rootless containers need that. Hours of debugging for one environment variable.

And then, the capitulation: **56b2484** — *"Replace Docker deployment with direct Python on Orange Pi"*.

The commit message reads like a surrender letter:
> Delete Dockerfile (no longer using containers)

Sometimes the boring technology wins. A Python venv in `~/.config/awning/`. Pip install. Cron runs Python directly. It just *works*.

The lesson: **containers are for servers, not embedded devices.** Fight the battles that matter.

---

## Act IV: The State Synchronization Problem

**Commit 1a01a54** solved a subtle bug that took real-world observation to discover.

The original code was clever: check the awning's state, only send a command if it differs from the desired state. Efficient! No unnecessary API calls!

But clever broke. The Bond Bridge tracks state—it remembers "open" or "closed." But what happens when someone uses the physical remote? The Bond doesn't know. Now its state is *wrong*, and the automation trusts that lie.

The fix was humility: **always send the command.** If the weather says close, send close. If it's already closed? The motor hits its limit switch and stops harmlessly. The awning doesn't care about efficiency. It cares about being correct.

Notifications now only fire when state *actually* changes (comparing before vs after), not when commands are sent. Intent preserved, reliability restored.

---

## Act V: The Sunlight Wars

The most fascinating technical saga spans four commits and reveals how theory meets reality.

**Round 1 (153fb4e)**: Use cloud cover percentage. Simple.

**Round 2 (e0545a6)**: Wait, cloud cover is crude. What about *shortwave radiation*? The actual W/m² hitting the ground! More scientific! Better data!

**Round 3 (82f6b2a)**: Hmm, shortwave radiation can be high on cloudy days because diffuse light scatters through clouds. Add cloud cover *back* as a second check.

**Round 4 (6433288)**: The final reckoning.

The commit message tells the whole story:
> Shortwave radiation was giving false positives - showing "sunny" (200+ W/m²) when it was visually grey and overcast. This is because the measurement includes diffuse radiation scattered through clouds, not just direct sunlight.

The sophisticated metric was *worse* than the naive one. The awning doesn't care about total energy—it cares about glare. Direct sun. Cloud cover handles that.

A hard lesson: **more data isn't always better data.** The right abstraction for the problem beats the most technically accurate measurement.

Also in this commit: the sun altitude threshold dropped from 30° to 15° "based on observed sun clearing trees around 9am." Real-world calibration. No simulation can tell you where the neighbor's oak tree ends and the window begins.

---

## Act VI: The Logging Trilogy

The final three commits before HEAD form a comedy of errors worthy of their own chapter.

**ba4c9cc**: Add daily log rotation! Symlinks! Auto-cleanup! Beautiful.

**69ade03**: "Fix duplicate log entries from cron" — The cron job redirected stdout to file. But Python's FileHandler *also* wrote to file. Double everything.

Fix: discard stdout, capture only stderr.

**89516ee**: "Fix duplicate log entries by explicitly configuring handlers" — That didn't work. Clear existing handlers first.

**82b8211**: "Fix duplicate log entries by logging to stderr only" — Nuclear option. Remove FileHandler entirely. Let cron handle the file. One source of truth.

**Three commits** to fix logging. Each one certain it was the solution. Each one revealing another layer of the onion. Python's logging module is powerful, but its handler accumulation behavior across imports is a trap that has claimed many developers.

---

## Epilogue: What Remains

The final commit (**9d8dbb3**) is documentation. Explaining to future maintainers—or future Claude sessions—why it's safe to send duplicate commands. The signal path from code through Bond Bridge to motor limit switches.

The system as it stands today:
- 7 weather conditions, all must pass
- Fail-safe: closes on API failure
- Notifications via Telegram
- Daily log rotation with 30-day retention
- Simple Python venv deployment
- Exponential backoff for transient failures

From a bash script to a sophisticated weather-aware automation system. Through containers and back out. Through radiation sensors and back to cloud cover. Through clever state tracking to humble command repetition.

The awning opens when the sun shines on a calm, clear day. It closes when anything changes. It doesn't think it's smarter than reality. And that's the whole point.

---

*24 commits. One awning that finally takes care of itself.*

---

*Written by Claude, who co-authored every commit.*
