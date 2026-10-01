#!/usr/bin/env python3
"""
orchestrator.py — WSS signal detector (production).

Watches for EMA crossovers on bluechip tokens and writes signals to signals.json.
Auto-reconnects on WSS drop. Runs forever until killed.
"""
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from base_wss import BaseStream, V2_SWAP_TOPIC
from ema_engine import EmaEngine, discover_pools, BLUECHIP_WATCHLIST


class SignalDetector:
    """Detect EMA crossovers and write signals to file."""
    
    def __init__(self):
        self._running = False
        self._signals_file = os.path.join(HERE, "signals.json")
        self._tick_count = 0
        self._signal_count = 0
        self._start_time = time.time()
        
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
    
    def _shutdown(self, *args):
        print("\n[ORCHESTRATOR] Shutting down...")
        self._running = False
    
    def run(self):
        """Main loop with auto-reconnect."""
        self._running = True
        
        print("=" * 60)
        print("ORCHESTRATOR — Signal Detector")
        print("=" * 60)
        print(f"Time: {datetime.now(timezone.utc).isoformat()}")
        print()
        
        while self._running:
            try:
                self._run_stream()
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[ORCHESTRATOR] Stream error: {e}")
            
            if self._running:
                print("[ORCHESTRATOR] Reconnecting in 5s...")
                time.sleep(5)
        
        self._print_stats()
    
    def _run_stream(self):
        """Run WSS stream and scan for signals."""
        stream = BaseStream()
        head_queue = []
        queue_lock = threading.Lock()
        
        def on_head(header):
            block_num = int(header.get("number", "0x0"), 16)
            with queue_lock:
                head_queue.append(block_num)
        
        stream.subscribe_new_heads(on_head)
        stream.start_background()
        
        print("[ORCHESTRATOR] WSS stream active. Watching for signals...")
        print()
        
        last_block = 0
        
        while self._running:
            with queue_lock:
                blocks = head_queue[:]
                head_queue.clear()
            
            if not blocks:
                time.sleep(1)
                continue
            
            block_num = max(blocks)
            if block_num == last_block:
                continue
            last_block = block_num
            
            # Scan for signals
            signals = self._scan_signals(block_num)
            
            if signals:
                self._write_signals(signals)
                self._signal_count += len(signals)
                print(f"[SIGNAL] Block #{block_num}: {len(signals)} signal(s)")
                for s in signals:
                    print(f"  {s['name']} {s['direction']} @ ${s['price']:.6f}")
            
            self._tick_count += 1
            
            # Print heartbeat every 100 ticks
            if self._tick_count % 100 == 0:
                print(f"[HEARTBEAT] {self._tick_count} blocks, {self._signal_count} signals")
    
    def _scan_signals(self, block_num):
        """Scan all bluechips for EMA crossovers."""
        engine = EmaEngine()
        signals = []
        
        tokens = list(BLUECHIP_WATCHLIST.keys())
        pools = discover_pools(tokens)
        
        for token_addr, pool_addr in pools.items():
            name = BLUECHIP_WATCHLIST.get(token_addr, {}).get("name", token_addr[:12])
            
            try:
                result = engine.check_crossover(pool_addr, require_activity=True)
                
                if result["crossover"] and result["direction"] == "bullish":
                    signals.append({
                        "name": name,
                        "token": token_addr,
                        "pool": pool_addr,
                        "direction": result["direction"],
                        "price": result["price"],
                        "ema9": result["fast_ema"],
                        "ema21": result["slow_ema"],
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "block": block_num,
                    })
            except Exception:
                pass
        
        return signals
    
    def _write_signals(self, signals):
        """Write signals to file."""
        existing = []
        if os.path.exists(self._signals_file):
            try:
                with open(self._signals_file) as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        
        existing.extend(signals)
        existing = existing[-100:]
        
        with open(self._signals_file, "w") as f:
            json.dump(existing, f, indent=2)
    
    def _print_stats(self):
        duration = time.time() - self._start_time
        print()
        print("=" * 60)
        print("ORCHESTRATOR — Final Stats")
        print(f"Uptime: {duration:.0f}s ({duration/3600:.1f}h)")
        print(f"Blocks: {self._tick_count} | Signals: {self._signal_count}")
        print("=" * 60)


def main():
    detector = SignalDetector()
    detector.run()


if __name__ == "__main__":
    main()
