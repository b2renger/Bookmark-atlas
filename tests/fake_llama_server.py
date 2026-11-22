"""A stand-in for llama.cpp's llama-server, started as a real program by the
tests: it takes the same arguments, listens on the port it is given, wants the
key from LLAMA_API_KEY, and answers with the stand-in model of fake_farm.py."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_farm import FakeFarm  # noqa: E402

args = sys.argv[1:]
if "--version" in args:
    print("version: stand-in")
    sys.exit(0)
value = lambda flag: args[args.index(flag) + 1]  # noqa: E731
if os.environ.get("FAKE_LLAMA_CRASH") or not os.path.isfile(value("--model")):
    sys.exit(3)
if any(name.upper().startswith("LLAMA_") and name != "LLAMA_API_KEY" for name in os.environ):
    sys.exit(4)  # the app must not pass its own LLAMA_* variables on
time.sleep(float(os.environ.get("FAKE_LLAMA_LOAD", "0")))
farm = FakeFarm(
    password=os.environ["LLAMA_API_KEY"],
    port=int(value("--port")),
    embeddings="--embeddings" in args,
)
farm.engine = True
farm.model = value("--alias")
print("listening", flush=True)
while True:
    time.sleep(3600)
