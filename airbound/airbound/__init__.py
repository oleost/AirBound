"""AirBound: secure, self-hosted remote access to Home Assistant."""

import os

# The Supervisor passes the add-on version as a build argument (see Dockerfile).
__version__ = os.environ.get("AIRBOUND_VERSION", "dev")
