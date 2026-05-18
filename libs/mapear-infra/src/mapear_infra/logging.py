"""Loguru configuration for structured JSON logging."""

import sys

from loguru import logger

from mapear_infra.config import get_settings


def setup_logging() -> None:
    """Configure loguru based on environment settings."""
    settings = get_settings()

    logger.remove()

    if settings.log_format == "json":
        logger.add(
            sys.stderr,
            level=settings.log_level,
            serialize=True,
        )
    else:
        logger.add(
            sys.stderr,
            level=settings.log_level,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
        )

    logger.info(
        "Logging initialized",
        environment=settings.environment.value,
        log_level=settings.log_level,
    )
