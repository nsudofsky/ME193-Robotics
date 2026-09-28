"""Two-way MQTT chat -- a quick way to confirm you and your partner (or
opponent) can actually reach the broker and each other before match day,
without needing any of the whistling/LEGO code running. Run this on two
laptops (or two terminals) with the same --topic and type messages back
and forth; if they show up on both sides, your MQTT setup is good.

Run:
    python mqtt_chat.py                                # chat on ME193/Rogers
    python mqtt_chat.py --topic ME193/Rogers/whistling-world-cup
"""

import argparse

from mqttlib import MQTTClient

DEFAULT_TOPIC = "ME193/tasha"


def main():
    parser = argparse.ArgumentParser(description="Two-way MQTT chat for testing the broker connection.")
    parser.add_argument("--topic", default=DEFAULT_TOPIC,
                         help=f"topic to chat on (default: {DEFAULT_TOPIC!r})")
    args = parser.parse_args()

    name = input("Your name: ").strip() or "anon"

    def on_message(topic, payload):
        sender, _, text = payload.partition(": ")
        if sender == name:
            return  # skip the broker echoing our own message back
        print(f"\r{payload}\n> ", end="", flush=True)

    with MQTTClient() as client:
        client.subscribe(args.topic, on_message)
        print(f"Chatting on '{args.topic}' as {name}. Type 'quit' to exit.")

        while True:
            try:
                text = input("> ").strip()
            except (KeyboardInterrupt, EOFError):
                break
            if text == "quit":
                break
            if text:
                client.publish(args.topic, f"{name}: {text}")


if __name__ == "__main__":
    main()
