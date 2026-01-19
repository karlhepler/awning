# Duplicate Command Behavior

This document explains what happens when Open/Close commands are sent to an awning that is already in that state.

## TL;DR

**Sending duplicate commands is safe.** The motor's limit switches prevent any current from flowing when the awning is already at its limit position. No movement, no wear, no heat.

## Signal Path

```
Python Code → HTTP API → Bond Bridge → RF Signal → Motor Receiver → Limit Switch
```

## Layer-by-Layer Behavior

### 1. Software Layer

The automation code (`awning_automation.py`) intentionally sends commands without checking state first. This is deliberate—Bond's state tracking can drift out of sync with reality if the physical remote is used.

### 2. Bond Bridge Layer

Bond does NOT deduplicate discrete Open/Close commands. Each API call triggers a new RF transmission. The `trust_state` feature only applies to toggle-only devices (single-button remotes), not awnings with separate Open/Close signals.

Bond is essentially a "dumb relay" for discrete commands—it transmits the RF signal every time, regardless of its internal state belief.

### 3. RF Layer

Bond transmits at 433 MHz (one-way communication). There is no feedback from the motor, so Bond cannot confirm actual position.

### 4. Motor Limit Switches

This is where duplicate commands become harmless.

Tubular awning motors contain mechanical or electronic limit switches:

- A **traveling nut** on a threaded spindle moves as the motor rotates
- At preset positions, it triggers a **microswitch**
- The microswitch **breaks the electrical circuit** for that direction
- No power can flow to the motor in that direction

When you send "Open" to an already-open awning:

1. Motor controller receives RF signal
2. Attempts to power the "extend" winding
3. Limit switch is already triggered → circuit is OPEN
4. **Zero current flows**
5. Motor does not engage at all

The motor doesn't "try and stop"—it never receives power in the first place.

### 5. Thermal Protection (Backup)

Motors also have thermal overload protection via bimetallic strips that cut power if temperature exceeds threshold. This is a backup safety mechanism.

## Why Always Sending Commands Is Correct

1. **Bond's state can drift** — physical remote usage is invisible to Bond (one-way RF)
2. **Motor is inherently idempotent** — limit switches make duplicates physically harmless
3. **Fail-safe** — better to send redundant command than miss a needed one
4. **No wear** — at limits, motor receives zero power

## Sources

- Bond Local API: docs-local.appbond.com
- Somfy motor specifications
- Patent DE3240495A1 (tubular motor limit switch mechanism)
- FCC filing 2AME8BD1K (Bond Bridge Pro)
