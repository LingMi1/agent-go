"""cognition 暴露标准 gRPC 健康检查：in-process channel 验 Check 返回 SERVING。"""

from __future__ import annotations

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc


async def test_health_servicer_serving():
    # 装配一个只含 Health servicer 的 aio server（与 grpc_server.serve 注册方式一致）
    server = grpc.aio.server()
    hs = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(hs, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    await hs.set("", health_pb2.HealthCheckResponse.SERVING)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            resp = await stub.Check(health_pb2.HealthCheckRequest(service=""))
            assert resp.status == health_pb2.HealthCheckResponse.SERVING
        await hs.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            resp = await stub.Check(health_pb2.HealthCheckRequest(service=""))
            assert resp.status == health_pb2.HealthCheckResponse.NOT_SERVING
    finally:
        await server.stop(grace=None)
