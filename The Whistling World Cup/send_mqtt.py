"""Publish a message to an MQTT topic -- by default "start" on
ME193/Rogers, the instructor's shared topic.

Useful for testing the *real* MQTT flow in whistling.py (i.e. without
--practice) without waiting for the instructor to publish "start" for
real: run this once after both cars are connected and waiting, and they
should both start driving.

Since ME193/Rogers is a shared topic the whole class uses, only publish
"start" on it when you actually mean to start a real or test match --
if anyone else's car is listening on the same topic at the same time,
this will start theirs too.

Run:
    python send_mqtt.py                                    # publishes "start" to ME193/Rogers
    python send_mqtt.py --message "caught:ball" --topic ME193/Rogers/whistling-world-cup
"""

import argparse

from mqttlib import MQTTClient

DEFAULT_TOPIC = "ME193/Rogers"
DEFAULT_MESSAGE = "start"


def main():
    parser = argparse.ArgumentParser(description="Publish a message to an MQTT topic.")
    parser.add_argument("--topic", default=DEFAULT_TOPIC,
                         help=f"topic to publish to (default: {DEFAULT_TOPIC!r})")
    parser.add_argument("--message", default=DEFAULT_MESSAGE,
                         help=f"message to publish (default: {DEFAULT_MESSAGE!r})")
    args = parser.parse_args()

    with MQTTClient() as client:
        print(f"Publishing {args.message!r} to {args.topic!r} on test.mosquitto.org...")
        client.publish(args.topic, args.message)
        print("Sent.")


if __name__ == "__main__":
    main()
