"""Bounded public HTTP reads with DNS validation and redirect checks."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit
import httpx


async def public_address(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError('只允许公开HTTP/HTTPS地址')
    answers = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    addresses = [item[4][0] for item in answers]
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('不允许访问非公开地址')
    return parsed, addresses[0]

async def fetch_public_response(url):
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        for _ in range(4):
            parsed, address = await public_address(url)
            # Pin the checked address while retaining TLS hostname verification.
            target = httpx.URL(url).copy_with(host=address)
            async with client.stream('GET', target, headers={'Host': parsed.hostname}, extensions={'sni_hostname': parsed.hostname}) as response:
                if response.is_redirect:
                    location = response.headers.get('location')
                    if not location: raise ValueError('重定向缺少地址')
                    url = str(httpx.URL(url).join(location)); continue
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2*1024*1024: raise ValueError('响应超过2MiB限制')
                    chunks.append(chunk)
                body = b''.join(chunks)
                # Bytes are already decompressed; Scrapy must not decode them again.
                headers = [(k, v) for k, v in response.headers.multi_items()
                    if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
                return httpx.Response(response.status_code, headers=headers, content=body, request=httpx.Request('GET', url))
    raise ValueError('重定向超过限制')
