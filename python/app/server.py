"""gRPC entrypoint for the M1 deterministic RAG service."""
import os
import signal
from concurrent import futures

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from grpc_reflection.v1alpha import reflection

from app import rag_service


def main():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    health_service = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_service, server)
    health_service.set("", health_pb2.HealthCheckResponse.SERVING)
    rag_service.register(server)
    if os.environ.get("APP_ENV", "development") in {"development", "test"}:
        reflection.enable_server_reflection(
            (health.SERVICE_NAME, reflection.SERVICE_NAME, "rag.v1.RagService"), server
        )
    address = os.environ.get("GRPC_ADDR", "127.0.0.1:50051")
    if not server.add_insecure_port(address):
        raise RuntimeError("Cannot bind gRPC address")
    server.start()
    print(f"RAG foundation listening on {address}", flush=True)
    def stop(_signum, _frame):
        server.stop(5)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
