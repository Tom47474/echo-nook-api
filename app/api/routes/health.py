from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """供部署探针确认 HTTP 进程已完成启动。"""
    return {"status": "ok"}
