"""Analysis worker entrypoint. Run with ``python -m gallery.worker``."""

import logging

from gallery.config import get_settings
from gallery.observability import init_observability

logger = logging.getLogger(__name__)


def main() -> None:
    """Start the worker. The polling loop arrives with #11."""
    init_observability(get_settings(), "worker")
    logger.info("worker not implemented yet")


if __name__ == "__main__":
    main()
