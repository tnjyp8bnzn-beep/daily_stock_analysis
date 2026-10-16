"""Read-only monitoring journal; portfolio input uses the existing portfolio API."""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_runtime_scheduler_service

from src.config import get_config
from src.repositories.intraday_repo import IntradayRepository
from src.services.intraday_portfolio import portfolio_context
from src.services.portfolio_service import PortfolioService

router = APIRouter()


@router.get('/status')
def status(scheduler=Depends(get_runtime_scheduler_service)):
    config = get_config()
    return {'enabled': config.intraday_monitor_enabled,
            'active': scheduler.is_background_task_active('intraday_monitor'),
            'notifications_enabled': config.intraday_monitor_notify,
            'interval_seconds': config.intraday_monitor_interval_seconds,
            'mode': 'paper_signals_no_orders', 'runtime_required': 'always_on_process'}


@router.get('/events')
def events(limit: int = Query(50, ge=1, le=200)):
    return {'events': IntradayRepository().recent(limit)}


@router.get('/portfolio')
def portfolio():
    try:
        return portfolio_context(PortfolioService(), get_config().intraday_portfolio_snapshot_path)
    except Exception:
        raise HTTPException(status_code=503, detail='portfolio_unavailable_or_snapshot_invalid')


@router.get('/observations')
def observations(limit: int = Query(200, ge=1, le=5000)):
    return {'observations': IntradayRepository().observations(limit)}
