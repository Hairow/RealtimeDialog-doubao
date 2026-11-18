import websockets
import gzip
import json
import asyncio

from typing import Dict, Any

import protocol
import config


class RealtimeDialogClient:
    def __init__(self, config: Dict[str, Any], session_id: str):
        self.config = config
        self.logid = ""
        self.session_id = session_id
        self.ws = None

    async def _handshake(self) -> None:
        """WebSocket 握手 + StartConnection。

        这两步与具体会话无关，可以提前到待机时做（预建连接），
        唤醒后只剩一次 StartSession 往返，省 0.3~0.5s。
        ping keepalive 防止待机久了 NAT/服务端把空闲连接掐掉。
        """
        self.ws = await websockets.connect(
            self.config['base_url'],
            extra_headers=self.config['headers'],
            ping_interval=20, ping_timeout=10)
        self.logid = self.ws.response_headers.get("X-Tt-Logid")
        print(f"dialog server response logid: {self.logid}")

        # StartConnection request
        start_connection_request = bytearray(protocol.generate_header())
        start_connection_request.extend(int(1).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        start_connection_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        start_connection_request.extend(payload_bytes)
        await self.ws.send(start_connection_request)
        response = await self.ws.recv()
        print(f"StartConnection response: {protocol.parse_response(response)}")

    async def _start_session(self) -> None:
        # StartSession request
        request_params = config.start_session_req
        payload_bytes = str.encode(json.dumps(request_params))
        payload_bytes = gzip.compress(payload_bytes)
        start_session_request = bytearray(protocol.generate_header())
        start_session_request.extend(int(100).to_bytes(4, 'big'))
        start_session_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        start_session_request.extend(str.encode(self.session_id))
        start_session_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        start_session_request.extend(payload_bytes)
        await self.ws.send(start_session_request)
        response = await self.ws.recv()
        print(f"StartSession response: {protocol.parse_response(response)}")

    async def connect(self) -> None:
        """建立WebSocket连接（若已预建则只补 StartSession）"""
        print(f"url: {self.config['base_url']}")
        if self.ws is None:
            await self._handshake()
        try:
            await self._start_session()
        except Exception:
            # 预建连接可能已被服务端/网络回收（待机久了）：重建握手再试一次
            print("[connect] 预建连接失效，重新建连...")
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
            await self._handshake()
            await self._start_session()

    async def task_request(self, audio: bytes) -> None:
        task_request = bytearray(
            protocol.generate_header(message_type=protocol.CLIENT_AUDIO_ONLY_REQUEST,
                                     serial_method=protocol.NO_SERIALIZATION))
        task_request.extend(int(200).to_bytes(4, 'big'))
        task_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        task_request.extend(str.encode(self.session_id))
        payload_bytes = gzip.compress(audio)
        task_request.extend((len(payload_bytes)).to_bytes(4, 'big'))  # payload size(4 bytes)
        task_request.extend(payload_bytes)
        await self.ws.send(task_request)

    async def receive_server_response(self) -> Dict[str, Any]:
        try:
            response = await self.ws.recv()
            data = protocol.parse_response(response)
            return data
        except Exception as e:
            raise Exception(f"Failed to receive message: {e}")

    async def finish_session(self):
        finish_session_request = bytearray(protocol.generate_header())
        finish_session_request.extend(int(102).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        finish_session_request.extend((len(self.session_id)).to_bytes(4, 'big'))
        finish_session_request.extend(str.encode(self.session_id))
        finish_session_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        finish_session_request.extend(payload_bytes)
        await self.ws.send(finish_session_request)

    async def finish_connection(self):
        finish_connection_request = bytearray(protocol.generate_header())
        finish_connection_request.extend(int(2).to_bytes(4, 'big'))
        payload_bytes = str.encode("{}")
        payload_bytes = gzip.compress(payload_bytes)
        finish_connection_request.extend((len(payload_bytes)).to_bytes(4, 'big'))
        finish_connection_request.extend(payload_bytes)
        await self.ws.send(finish_connection_request)
        response = await self.ws.recv()
        print(f"FinishConnection response: {protocol.parse_response(response)}")

    async def close(self) -> None:
        """关闭WebSocket连接"""
        if self.ws:
            print(f"Closing WebSocket connection...")
            await self.ws.close()
