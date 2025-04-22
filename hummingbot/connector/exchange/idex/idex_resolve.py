import asyncio
import functools
import random
import time
from collections import defaultdict

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler

# IDEX v3 REST API url for production and sandbox (users may need to modify this someday)
from hummingbot.core.api_throttler.data_types import RateLimit

_IDEX_REST_URL_PROD_MATIC = "https://api-matic.idex.io"
_IDEX_REST_URL_SANDBOX_MATIC = "https://api-sandbox-matic.idex.io"

# IDEX v3 WebSocket urls for production and sandbox (users may need to modify this someday)
_IDEX_WS_FEED_PROD_MATIC = "wss://websocket-matic.idex.io/v1"
_IDEX_WS_FEED_SANDBOX_MATIC = "wss://websocket-sandbox-matic.idex.io/v1"


# --- users should not modify anything beyond this point ---

_IDEX_BLOCKCHAIN = None
_IS_IDEX_SANDBOX = None


def set_domain(domain: str):
    """Save values corresponding to selected domain at module level"""
    global _IDEX_BLOCKCHAIN, _IS_IDEX_SANDBOX

    if domain == "matic":  # prod matic
        _IDEX_BLOCKCHAIN = 'MATIC'
        _IS_IDEX_SANDBOX = False
    elif domain == "sandbox_matic":
        _IDEX_BLOCKCHAIN = 'MATIC'
        _IS_IDEX_SANDBOX = True
    else:
        raise Exception(f'Bad configuration of domain "{domain}"')


def get_idex_blockchain(domain=None) -> str:
    """Late loading of user selected blockchain from configuration"""
    if domain in ("matic", "sandbox_matic"):
        return 'MATIC'
    return _IDEX_BLOCKCHAIN or 'MATIC'


def is_idex_sandbox(domain=None) -> bool:
    """Late loading of user selection of using sandbox from configuration"""
    if domain == "matic":
        return False
    elif domain == "sandbox_matic":
        return True
    return bool(_IS_IDEX_SANDBOX)


def get_idex_rest_url(domain=None):
    """Late resolution of idex rest url to give time for configuration to load"""
    if domain == "matic":  # production uses polygon mainnet (matic)
        return _IDEX_REST_URL_PROD_MATIC
    elif domain == "sandbox_matic":
        return _IDEX_REST_URL_SANDBOX_MATIC
    elif domain is None:  # no domain, use module level memory
        blockchain = get_idex_blockchain()
        platform = 'SANDBOX' if is_idex_sandbox() else 'PROD'
        return globals()[f'_IDEX_REST_URL_{platform}_{blockchain}']
    else:
        raise Exception(f'Bad configuration of domain "{domain}"')


def get_idex_ws_feed(domain=None):
    """Late resolution of idex WS url to give time for configuration to load"""
    if domain == "matic":  # production uses polygon mainnet (matic)
        return _IDEX_WS_FEED_PROD_MATIC
    elif domain == "sandbox_matic":
        return _IDEX_WS_FEED_SANDBOX_MATIC
    elif domain is None:  # no domain, use module level memory
        blockchain = get_idex_blockchain()
        platform = 'SANDBOX' if is_idex_sandbox() else 'PROD'
        return globals()[f'_IDEX_WS_FEED_{platform}_{blockchain}']
    else:
        raise Exception(f'Bad configuration of domain "{domain}"')


# Pool IDs for AsyncThrottler
HTTP_PUBLIC_ENDPOINTS_LIMIT_ID = "PublicAccessHTTP"
HTTP_USER_ENDPOINTS_LIMIT_ID = "UserAccessHTTP"


RATE_LIMITS = [
    # Public access REST API Pool (applies to all Public REST API endpoints)
    RateLimit(limit_id=HTTP_PUBLIC_ENDPOINTS_LIMIT_ID, limit=5, time_interval=1),

    # User access REST API Pool (applies to all REST API endpoints that require authentication)
    RateLimit(limit_id=HTTP_USER_ENDPOINTS_LIMIT_ID, limit=10, time_interval=1),
]


_throttler = None


def get_throttler() -> AsyncThrottler:
    global _throttler
    if _throttler is None:
        _throttler = AsyncThrottler(rate_limits=RATE_LIMITS, silence_warnings=True)
    return _throttler


_ts_sleep_start = defaultdict(time.time)
_ts_sleep_window = 10


def sleep_random_start(func):
    """decorate an async function to add a small random delay on first few calls. Silence annoying throttler log msg"""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if time.time() - _ts_sleep_start[func.__name__] < _ts_sleep_window:
            await asyncio.sleep(1 + 0.5 * random.random())
        return await func(*args, **kwargs)
    return wrapper


def reset_random_start():
    _ts_sleep_start.clear()
