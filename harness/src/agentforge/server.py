import os

from agentforge.api import create_app
from agentforge.logging_config import configure

# Configure before the app is built so import-time problems are formatted the
# same way as request-time ones.
configure()

if os.getenv("AGENTFORGE_PROTOCOL_STUB") == "1":
    from agentforge.protocol_stub import app
else:
    app = create_app()
