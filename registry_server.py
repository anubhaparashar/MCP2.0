import os
import time
from concurrent import futures

import grpc
import redis
import json

import mcp2_pb2 as pb2
import mcp2_pb2_grpc as pb2_grpc

from auth import verify_jwt_token, has_capability, has_audience
from middleware import TelemetryLogger

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS = redis.Redis.from_url(REDIS_URL)

TLS_CERT_DIR = os.getenv("CERTS_DIR", "certs")
SERVER_CERT_FILE = os.path.join(TLS_CERT_DIR, "server.crt")
SERVER_KEY_FILE = os.path.join(TLS_CERT_DIR, "server.key")
CA_CERT_FILE = os.path.join(TLS_CERT_DIR, "ca.crt")

SERVER_NAME = "RegistryServer"

TELEMETRY = TelemetryLogger()

# ------------------------------------------------------------------
# Helper: Redis Keys & Serialization
# ------------------------------------------------------------------
def _redis_key_for(server_name: str) -> str:
    return f"mcp2:registry:{server_name}"

def register_in_redis(server_name: str, grpc_url: str, capabilities: list):
    key = _redis_key_for(server_name)
    value = {
        "grpc_url": grpc_url,
        "capabilities": capabilities,
        "registered_at": time.time()
    }
    REDIS.set(key, json.dumps(value))

def lookup_in_redis(cap_filters):
    out = []
    for key in REDIS.scan_iter(match="mcp2:registry:*"):
        server_name = key.decode().split(":", 2)[-1]
        data = json.loads(REDIS.get(key))
        caps = data.get("capabilities", [])
        for cap_filter in cap_filters:
            for cap in caps:
                if cap == cap_filter or (cap.endswith("*") and cap_filter.startswith(cap[:-1])):
                    out.append({
                        "server_name": server_name,
                        "grpc_url": data.get("grpc_url"),
                        "capabilities": caps
                    })
                    break
            else:
                continue
            break
    return out

# ------------------------------------------------------------------
# Discovery Servicer
# ------------------------------------------------------------------
class DiscoveryServicer(pb2_grpc.DiscoveryServicer):
    def Register(self, request, context):
        start_time = time.time()
        peer = context.peer()

        md = dict(context.invocation_metadata())
        token = md.get("registration_token")
        if not token:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing registration_token")

        try:
            payload = verify_jwt_token(token, audience=SERVER_NAME)
            if not has_capability(payload, "registry:register"):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "Token lacks registry:register")

            grpc_url = md.get("grpc-url")
            if not grpc_url:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Missing 'grpc-url'")

            register_in_redis(request.server_name, grpc_url, list(request.capabilities))

            TELEMETRY.log({
                "method": "Register",
                "client": payload["sub"],
                "server_name": request.server_name,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "success"
            })
            return pb2.RegisterResponse(success=True, message="Registered successfully")

        except Exception as e:
            TELEMETRY.log({
                "method": "Register",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"failure: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Registration failed: {e}")

    def Lookup(self, request, context):
        start_time = time.time()
        peer = context.peer()

        token = request.requester_token
        if not token:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Missing requester_token")

        try:
            payload = verify_jwt_token(token, audience=SERVER_NAME)
            if not has_capability(payload, "registry:lookup"):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, "Token lacks registry:lookup")

            matches = lookup_in_redis(list(request.capability_filter))
            endpoints = []
            for entry in matches:
                name = entry["server_name"]
                if has_audience(payload, name):
                    endpoints.append(pb2.EndpointDescriptor(
                        server_name=name,
                        grpc_url=entry["grpc_url"],
                        capabilities=entry["capabilities"]
                    ))

            TELEMETRY.log({
                "method": "Lookup",
                "client": payload["sub"],
                "found": len(endpoints),
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": "success"
            })
            return pb2.LookupResponse(endpoints=endpoints)

        except Exception as e:
            TELEMETRY.log({
                "method": "Lookup",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"failure: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Lookup failed: {e}")

# ------------------------------------------------------------------
# BOOTSTRAP SERVER (mTLS)
# ------------------------------------------------------------------
def serve_registry():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_DiscoveryServicer_to_server(DiscoveryServicer(), server)

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
    server.add_secure_port("[::]:50050", server_credentials)
    print(f"[Registry] (mTLS) Listening on 50050")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve_registry()
