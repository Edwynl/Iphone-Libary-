"""Start persistent local stdio worker: python -m ai_photos."""
from .config import app_home
from .logs import setup_logging
from .worker import Worker

if __name__ == "__main__":
    setup_logging("worker", app_home())
    Worker().serve()
