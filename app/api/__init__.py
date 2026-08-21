"""API v1 routes."""
from fastapi import APIRouter

from app.api.v1 import search, predict, admin, terminal, crypto_checkout

router = APIRouter()
router.include_router(search.router, prefix="/search", tags=["search"])
router.include_router(predict.router, prefix="/predict", tags=["predict"])
router.include_router(admin.router, prefix="/admin", tags=["admin"])
router.include_router(terminal.router, prefix="/terminal", tags=["terminal"])
router.include_router(crypto_checkout.router, prefix="/crypto", tags=["crypto"])

__all__ = ["router"]
