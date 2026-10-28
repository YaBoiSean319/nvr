"""An NVR for a console to federate in a test: the production application without its background workers, served by ``uvicorn tests.fixtures.site_app:app``."""
from app.application import create_app

app = create_app(start_workers=False)
