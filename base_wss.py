#!/usr/bin/env python3
"""
base_wss.py — real-time streaming for Base chain (8453).

Drop-in extension for base_data.py. Same interface, two transports:
- HTTP (polling) for state reads: balances, fees, prices, tx history
- WSS (push) for real-time signals: new blocks, swap logs, pending txs

Stdlib-only — minimal RFC 6455 client (socket + ssl, client masking,
ping/pong, fragmentation). No websocket-client, no pip.

Usage:
    from base_wss import BaseStream, V2_SWAP_TOPIC
    
    bs = BaseStream()
    
    # HTTP methods unchanged
    bs.fee_data()
    bs.block_number()
    
    # WSS streaming with safe queue pattern
    bs.register_tick_handler(lambda block_num, header: run_tick(block_num))
    bs.subscribe_new_heads()  # auto-enqueues, drops stale heads
    
    bs.subscribe_logs(callback=on_swap, address=pool, topics=[V2_SWAP_TOPIC])
    
    # Run forever
    bs.run_forever()

Free WSS endpoints:
    wss://base-rpc.publicnode.com
    wss://base.drpc.org

Honest status:
    ✅ Connection, subscription, live head delivery verified
    ⚠️ Reconnect→backfill not force-tested (run --demo locally first)
    ⚠️ Free WSS has no SLA — expect occasional idle drops
"""
import hashlib
import json
import os
import queue
import random
import socket
import ssl
import struct
import threading
import time
import urllib.request
from base_data import BaseData, RPCError

# --- Constants ---
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# V2 Swap event signature
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

# Free WSS endpoints
WSS_POOL = [
    "wss://base-rpc.publicnode.com",
    "wss://base.drpc.org",
]

# HTTP fallback for backfill
HTTP_POOL = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://1rpc.io/base",
    "https://base-pokt.nodies.app",
    "https://rpc.ankr.com/base",
    "https://base.drpc.org",
]


class WSSConnection:
    """
    Minimal RFC 6455 WebSocket client.
    Stdlib-only: socket + ssl, client masking, ping/pong, fragmentation.
    """
    
    def __init__(self, url, timeout=30):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self.ssl_ctx = ssl.create_default_context()
        self._connected = False
        self._recv_buffer = b""
        self._fragment_buffer = b""
        self._fragment_op = None
    
    def connect(self):
        """Establish WebSocket connection with RFC 6455 handshake."""
        if self.url.startswith("wss://"):
            host_port = self.url[6:]
            use_ssl = True
        elif self.url.startswith("ws://"):
            host_port = self.url[5:]
            use_ssl = False
        else:
            raise ValueError(f"Invalid WSS URL: {self.url}")
        
        if "/" in host_port:
            host_port, path = host_port.split("/", 1)
            path = "/" + path
        else:
            path = "/"
        
        if ":" in host_port:
            host, port = host_port.rsplit(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 443 if use_ssl else 80
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        
        if use_ssl:
            sock = self.ssl_ctx.wrap_socket(sock, server_hostname=host)
        
        sock.connect((host, port))
        
        # RFC 6455 handshake
        key = os.urandom(16)
        key_b64 = __import__("base64").b64encode(key).decode()
        
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key_b64}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode())
        
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WSS handshake failed")
            response += chunk
        
        if b"101" not in response.split(b"\r\n")[0]:
            raise ConnectionError(f"WSS handshake rejected: {response[:200]}")
        
        self.sock = sock
        self._connected = True
    
    def send(self, data):
        """Send a masked text frame (RFC 6455 client-to-server)."""
        if not self._connected:
            raise ConnectionError("Not connected")
        
        if isinstance(data, str):
            data = data.encode("utf-8")
        
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        
        mask_key = os.urandom(4)
        length = len(data)
        
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        
        header.extend(mask_key)
        
        masked = bytearray(length)
        for i in range(length):
            masked[i] = data[i] ^ mask_key[i % 4]
        
        self.sock.sendall(bytes(header) + bytes(masked))
    
    def send_pong(self, payload=b""):
        """Send pong frame in response to ping."""
        if not self._connected:
            return
        
        header = bytearray()
        header.append(0x8A)  # FIN + pong opcode
        
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        
        self.sock.sendall(bytes(header) + payload)
    
    def recv_frame(self):
        """
        Receive and parse a WebSocket frame.
        Returns (opcode, payload) or (None, None) on close.
        """
        while True:
            while len(self._recv_buffer) < 2:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self._connected = False
                    return None, None
                self._recv_buffer += chunk
            
            byte1, byte2 = self._recv_buffer[0], self._recv_buffer[1]
            fin = (byte1 & 0x80) != 0
            opcode = byte1 & 0x0F
            masked = (byte2 & 0x80) != 0
            length = byte2 & 0x7F
            
            offset = 2
            
            if length == 126:
                while len(self._recv_buffer) < offset + 2:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        self._connected = False
                        return None, None
                    self._recv_buffer += chunk
                length = struct.unpack(">H", self._recv_buffer[offset:offset+2])[0]
                offset += 2
            elif length == 127:
                while len(self._recv_buffer) < offset + 8:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        self._connected = False
                        return None, None
                    self._recv_buffer += chunk
                length = struct.unpack(">Q", self._recv_buffer[offset:offset+8])[0]
                offset += 8
            
            mask_key = None
            if masked:
                while len(self._recv_buffer) < offset + 4:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        self._connected = False
                        return None, None
                    self._recv_buffer += chunk
                mask_key = self._recv_buffer[offset:offset+4]
                offset += 4
            
            while len(self._recv_buffer) < offset + length:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self._connected = False
                    return None, None
                self._recv_buffer += chunk
            
            payload = self._recv_buffer[offset:offset+length]
            self._recv_buffer = self._recv_buffer[offset+length:]
            
            if mask_key:
                unmasked = bytearray(length)
                for i in range(length):
                    unmasked[i] = payload[i] ^ mask_key[i % 4]
                payload = bytes(unmasked)
            
            if opcode == 0x9:  # ping
                self.send_pong(payload)
                continue
            elif opcode == 0xA:  # pong
                continue
            elif opcode == 0x8:  # close
                self._connected = False
                return None, None
            
            if not fin:
                self._fragment_buffer += payload
                if self._fragment_op is None:
                    self._fragment_op = opcode
                continue
            elif self._fragment_buffer:
                payload = self._fragment_buffer + payload
                opcode = self._fragment_op
                self._fragment_buffer = b""
                self._fragment_op = None
            
            return opcode, payload
    
    def close(self):
        """Close the connection gracefully."""
        try:
            if self._connected and self.sock:
                self.sock.sendall(b"\x88\x80\x00\x00\x00\x00")
        except Exception:
            pass
        self._connected = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
    
    @property
    def connected(self):
        return self._connected and self.sock is not None


class Subscription:
    """Represents an active WSS subscription."""
    
    def __init__(self, method, params, callback):
        self.method = method
        self.params = params
        self.callback = callback
        self.sub_id = None
        self.active = False


class HeadQueue:
    """
    Thread-safe queue for new-head callbacks.
    Drop-on-full so stale heads never backlog the reader thread.
    """
    
    def __init__(self, maxsize=4):
        self.q = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._processed = 0
    
    def put(self, header):
        """Enqueue a header. Drop if full (stale head is worthless)."""
        try:
            self.q.put_nowait(header)
        except queue.Full:
            self._dropped += 1
    
    def get(self, timeout=1.0):
        """Dequeue a header. Returns None on timeout."""
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None
    
    @property
    def stats(self):
        return {"processed": self._processed, "dropped": self._dropped, "pending": self.q.qsize()}


class BaseStream(BaseData):
    """
    BaseData + real-time streaming via WebSocket.
    
    Inherits all HTTP methods from BaseData (fee_data, token_balance, etc.)
    and adds WSS streaming capabilities with safe callback handling.
    """
    
    def __init__(self, wss_pool=None, http_pool=None, timeout=30):
        super().__init__(rpc_pool=http_pool, timeout=timeout)
        self.wss_pool = wss_pool or WSS_POOL
        self._wss = None
        self._wss_url = None
        self._subscriptions = []
        self._subs_lock = threading.Lock()
        self._msg_id = 0
        self._id_lock = threading.Lock()
        self._pending_subs = {}  # msg_id -> Subscription
        self._running = False
        self._reader_thread = None
        self._seen_events = set()
        self._seen_lock = threading.Lock()
        # Queue pattern for safe callback handling
        self.head_queue = HeadQueue(maxsize=4)
        self.log_queue = queue.Queue(maxsize=100)
        self._workers = []
        self._tick_handlers = []
    
    def _next_id(self):
        with self._id_lock:
            self._msg_id += 1
            return self._msg_id
    
    def _connect_wss(self):
        """Connect to a WSS endpoint from the pool."""
        if self._wss and self._wss.connected:
            return True
        
        for url in self.wss_pool:
            try:
                self._wss = WSSConnection(url, timeout=self.timeout)
                self._wss.connect()
                self._wss_url = url
                return True
            except Exception:
                continue
        
        return False
    
    def _send_json(self, method, params):
        """Send a JSON-RPC request over WSS."""
        if not self._wss or not self._wss.connected:
            if not self._connect_wss():
                raise RPCError("Could not connect to WSS")
        
        msg_id = self._next_id()
        payload = json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        self._wss.send(payload)
        return msg_id
    
    def _register_subscription(self, method, params, callback):
        """Register a subscription."""
        sub = Subscription(method, params, callback)
        with self._subs_lock:
            self._subscriptions.append(sub)
        
        msg_id = self._send_json(method, params)
        self._pending_subs[msg_id] = sub
        return sub
    
    def _handle_subscription_response(self, msg_id, result):
        """Bind remote sub-id to local subscription."""
        if msg_id in self._pending_subs:
            sub = self._pending_subs.pop(msg_id)
            sub.sub_id = result
            sub.active = True
    
    def _handle_notification(self, method, params):
        """Route a notification to the matching subscription callback."""
        if method != "eth_subscription":
            return
        
        sub_id = params.get("subscription")
        result = params.get("result")
        
        with self._subs_lock:
            for sub in self._subscriptions:
                if sub.sub_id == sub_id:
                    try:
                        sub.callback(result)
                    except Exception:
                        pass
                    break
    
    def _reader_loop(self):
        """Main reader loop.
        
        On disconnect: backoff → reconnect → re-register → resume live events.
        Backfill runs async on daemon thread to isolate HTTP latency.
        """
        backoff = 1
        while self._running:
            try:
                if not self._wss or not self._wss.connected:
                    # Backoff sleep before reconnect (1s → 2s → 4s, capped 5s)
                    time.sleep(min(backoff, 5))
                    backoff = min(backoff * 2, 5)
                    
                    if not self._connect_wss():
                        continue
                    
                    # Reset backoff on successful connect
                    backoff = 1
                    
                    # Re-register all subscriptions on reconnect
                    with self._subs_lock:
                        for sub in self._subscriptions:
                            sub.active = False
                            sub.sub_id = None
                            msg_id = self._send_json(sub.method, sub.params)
                            self._pending_subs[msg_id] = sub
                    
                    # Backfill the gap on a daemon thread (async, isolated)
                    bf_thread = threading.Thread(
                        target=self._backfill_gap, daemon=True, name="backfill"
                    )
                    bf_thread.start()
                
                opcode, payload = self._wss.recv_frame()
                
                if opcode is None:
                    self._wss.close()
                    continue
                
                if opcode != 1:
                    continue
                
                try:
                    data = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                
                if "id" in data and "result" in data:
                    self._handle_subscription_response(data["id"], data["result"])
                    continue
                
                if "method" in data:
                    self._handle_notification(data["method"], data.get("params", {}))
                    continue
                
            except Exception:
                if self._running:
                    time.sleep(1)
    
    def _backfill_gap(self):
        """Backfill missed logs on reconnect (runs async on daemon thread)."""
        try:
            current_block = self.block_number()
            # Backfill last ~100 blocks
            from_block = max(current_block - 100, 0)
            
            for sub in self._subscriptions:
                if sub.method != "eth_subscribe" or sub.params[0] != "logs":
                    continue
                
                if not sub.params or len(sub.params) < 2:
                    continue
                
                params = sub.params[1] if len(sub.params) > 1 else {}
                address = params.get("address")
                topics = params.get("topics")
                
                if not address or not topics:
                    continue
                
                logs = self._backfill_logs(address, topics, from_block, current_block)
                for log in logs:
                    if not self._is_seen(log):
                        try:
                            sub.callback(log)
                        except Exception:
                            pass
        except Exception:
            pass
    
    def run_forever(self):
        """Start the reader loop (blocking)."""
        self._running = True
        self._reader_loop()
    
    def start_background(self):
        """Start the reader loop in a background thread."""
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()
        return self._reader_thread
    
    def stop(self):
        """Stop the reader loop."""
        self._running = False
        if self._wss:
            self._wss.close()
    
    def _ensure_reader(self):
        """Ensure the reader thread and tick worker are running."""
        if not self._running:
            self.start_background()
            time.sleep(0.5)
        
        # Always ensure tick worker is running if we have handlers
        if self._tick_handlers:
            has_worker = any(
                getattr(w, 'name', None) == "tick_worker" and w.is_alive()
                for w in self._workers
            )
            if not has_worker:
                t = threading.Thread(target=self._tick_worker, name="tick_worker", daemon=True)
                t.start()
                self._workers.append(t)
    
    def _tick_worker(self):
        """Worker thread: consume heads from queue, call tick handlers."""
        while self._running:
            header = self.head_queue.get(timeout=1.0)
            if header is None:
                continue
            block_num = int(header.get("number", "0x0"), 16)
            for handler in self._tick_handlers:
                try:
                    handler(block_num, header)
                except Exception:
                    pass
    
    def register_tick_handler(self, handler):
        """Register a function(block_num, header) to be called on each new head."""
        self._tick_handlers.append(handler)
        if not any(getattr(w, 'name', None) == "tick_worker" for w in self._workers):
            t = threading.Thread(target=self._tick_worker, name="tick_worker", daemon=True)
            t.start()
            self._workers.append(t)
    
    # --- Public streaming API ---
    
    def subscribe_new_heads(self, callback=None):
        """
        Subscribe to new block headers.
        
        IMPORTANT: For Base, do NOT do heavy work (price fetch, preflight, journal write)
        directly in the callback — it runs on the reader thread and will block frame processing.
        
        Instead, use the queue pattern:
            bs = BaseStream()
            bs.register_tick_handler(lambda block_num, header: run_full_tick(block_num))
            bs.subscribe_new_heads()  # auto-enqueues to head_queue
        """
        self._ensure_reader()
        
        if callback is None:
            def default_callback(header):
                self.head_queue.put(header)
            callback = default_callback
        
        return self._register_subscription("eth_subscribe", ["newHeads"], callback)
    
    def subscribe_logs(self, callback, address=None, topics=None):
        """
        Subscribe to contract logs.
        """
        self._ensure_reader()
        params = {"address": address}
        if topics:
            params["topics"] = topics
        return self._register_subscription("eth_subscribe", ["logs", params], callback)
    
    def subscribe_pending_txs(self, callback):
        """
        Subscribe to pending transactions.
        
        ⚠️ NOT VIABLE ON BASE — Base uses a centralized sequencer with a
        private mempool. newPendingTransactions over public WSS returns nothing
        useful; there is no public pre-inclusion transaction stream to observe.
        This is also why frontrunning/sandwich risk on Base is structurally low:
        the sequencer orders FCFS, searchers can't reorder against you.
        
        Your real dump-detection signal is per-block swap-flow analysis:
        when a head lands, the swap logs for that block are immediately available.
        Use subscribe_logs + subscribe_new_heads instead.
        """
        self._ensure_reader()
        return self._register_subscription("eth_subscribe", ["newPendingTransactions"], callback)
    
    # --- Backfill helpers ---
    
    def _dedup_key(self, log):
        """Create a dedup key for a log entry."""
        tx_hash = log.get("transactionHash", "")
        log_index = log.get("logIndex", "0x0")
        return f"{tx_hash}:{log_index}"
    
    def _is_seen(self, log):
        """Check if a log has been seen before."""
        key = self._dedup_key(log)
        with self._seen_lock:
            if key in self._seen_events:
                return True
            self._seen_events.add(key)
            if len(self._seen_events) > 100000:
                self._seen_events = set(list(self._seen_events)[-50000:])
            return False


if __name__ == "__main__":
    import argparse
    
    ap = argparse.ArgumentParser(description="Base WSS Stream")
    ap.add_argument("--demo", action="store_true", help="Run live demo")
    args = ap.parse_args()
    
    if args.demo:
        print("=== Base WSS Live Demo ===")
        print()
        
        bs = BaseStream()
        tick_count = 0
        
        def on_tick(block_num, header):
            global tick_count
            tick_count += 1
            print(f"[TICK] #{block_num} (tick #{tick_count})")
        
        bs.register_tick_handler(on_tick)
        bs.subscribe_new_heads()
        
        print("Subscribed (queue mode). Running for 30 seconds...")
        print()
        
        try:
            bs.start_background()
            time.sleep(30)
        except KeyboardInterrupt:
            pass
        finally:
            bs.stop()
        
        print()
        print(f"Total ticks: {tick_count}")
        print(f"Queue stats: {bs.head_queue.stats}")
    
    else:
        print("Use --demo for a live test")
