"""Start persistent local stdio worker: python -m ai_photos."""
from .worker import Worker

if __name__ == "__main__":
    Worker().serve()
