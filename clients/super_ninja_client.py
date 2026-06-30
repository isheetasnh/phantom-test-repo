"""
Sandbox environment helpers.

Reads /dev/shm/sandbox_metadata.json (written by entrypoint.sh) and exposes
the running environment name and derived URLs.
"""

import json
import sys
from functools import cache

SANDBOX_METADATA_FILE = "/dev/shm/sandbox_metadata.json"


@cache
def _load_sandbox_metadata() -> dict:
    try:
        with open(SANDBOX_METADATA_FILE, "r") as f:
            data = json.load(f)
            environment = data.get("environment", "")
            if environment:
                return {"environment": environment}
            else:
                print(f"⚠️ Sandbox metadata missing environment", file=sys.stderr)
                return {}
    except FileNotFoundError:
        print(f"⚠️ {SANDBOX_METADATA_FILE} is missing", file=sys.stderr)
        return {}
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️ Error reading sandbox metadata: {e}", file=sys.stderr)
        return {}


def get_super_ninja_url() -> str:
    """Return the URL where client can buy more credit, e.g. https://super.myninja.ai/"""
    metadata = _load_sandbox_metadata()
    env = metadata.get("environment", "")
    prefix = env if env and env != "prod" else ""

    return f"https://super.{prefix}myninja.ai"
