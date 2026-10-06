import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.bootstrap import check_postgres, check_mongodb, check_minio, check_redis

router = APIRouter()
logger = logging.getLogger("uvicorn.error")


@router.get("/health/live")
async def health_live():
    return {"status": "alive"}


@router.get("/health/ready")
def health_ready():
    services = {}

    checks = (
        ("postgresql", check_postgres),
        ("mongodb", check_mongodb),
        ("minio", check_minio),
    )

    for name, check in checks:
        try:
            check()
            services[name] = "up"
        except Exception as exc:
            logger.warning("%s readiness failed: %s", name, type(exc).__name__)
            services[name] = "down"

    ready = all(value == "up" for value in services.values())

    # Reported but not required: without Redis the rate limiter counts in memory.
    try:
        services["redis"] = check_redis()
    except Exception as exc:
        logger.warning("redis readiness failed: %s", type(exc).__name__)
        services["redis"] = "down"

    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "services": services,
        },
    )
