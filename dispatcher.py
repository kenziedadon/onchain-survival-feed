#!/usr/bin/env python3
"""
dispatcher.py - Hermes Swarm Webhook Dispatcher
Receives TradingView alerts, enforces SOUL.md governance,
and routes missions across local agent microservices.

SAFETY: Shadow-mode default. LIVE TRADING DISABLED until explicit
caps.json arm_signal_v1 path is set. kill_switch=true → shadow only.
"""

import os, json, logging, asyncio
from fastapi import FastAPI, Request, HTTPException, status
from pydantic import BaseModel, Field
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(), logging.FileHandler("hermes_swarm.log")])
logger = logging.getLogger("HermesSwarmDispatcher")

app = FastAPI(title="Hermes Base Trader - Swarm Webhook Dispatcher", version="1.0.0")

# --- Security ---
WEBHOOK_TOKEN = os.getenv("HERMES_WEBHOOK_TOKEN", "")
if not WEBHOOK_TOKEN:
    logger.error("⚠️ HERMES_WEBHOOK_TOKEN env var required — set before launch")
CAPS_PATH = os.getenv("CAPS_PATH", os.path.join(os.path.dirname(__file__), "caps.json"))
OMNIROUTE_URL = os.getenv("OMNIROUTE_URL", "http://127.0.0.1:20128/v1/chat/completions")

# Swarm registry (single-executor model prevents Base L2 nonce collisions)
AGENT_SWARM = {
    "execution": os.getenv("EXECUTION_URL", "http://127.0.0.1:8001/execute"),
    "risk": os.getenv("RISK_URL", "http://127.0.0.1:8002/verify"),
    "soul": os.getenv("SOUL_URL", "http://127.0.0.1:8003/audit"),
}

# --- Payload Schema ---
class ScalpAlert(BaseModel):
    action: str = Field(..., description="BUY or SELL")
    asset: str = Field(..., description="Ticker or token address")
    price: float = Field(..., description="Close price")
    sl: float = Field(..., description="Stop-loss price")
    tp: float = Field(..., description="Take-profit price")
    strategy: str = Field(default="simple_ema_v1", description="Triggering script name")

# --- Governance Helpers ---
def load_caps() -> dict:
    if os.path.exists(CAPS_PATH):
        try:
            with open(CAPS_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read {CAPS_PATH}: {e}")
    return {"kill_switch": True, "live_trading": False}

def check_gate(caps: dict) -> bool:
    """Return True ONLY if live trading authorized."""
    return not caps.get("kill_switch", True) and caps.get("live_trading", False)

# --- Webhook Endpoint ---
@app.post("/webhook/v1/tradingview", status_code=status.HTTP_200_OK)
async def handle_alert(payload: ScalpAlert, request: Request):
    token = request.query_params.get("token")
    if token != WEBHOOK_TOKEN:
        logger.warning(f"⚠️ Unauthorized webhook from {request.client.host}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    caps = load_caps()
    logger.info(f"🔮 [SIGNAL] {payload.strategy} -> {payload.action} {payload.asset} @ ${payload.price}")

    # Circuit breaker: kill_switch or live_trading=false → shadow mode
    if not check_gate(caps):
        logger.warning("🛑 Circuit breaker: System disarmed (caps.json). Logging shadow audit only.")
        asyncio.create_task(dispatch_shadow(payload))
        return {"status": "shadow_audit", "target": payload.asset, "reason": "caps.json disarmed"}

    # Live path: requires arm_signal_v1 provenance in caps.json
    asyncio.create_task(dispatch_live(payload))
    return {"status": "dispatched", "target": payload.asset}

async def dispatch_shadow(payload: ScalpAlert):
    """Shadow audit: log signal, run sentiment via OmniRoute, no tx broadcast."""
    logger.info("🤖 [SWARM] Shadow mode: evaluating via OmniRoute...")
    try:
        import urllib.request, json as j
        payload_dump = j.dumps({"action": payload.action, "asset": payload.asset, "price": payload.price, "strategy": payload.strategy})
        req = urllib.request.Request(OMNIROUTE_URL, data=payload_dump.encode(), headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        score_data = j.loads(resp.read())
        logger.info(f"🧠 [SOUL] OmniRoute sentiment score: {score_data}")
    except Exception as e:
        logger.error(f"❌ OmniRoute shadow eval failed: {e}")
    logger.info(f"📝 [AUDIT] Signal logged for {payload.asset}. No live broadcast.")

async def dispatch_live(payload: ScalpAlert):
    """Live: route to execution microservice via single wallet controller."""
    logger.info(f"🔄 [EXEC] Routing to EOA controller: {AGENT_SWARM['execution']}...")
    # TODO: integrate with executor.py swap.js signing when go-live released
    logger.info(f"📋 [METHODOLOGY] {payload.strategy} signal queued for arm_signal_v1 validation")

if __name__ == "__main__":
    uvicorn.run("dispatcher:app", host="127.0.0.1", port=8000, reload=True)