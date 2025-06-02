import os
import threading
import time
import grpc
import json
from sqlalchemy import create_engine, Column, String, LargeBinary
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import mcp2_pb2 as pb2
import mcp2_pb2_grpc as pb2_grpc

from auth import (
    create_capability_token,
    verify_jwt_token,
    has_capability,
    has_audience
)

# ------------------------------------------------------------------
# CONFIGURATION (MATCH SERVER CERTS)
# ------------------------------------------------------------------
TLS_CERT_DIR = os.getenv("CERTS_DIR", "certs")
CLIENT_CERT_FILE = os.path.join(TLS_CERT_DIR, "client.crt")
CLIENT_KEY_FILE = os.path.join(TLS_CERT_DIR, "client.key")
CA_CERT_FILE = os.path.join(TLS_CERT_DIR, "ca.crt")

REGISTRY_ADDR = "localhost:50050"
CONTEXTOOL_ADDR = "localhost:50051"
EVENTBUS_ADDR = "localhost:50052"

# ------------------------------------------------------------------
# SQLAlchemy Model (duplicate of server) for demo insertion
# ------------------------------------------------------------------
Base = declarative_base()

class ContextEntry(Base):
    __tablename__ = "context_entries"
    context_key = Column(String, primary_key=True)
    serialized_value = Column(LargeBinary, nullable=False)
    metadata_json = Column(String)

POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql+psycopg2://user:pass@localhost:5432/mcp2db")
engine = create_engine(POSTGRES_URL)
SessionLocal = sessionmaker(bind=engine)

# ------------------------------------------------------------------
# Helper: Load mTLS credentials
# ------------------------------------------------------------------
def load_mtls_credentials():
    with open(CA_CERT_FILE, "rb") as f:
        trusted_certs = f.read()
    with open(CLIENT_KEY_FILE, "rb") as f:
        client_key = f.read()
    with open(CLIENT_CERT_FILE, "rb") as f:
        client_cert = f.read()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=trusted_certs,
        private_key=client_key,
        certificate_chain=client_cert
    )
    return credentials

# ------------------------------------------------------------------
# 1. Prepare a ContextEntry in PostgreSQL
# ------------------------------------------------------------------
def insert_demo_context():
    session = SessionLocal()
    entry = session.get(ContextEntry, "inventory:prod_12345:stock_count")
    if entry is None:
        entry = ContextEntry(
            context_key="inventory:prod_12345:stock_count",
            serialized_value=b"42",
            metadata_json=json.dumps(["timestamp:2025-06-01T12:00:00Z"])
        )
        session.add(entry)
        session.commit()
    session.close()

# ------------------------------------------------------------------
# 2. Create OIDC tokens (replace 'your-audience' with correct values)
# ------------------------------------------------------------------
def make_tokens():
    return {
        "registry_register": "<RS256_JWT with 'registry:register', aud='RegistryServer'>",
        "registry_lookup": "<RS256_JWT with 'registry:lookup', aud='RegistryServer'>",
        "context": "<RS256_JWT with 'db:inventory:read','telemetry:read','tool:compute_pricing','tool:multimodal_exchange', aud='ContextToolServer'>",
        "event": "<RS256_JWT with 'event:subscribe:inventory:*','event:publish:inventory:*', aud='EventBusServer'>"
    }

# ------------------------------------------------------------------
# 3. Register InventoryDB in Registry
# ------------------------------------------------------------------
def register_inventorydb(creds, tokens):
    channel = grpc.secure_channel(REGISTRY_ADDR, creds)
    stub = pb2_grpc.DiscoveryStub(channel)

    metadata = [
        ("grpc-url", CONTEXTOOL_ADDR),
        ("registration_token", tokens["registry_register"])
    ]
    req = pb2.RegisterRequest(
        server_name="InventoryDB_Primary",
        capabilities=["db:inventory:read", "telemetry:read", "tool:compute_pricing", "tool:multimodal_exchange"]
    )
    resp = stub.Register(req, metadata=metadata, timeout=5)
    print(f"[Client] Register: success={resp.success}, message='{resp.message}'")

# ------------------------------------------------------------------
# 4. Lookup InventoryDB Endpoint
# ------------------------------------------------------------------
def lookup_inventorydb(creds, tokens):
    channel = grpc.secure_channel(REGISTRY_ADDR, creds)
    stub = pb2_grpc.DiscoveryStub(channel)

    req = pb2.LookupRequest(
        requester_token=tokens["registry_lookup"],
        capability_filter=["db:inventory:read"]
    )
    resp = stub.Lookup(req, timeout=5)
    print("[Client] Lookup Results:")
    for ep in resp.endpoints:
        print(f"  * {ep.server_name} @ {ep.grpc_url} (caps={ep.capabilities})")
    return resp.endpoints

# ------------------------------------------------------------------
# 5. Fetch Stock Count from ContextTool
# ------------------------------------------------------------------
def fetch_stock_count(creds, tokens, grpc_url):
    channel = grpc.secure_channel(grpc_url, creds)
    stub = pb2_grpc.ContextToolStub(channel)

    req = pb2.ContextRequest(
        context_key="inventory:prod_12345:stock_count",
        parameters={"warehouse": "NY", "min_qty": "1"},
        capability_token=tokens["context"]
    )
    resp = stub.RequestContext(req, timeout=5)
    stock = resp.serialized_value.decode("utf-8")
    print(f"[Client] Stock Count = {stock}, metadata={resp.metadata}")

# ------------------------------------------------------------------
# 6. Subscribe to Telemetry (background thread)
# ------------------------------------------------------------------
def subscribe_telemetry(creds, tokens, grpc_url):
    def run():
        channel = grpc.secure_channel(grpc_url, creds)
        stub = pb2_grpc.ContextToolStub(channel)
        req = pb2.TelemetryRequest(
            stream_id="fleet123:engine_temp",
            capability_token=tokens["context"]
        )
        try:
            for frame in stub.SubscribeTelemetry(req):
                print(f"[Telemetry] ts={frame.timestamp_ms} | payload={frame.payload.decode('utf-8')}")
        except grpc.RpcError as e:
            print(f"[Telemetry] disconnected: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

# ------------------------------------------------------------------
# 7. Publish Low-Stock Event via EventBus
# ------------------------------------------------------------------
def publish_low_stock(creds, tokens):
    channel = grpc.secure_channel(EVENTBUS_ADDR, creds)
    stub = pb2_grpc.EventBusStub(channel)
    topic = "inventory:prod_12345:low_stock"
    payload = json.dumps({"current_stock": 9}).encode("utf-8")

    req = pb2.EventPublishRequest(
        topic=topic,
        payload=payload,
        publisher_token=tokens["event"]
    )
    resp = stub.Publish(req, timeout=5)
    print(f"[Client] Publish Low-Stock: success={resp.success}, msg='{resp.message}'")

# ------------------------------------------------------------------
# 8. Subscribe to Low-Stock Events
# ------------------------------------------------------------------
def subscribe_low_stock(creds, tokens):
    def run():
        channel = grpc.secure_channel(EVENTBUS_ADDR, creds)
        stub = pb2_grpc.EventBusStub(channel)
        req = pb2.EventSubscribeRequest(
            topic_filter="inventory:prod_12345:low_stock",
            subscriber_token=tokens["event"]
        )
        try:
            for env in stub.Subscribe(req):
                print(f"[LowStockEvent] topic={env.topic}, seq={env.sequence_id}, payload={env.payload.decode('utf-8')}")
        except grpc.RpcError as e:
            print(f"[LowStockEvent] disconnected: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t

# ------------------------------------------------------------------
# 9. Invoke compute_pricing
# ------------------------------------------------------------------
def invoke_compute_pricing(creds, tokens, grpc_url):
    channel = grpc.secure_channel(grpc_url, creds)
    stub = pb2_grpc.ContextToolStub(channel)
    req = pb2.ToolRequest(
        tool_name="compute_pricing",
        arguments={"sku": "prod_12345", "stock_count": "42"},
        capability_token=tokens["context"]
    )
    resp = stub.InvokeTool(req, timeout=5)
    price = resp.outputs.get("recommended_price", b"0.0").decode("utf-8")
    print(f"[Client] compute_pricing → recommended_price = {price}")

# ------------------------------------------------------------------
# MAIN DEMO FLOW
# ------------------------------------------------------------------
if __name__ == "__main__":
    insert_demo_context()
    creds = load_mtls_credentials()
    tokens = make_tokens()
    register_inventorydb(creds, tokens)
    endpoints = lookup_inventorydb(creds, tokens)
    if not endpoints:
        print("[Error] No InventoryDB endpoints found.")
        exit(1)
    inventory_ep = endpoints[0].grpc_url
    fetch_stock_count(creds, tokens, inventory_ep)
    tel_thread = subscribe_telemetry(creds, tokens, inventory_ep)
    time.sleep(12)
    publish_low_stock(creds, tokens)
    low_stock_thread = subscribe_low_stock(creds, tokens)
    time.sleep(5)
    invoke_compute_pricing(creds, tokens, inventory_ep)
    print("[Client] Demo complete.")
