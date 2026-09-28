"""Wait for the instructor's "start" message on ME193/Rogers, then prompt
for a role and launch whistling.py -- for when you'd rather leave a
laptop idly watching for the match to begin than have whistling.py
already running and calibrated.

IMPORTANT TRADEOFF: whistling.py's own hardware connect + calibration
(noise floor, then all 6 recorder notes -- tens of seconds) all happen
*after* this launches it, since "start" is what triggers the launch in
the first place. That means real delay between the match actually
starting and the car being ready to drive. For an actual live match,
running `python whistling.py --role ball` (or goalie) directly ahead of
time -- calibrated and idle, waiting for "start" itself -- gets you
driving the instant the match begins instead. Use this script when that
delay doesn't matter (e.g. testing, or you don't know your role until
the trigger fires).

Sound input (which microphone) isn't asked here -- whistling.py already
prompts for that itself once launched, so this script leaves it alone
rather than duplicating that picker.

Run:
    python wait_and_launch.py
"""

import subprocess
import sys
import time

from mqttlib import MQTTClient

START_TOPIC = "ME193/Rogers"  # instructor-assigned start trigger (do not change)


def main():
    print(f"Waiting for 'start' on {START_TOPIC} ...")
    received = {"started": False}

    def on_start(_topic, payload):
        if payload.strip().lower() == "start":
            received["started"] = True

    with MQTTClient() as client:
        client.subscribe(START_TOPIC, on_start)
        while not received["started"]:
            time.sleep(0.2)

    print(f"'start' received on {START_TOPIC}!")

    role = ""
    while role not in ("ball", "goalie"):
        role = input("Role (ball/goalie): ").strip().lower()

    print(f"Launching whistling.py --role {role} -- you'll be prompted to pick your microphone next.")
    subprocess.run([sys.executable, "whistling.py", "--role", role])


if __name__ == "__main__":
    main()
