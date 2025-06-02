import os
import time
import threading
from concurrent import futures

import grpc
import redis
import json
from sqlalchemy import create_engine, Column, String, LargeBinary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import mcp2_pb2 as pb2
import mcp2_pb2_grpc as pb2_grpc

from auth import (
    verify_jwt_token, has_capability, has_audience, verify_delegation_proof
)
from middleware import TelemetryLogger, SimpleCache, CircuitBreaker

# ------------------------------------------------------------------
# CONFIGURATION (via ENV)
# ------------------------------------------------------------------
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql+psycopg2://user:pass@localhost:5432/mcp2db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/1")

TLS_CERT_DIR = os.getenv("CERTS_DIR", "certs")
SERVER_CERT_FILE = os.path.join(TLS_CERT_DIR, "server.crt")
SERVER_KEY_FILE = os.path.join(TLS_CERT_DIR, "server.key")
CA_CERT_FILE = os.path.join(TLS_CERT_DIR, "ca.crt")

SERVER_NAME = "ContextToolServer"

# ------------------------------------------------------------------
# SET UP SQLAlchemy (PostgreSQL) for context storage
# ------------------------------------------------------------------
Base = declarative_base()

class ContextEntry(Base):
    __tablename__ = "context_entries"
    context_key = Column(String, primary_key=True)
    serialized_value = Column(LargeBinary, nullable=False)
    metadata_json = Column(String)  # e.g., JSON-serialized metadata (timestamps, etc.)

# Create engine & session factory
engine = create_engine(POSTGRES_URL, echo=False, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine)

# Ensure table exists
Base.metadata.create_all(bind=engine)

# ------------------------------------------------------------------
# Redis for telemetry pub/sub
# ------------------------------------------------------------------
redis_client = redis.Redis.from_url(REDIS_URL)
TELEMETRY_CHANNEL_PREFIX = "mcp2:telemetry:"  # channel per stream_id

# ------------------------------------------------------------------
# In-Memory Telemetry Cache (to store historical or latest value, if needed)
# ------------------------------------------------------------------
CACHE = SimpleCache()
CIRCUIT = CircuitBreaker(threshold=3, recovery_time=30)
TELEMETRY = TelemetryLogger()

# ------------------------------------------------------------------
# Helper: Publish telemetry to Redis channel
# ------------------------------------------------------------------
def publish_telemetry_to_redis(stream_id: str, payload: bytes):
    channel = TELEMETRY_CHANNEL_PREFIX + stream_id
    redis_client.publish(channel, payload)

# ------------------------------------------------------------------
# BACKGROUND THREAD: Simulate telemetry generation
# ------------------------------------------------------------------
def telemetry_pusher():
    while True:
        time.sleep(5)
        payload = b'{"engine_temp": %d}' % (65 + int(time.time()) % 10)
        publish_telemetry_to_redis("fleet123:engine_temp", payload)

# ------------------------------------------------------------------
# ContextToolService Implementation
# ------------------------------------------------------------------
class ContextToolServicer(pb2_grpc.ContextToolServicer):
    def RequestContext(self, request, context):
        """
        Retrieves a context_entry from PostgreSQL by 'context_key'.  
        Checks capability_token (OIDC), audience, and circuit breaker.
        Caches responses in-memory for 60s.
        """
        start_time = time.time()
        peer = context.peer()

        # 1. Validate token from request.capability_token
        try:
            payload = verify_jwt_token(request.capability_token, audience=SERVER_NAME)
            if not has_capability(payload, "db:inventory:read"):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "Token lacks db:inventory:read")
            if not has_audience(payload, SERVER_NAME):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")
        except Exception as e:
            TELEMETRY.log({
                "method": "RequestContext",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"unauthenticated: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Auth failed: {e}")

        # 2. Circuit breaker
        if not CIRCUIT.before_call():
            TELEMETRY.log({
                "method": "RequestContext",
                "client": payload["sub"],
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "circuit_open"
            })
            context.abort(grpc.StatusCode.UNAVAILABLE, "Service temporarily unavailable")

        # 3. Check cache
        cache_key = f"context::{request.context_key}::{tuple(sorted(request.parameters.items()))}"
        cached_resp = CACHE.get(cache_key)
        if cached_resp is not None:
            TELEMETRY.log({
                "method": "RequestContext",
                "client": payload["sub"],
                "cache_hit": True,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "success"
            })
            return cached_resp

        # 4. Fetch from PostgreSQL
        session = SessionLocal()
        try:
            entry = session.get(ContextEntry, request.context_key)
            if entry is None:
                # If no entry, return empty payload
                serialized = b""
                metadata_list = []
            else:
                serialized = entry.serialized_value
                try:
                    metadata_list = json.loads(entry.metadata_json)
                except:
                    metadata_list = []
            resp = pb2.ContextResponse(
                serialized_value=serialized,
                metadata=metadata_list
            )
            # 5. Cache it
            CACHE.set(cache_key, resp, ttl=60)

            CIRCUIT.after_call(success=True)
            TELEMETRY.log({
                "method": "RequestContext",
                "client": payload["sub"],
                "cache_hit": False,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "success"
            })
            return resp

        except Exception as e:
            CIRCUIT.after_call(success=False)
            TELEMETRY.log({
                "method": "RequestContext",
                "client": payload["sub"],
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"db_error: {e}"
            })
            context.abort(grpc.StatusCode.INTERNAL, f"DB failure: {e}")
        finally:
            session.close()

    def SubscribeTelemetry(self, request, context):
        """
        Subscribes to telemetry via Redis Pub/Sub.  
        Checks token, audience, and then streams any published messages on that channel.
        """
        start_time = time.time()
        peer = context.peer()

        # 1. Validate token
        try:
            payload = verify_jwt_token(request.capability_token, audience=SERVER_NAME)
            if not has_capability(payload, "telemetry:read"):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "Token lacks telemetry:read")
            if not has_audience(payload, SERVER_NAME):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")
        except Exception as e:
            TELEMETRY.log({
                "method": "SubscribeTelemetry",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"unauthenticated: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Auth failed: {e}")

        channel_name = TELEMETRY_CHANNEL_PREFIX + request.stream_id
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(channel_name)

        TELEMETRY.log({
            "method": "SubscribeTelemetry.start",
            "client": payload["sub"],
            "stream_id": request.stream_id,
            "latency_ms": int((time.time() - start_time) * 1000),
            "status": "subscribed"
        })

        try:
            for message in pubsub.listen():
                if not context.is_active():
                    break
                data = message.get("data")
                if isinstance(data, bytes):
                    frame = pb2.TelemetryFrame(
                        timestamp_ms=int(time.time() * 1000),
                        payload=data
                    )
                    try:
                        context.write(frame)
                    except grpc.RpcError:
                        break
        except Exception as e:
            TELEMETRY.log({
                "method": "SubscribeTelemetry",
                "client": payload["sub"],
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"failure: {e}"
            })
            context.abort(grpc.StatusCode.INTERNAL, f"Subscribe error: {e}")
        finally:
            pubsub.unsubscribe(channel_name)
            pubsub.close()

    def MultiModalExchange(self, request_iterator, context):
        """
        Bidirectional streaming: expects the first frame’s metadata to include 'capability_token'.  
        Validates "tool:multimodal_exchange" and then echoes frames.  
        """
        start_time = time.time()
        peer = context.peer()
        token_payload = None
        first_frame = True

        try:
            for mm_frame in request_iterator:
                if first_frame:
                    md = dict(context.invocation_metadata())
                    token = md.get("capability_token")
                    if not token:
                        context.abort(grpc.StatusCode.PERMISSION_DENIED, "Missing capability_token")
                    token_payload = verify_jwt_token(token, audience=SERVER_NAME)
                    if not has_capability(token_payload, "tool:multimodal_exchange"):
                        context.abort(grpc.StatusCode.PERMISSION_DENIED, "Token lacks tool:multimodal_exchange")
                    if not has_audience(token_payload, SERVER_NAME):
                        context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")
                    first_frame = False

                yield mm_frame

            TELEMETRY.log({
                "method": "MultiModalExchange",
                "client": token_payload["sub"],
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "completed"
            })
        except Exception as e:
            TELEMETRY.log({
                "method": "MultiModalExchange",
                "client": token_payload.get("sub", peer) if token_payload else peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"failure: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Error: {e}")

    def InvokeTool(self, request, context):
        """
        Invokes a named tool.  
        Validates token (and optional delegation proof) and then executes a dummy 'compute_pricing' tool.  
        """
        start_time = time.time()
        peer = context.peer()

        try:
            payload = verify_jwt_token(request.capability_token, audience=SERVER_NAME)
            required_cap = f"tool:{request.tool_name}"
            if not has_capability(payload, required_cap):
                del_proof = request.agent_delegation_proof
                if not del_proof:
                    context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token lacks {required_cap}")
                delegated_payload = verify_delegation_proof(del_proof, delegatee=SERVER_NAME, original_token_payload=payload)
                if required_cap not in delegated_payload.get("capabilities", []):
                    context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Delegation proof lacks {required_cap}")
                payload = delegated_payload

            if not has_audience(payload, SERVER_NAME):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")

        except Exception as e:
            TELEMETRY.log({
                "method": "InvokeTool",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"unauthenticated: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Auth failed: {e}")

        if not CIRCUIT.before_call():
            TELEMETRY.log({
                "method": "InvokeTool",
                "client": payload["sub"],
                "tool": request.tool_name,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "circuit_open"
            })
            context.abort(grpc.StatusCode.UNAVAILABLE, "Service temporarily unavailable")

        outputs = {}
        warnings = []

        if request.tool_name == "compute_pricing":
            sku = request.arguments.get("sku", "")
            stock = int(request.arguments.get("stock_count", "0"))
            recommended_price = max(0.0, 100.0 - 0.1 * stock)
            outputs["recommended_price"] = str(recommended_price).encode("utf-8")
        else:
            warnings.append(f"Tool '{request.tool_name}' not recognized")

        CIRCUIT.after_call(success=True)
        TELEMETRY.log({
            "method": "InvokeTool",
            "client": payload["sub"],
            "tool": request.tool_name,
            "latency_ms": int((time.time() - start_time) * 1000),
            "status": "success"
        })
        return pb2.ToolResponse(success=True, outputs=outputs, warnings=warnings)

# ------------------------------------------------------------------
# Server Bootstrap (mTLS)
# ------------------------------------------------------------------
def serve_context_tool():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_ContextToolServicer_to_server(ContextToolServicer(), server)

    with open(SERVER_CERT_FILE, "rb") as f:
        server_cert = f.read()
    with open(SERVER_KEY_FILE, "rb") as f:
        server_key = f.read()
    with open(CA_CERT_FILE, "rb") as f:
        ca_cert = f.read()

    server_credentials = grpc.ssl_server_credentials(
        [(server_key, server_cert)],
        root_certificates=ca_cert,
        require_client_auth=True
    )
    server.add_secure_port("[::]:50051", server_credentials)
    print(f"[ContextTool] (mTLS) Listening on 50051")

    t = threading.Thread(target=telemetry_pusher, daemon=True)
    t.start()

    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve_context_tool()
