"""Write an invented collection as a CSV file, to try the app without real data.

    python tools/demo_collection.py demo.csv
    atlas --db demo.sqlite init && atlas --db demo.sqlite import-csv demo.csv

Every title, summary and address is made up (the addresses end in .example,
a name reserved for documentation). The screenshots in the README were taken
from this collection.
"""

from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta

TOPICS = {
    "Gardening": (
        [
            "soil",
            "seedlings",
            "compost",
            "raised beds",
            "pruning",
            "mulch",
            "pollinators",
            "greenhouse",
        ],
        ["Garden", "Soil", "Vegetables"],
    ),
    "Bread baking": (
        ["sourdough", "hydration", "starter", "crumb", "proofing", "rye", "scoring", "oven spring"],
        ["Bread", "Sourdough", "Baking"],
    ),
    "Cycling": (
        [
            "gravel",
            "touring",
            "gearing",
            "tubeless",
            "bikepacking",
            "cadence",
            "frame geometry",
            "wheel building",
        ],
        ["Cycling", "Touring", "Repair"],
    ),
    "Astronomy": (
        [
            "nebulae",
            "star charts",
            "eyepieces",
            "light pollution",
            "comets",
            "meteor showers",
            "tracking mounts",
            "double stars",
        ],
        ["Astronomy", "Telescopes", "Night sky"],
    ),
    "Woodworking": (
        [
            "joinery",
            "dovetails",
            "hand planes",
            "sharpening",
            "workbench",
            "finishing oil",
            "green wood",
            "chisels",
        ],
        ["Woodworking", "Hand tools", "Joinery"],
    ),
    "Typography": (
        [
            "kerning",
            "variable fonts",
            "ligatures",
            "type specimens",
            "letterpress",
            "x-height",
            "book design",
            "grids",
        ],
        ["Typography", "Fonts", "Book design"],
    ),
    "Field recording": (
        [
            "microphones",
            "wind noise",
            "dawn chorus",
            "hydrophones",
            "ambisonics",
            "preamps",
            "soundscapes",
            "contact mics",
        ],
        ["Field recording", "Audio", "Soundscapes"],
    ),
    "Map making": (
        [
            "projections",
            "contours",
            "hillshading",
            "open data",
            "tiles",
            "labels",
            "survey",
            "cartograms",
        ],
        ["Maps", "Cartography", "Open data"],
    ),
}
# Words shared by two topics make links across topics, as in a real collection.
BRIDGES = [("Gardening", "Bread baking", "fermentation"), ("Astronomy", "Map making", "coordinates"), ("Woodworking", "Cycling", "workshop"),
           ("Typography", "Map making", "labels"), ("Field recording", "Astronomy", "night"), ("Cycling", "Map making", "routes")]  # fmt: skip
FORMS = ["Notes on {a} and {b}", "A beginner's guide to {a}", "What I learned about {a}", "{a}: a field report", "Getting better at {a} with {b}",
         "The trouble with {a}", "Tools for {a}", "Why {a} matters for {b}", "{a}, step by step", "A year of {a}"]  # fmt: skip
SITES = [
    "fieldnotes",
    "longread",
    "workbench",
    "smallhours",
    "greenshed",
    "papertrail",
    "slowroads",
    "nightdesk",
]
# How much of each topic arrives in each half-year: interests rise and fade.
WAVES = {"Gardening": [3, 5, 2, 6], "Bread baking": [6, 4, 2, 1], "Cycling": [2, 3, 5, 5], "Astronomy": [1, 2, 4, 7],
         "Woodworking": [4, 4, 3, 2], "Typography": [5, 3, 3, 3], "Field recording": [0, 1, 4, 6], "Map making": [2, 4, 4, 3]}  # fmt: skip


def rows(count: int = 360, seed: int = 7):
    rng = random.Random(seed)
    start = date(2025, 1, 6)
    names = list(TOPICS)
    for number in range(count):
        half = rng.choices(range(4), weights=[1, 1.3, 1.7, 2.2])[0]
        topic = rng.choices(names, weights=[WAVES[name][half] for name in names])[0]
        words, tags = TOPICS[topic]
        a, b = rng.sample(words, 2)
        text_words = rng.sample(words, 4)
        tag_names = (
            [tags[0]] + rng.sample(tags[1:], rng.randint(0, 2)) + [rng.choice(words).title()]
        )
        if rng.random() < 0.14:  # now and then a bookmark belongs to two interests at once
            other_words, other_tags = TOPICS[rng.choice([name for name in names if name != topic])]
            text_words += rng.sample(other_words, 3)
            tag_names += [other_tags[0], rng.choice(other_words).title()]
            b = rng.choice(other_words)
        for left, right, shared in BRIDGES:
            if topic in (left, right) and rng.random() < 0.22:
                text_words.append(shared)
                tag_names.append(shared.title())
                if rng.random() < 0.5:
                    b = shared
        title = rng.choice(FORMS).format(a=a, b=b)
        title = title[0].upper() + title[1:]
        summary = (
            f"About {topic.lower()}: "
            + ", ".join(text_words)
            + ". "
            + rng.choice(
                [
                    "A practical account with photographs.",
                    "Short, with a useful list at the end.",
                    "Long and careful; worth reading twice.",
                    "A talk written up afterwards.",
                ]
            )
        )
        published = start + timedelta(days=half * 182 + rng.randint(0, 175))
        saved = published + timedelta(
            days=rng.choice([0, 1, 1, 2, 3, 5, 8, 13, 30, 90]),
            hours=rng.randint(7, 22),
            minutes=rng.randint(0, 59),
        )
        yield {
            "Name": title,
            "URL": f"https://{rng.choice(SITES)}.example/{topic.lower().replace(' ', '-')}/{number}",
            "Summary": summary,
            "Tags": ", ".join(dict.fromkeys(tag_names)),
            "Date": saved.strftime("%B %-d, %Y %-I:%M %p (GMT+1)")
            if rng.random() < 0.8
            else published.strftime("%B %-d, %Y"),
        }


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "demo.csv"
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Name", "URL", "Summary", "Tags", "Date"])
        writer.writeheader()
        writer.writerows(rows())
    print(f"Wrote {target}")


if __name__ == "__main__":
    main()
