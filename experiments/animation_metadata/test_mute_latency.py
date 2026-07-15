"""Latency comparison: animation with vs without the ?mute=1 sound mute.

Runs the same animation alternately with and without mute=1 (blocking
?wait=1 both times) and reports per-run and mean timings:

  - http_s     — full client-side round-trip
  - elapsed_s  — bridge-measured time (gesture + for muted runs the two
                 ssh renames, which happen inside the timed section)

The mute overhead is the difference of the means. Unmuted runs DO play
the animation's sound.

Run (on the RPi, host-side):
    python3 experiments/animation_metadata/test_mute_latency.py
"""

import json
import time

import urllib.request

# --- Configuration ---------------------------------------------------------
BRIDGE_URL = "http://localhost:5000"
ANIMATION_NAME = "Angry_1"
RUNS_PER_MODE = 4
SETTLE_SEC = 1.5            # pause between runs
TRIGGER_TIMEOUT_SEC = 60.0
# ---------------------------------------------------------------------------


def run_once(mute):
    url = "%s/animation/%s?wait=1%s" % (
        BRIDGE_URL, ANIMATION_NAME, "&mute=1" if mute else "",
    )
    request = urllib.request.Request(url, data=b"", method="POST")
    started = time.time()
    with urllib.request.urlopen(request, timeout=TRIGGER_TIMEOUT_SEC) as response:
        body = json.loads(response.read().decode("utf-8"))
    http_s = time.time() - started
    return {"http_s": http_s, "elapsed_s": body.get("elapsed_s"), "ok": body.get("ok")}


def main():
    print("latency test: %s, %d runs per mode (alternating)" % (
        ANIMATION_NAME, RUNS_PER_MODE,
    ))
    results = {False: [], True: []}
    for round_index in range(RUNS_PER_MODE):
        for mute in (False, True):
            label = "muted  " if mute else "unmuted"
            try:
                result = run_once(mute)
            except Exception as exc:
                print("  run %d %s FAILED: %s" % (round_index + 1, label, exc))
                continue
            results[mute].append(result)
            print("  run %d %s http=%.3fs bridge_elapsed=%.3fs" % (
                round_index + 1, label, result["http_s"], result["elapsed_s"] or -1,
            ))
            time.sleep(SETTLE_SEC)

    print("\nsummary:")
    means = {}
    for mute in (False, True):
        runs = results[mute]
        label = "muted" if mute else "unmuted"
        if not runs:
            print("  %s: no successful runs" % label)
            continue
        http_values = [r["http_s"] for r in runs]
        elapsed_values = [r["elapsed_s"] for r in runs if r["elapsed_s"] is not None]
        means[mute] = sum(http_values) / len(http_values)
        print("  %-7s n=%d http mean=%.3fs min=%.3fs max=%.3fs | bridge mean=%.3fs" % (
            label, len(runs),
            means[mute], min(http_values), max(http_values),
            sum(elapsed_values) / len(elapsed_values) if elapsed_values else -1,
        ))
    if False in means and True in means:
        print("\n  mute overhead: %+.3fs per animation call" % (means[True] - means[False]))


if __name__ == "__main__":
    main()
