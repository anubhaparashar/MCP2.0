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
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/2")
redis_client = redis.Redis.from_url(REDIS_URL)

TLS_CERT_DIR = os.getenv("CERTS_DIR", "certs")
SERVER_CERT_FILE = os.path.join(TLS_CERT_DIR, "server.crt")
SERVER_KEY_FILE = os.path.join(TLS_CERT_DIR, "server.key")
CA_CERT_FILE = os.path.join(TLS_CERT_DIR, "ca.crt")

SERVER_NAME = "EventBusServer"
TELEMETRY = TelemetryLogger()

# Tracks sequence IDs per topic (in-memory; consider Redis hash for persistence)
TOPIC_COUNTER = {}

# ------------------------------------------------------------------
# EventBus Service
# ------------------------------------------------------------------
class EventBusServicer(pb2_grpc.EventBusServicer):
    def Publish(self, request, context):
        start_time = time.time()
        peer = context.peer()

        try:
            payload = verify_jwt_token(request.publisher_token, audience=SERVER_NAME)
            required_cap = f"event:publish:{request.topic}"
            if not has_capability(payload, required_cap):
                prefix = request.topic.split(":")[0]
                wildcard = f"event:publish:{prefix}*"
                if not has_capability(payload, wildcard):
                    context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token lacks event:publish:{request.topic}")
            if not has_audience(payload, SERVER_NAME):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")

        except Exception as e:
            TELEMETRY.log({
                "method": "Publish",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"unauthenticated: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Auth failed: {e}")

        seq = TOPIC_COUNTER.get(request.topic, 0) + 1
        TOPIC_COUNTER[request.topic] = seq

        channel = f"mcp2:event:{request.topic}"
        message = json.dumps({
            "topic": request.topic,
            "payload": request.payload.decode("utf-8", errors="ignore"),
            "sequence_id": seq,
            "timestamp": time.time()
        }).encode("utf-8")
        redis_client.publish(channel, message)

        TELEMETRY.log({
            "method": "Publish",
            "client": payload["sub"],
            "topic": request.topic,
            "latency_ms": int((time.time() - start_time) * 1000),
            "status": "success"
        })
        return pb2.EventPublishResponse(success=True, message="Published")

    def Subscribe(self, request, context):
        start_time = time.time()
        peer = context.peer()

        try:
            payload = verify_jwt_token(request.subscriber_token, audience=SERVER_NAME)
            required_cap = f"event:subscribe:{request.topic_filter}"
            if not has_capability(payload, required_cap):
                prefix = request.topic_filter.split(":")[0]
                wildcard = f"event:subscribe:{prefix}*"
                if not has_capability(payload, wildcard):
                    context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token lacks event:subscribe:{request.topic_filter}")
            if not has_audience(payload, SERVER_NAME):
                context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Token not for {SERVER_NAME}")
        except Exception as e:
            TELEMETRY.log({
                "method": "Subscribe",
                "client": peer,
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"unauthenticated: {e}"
            })
            context.abort(grpc.StatusCode.UNAUTHENTICATED, f"Auth failed: {e}")

        if request.topic_filter.endswith("*"):
            prefix = request.topic_filter[:-1]
            pattern = f"mcp2:event:{prefix}*"
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.psubscribe(pattern)
        else:
            channel = f"mcp2:event:{request.topic_filter}"
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(channel)

        TELEMETRY.log({
            "method": "Subscribe.start",
            "client": payload["sub"],
            "topic_filter": request.topic_filter,
            "latency_ms": int((time.time() - start_time) * 1000),
            "status": "subscribed"
        })

        try:
            for message in pubsub.listen():
                if not context.is_active():
                    break
                data = message.get("data")
                if isinstance(data, bytes):
                    try:
                        obj = json.loads(data)
                        envelope = pb2.EventEnvelope(
                            topic=obj.get("topic", ""),
                            payload=obj.get("payload", "").encode("utf-8"),
                            sequence_id=int(obj.get("sequence_id", 0))
                        )
                        context.write(envelope)
                    except Exception:
                        continue
        except Exception as e:
            TELEMETRY.log({
                "method": "Subscribe",
                "client": payload["sub"],
                "latency_ms": int((time.time() - start_time) * 1000),
                "status": f"failure: {e}"
            })
            context.abort(grpc.StatusCode.INTERNAL, f"Subscribe error: {e}")
        finally:
            pubsub.close()

# ------------------------------------------------------------------
# Server Bootstrap (mTLS)
# ------------------------------------------------------------------
def serve_event_bus():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pb2_grpc.add_EventBusServicer_to_server(EventBusServicer(), server)

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
    server.add_secure_port("[::]:50052", server_credentials)
    print(f"[EventBus] (mTLS) Listening on 50052")
    server.start()
    server.wait_for_termination()

if __name__ == "__main__":
    serve_event_bus()
