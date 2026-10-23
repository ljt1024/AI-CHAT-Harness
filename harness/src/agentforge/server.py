import os

from agentforge.api import create_app

if os.getenv("AGENTFORGE_PROTOCOL_STUB") == "1":
    from agentforge.protocol_stub import app
else:
    app = create_app()
