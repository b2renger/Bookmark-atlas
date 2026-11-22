# ruff: noqa: E501
"""Does the bundled Python reach this system's keychain?

Run by the release workflow on each platform with the bundled interpreter:
    python -I keychain-selfcheck.py <path to backend/app>
It stores, reads back and deletes one made-up value through atlas.settings,
exactly as the app does, and says where it went. Not a gate: a build machine
may have no keychain session (Linux without a desktop), and then the app
falls back to a private file, which this also shows.
"""

import os
import sys
import tempfile

sys.path.insert(0, sys.argv[1])
scratch = tempfile.mkdtemp()
os.environ["ATLAS_CONFIG_DIR"] = os.path.join(scratch, "data")
os.environ["ATLAS_SECRETS_DIR"] = os.path.join(scratch, "private")
os.environ.pop("ATLAS_NO_KEYRING", None)

from atlas import settings  # noqa: E402

try:
    import keyring

    print(
        "keyring backend:",
        type(keyring.get_keyring()).__module__,
        type(keyring.get_keyring()).__name__,
    )
except Exception as error:  # noqa: BLE001
    print("keyring not usable:", type(error).__name__, error)

name = "selfcheck-made-up"
where = settings.set_secret(name, "made-up-value-not-a-secret")
back = settings.get_secret(name)
settings.delete_secret(name)
gone = settings.get_secret(name) is None
in_data = os.path.exists(os.path.join(scratch, "data", "secrets.json"))
print(
    f"stored in: {where} | read back: {back == 'made-up-value-not-a-secret'} | deleted: {gone} | file in data folder: {in_data}"
)
sys.exit(0 if back and gone and not in_data else 1)
