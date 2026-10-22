"""Untrusted QQ media: validate DNS at connection time, no redirects, bounded streams."""
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp

MAX_BYTES = 10 * 1024 * 1024


def public_ip(value):
    ip = ipaddress.ip_address(value)
    if not ip.is_global or (getattr(ip,'ipv4_mapped',None) and not ip.ipv4_mapped.is_global):
        raise ValueError('Non-public media destination')


def validate_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError('Invalid media URL')
    if p.port not in (None,80,443) or p.hostname.lower() in ('localhost','localhost.localdomain'):
        raise ValueError('Disallowed media host/port')
    try:
        ipaddress.ip_address(p.hostname)
    except ValueError:
        if '.' not in p.hostname or '%' in p.hostname:
            raise ValueError('Invalid media hostname') from None
    else:
        public_ip(p.hostname)
    return p


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.inner = aiohttp.resolver.DefaultResolver()

    async def resolve(self,host,port=0,family=socket.AF_INET):
        answers = await self.inner.resolve(host,port,family)
        if not answers:
            raise ValueError('No media DNS answers')
        for item in answers:
            public_ip(item['host'])
        return answers

    async def close(self):
        await self.inner.close()


async def read_response(r, kind='image', max_bytes=MAX_BYTES):
    if r.status != 200:
        raise ValueError('Media HTTP rejected')
    mime = r.headers.get('Content-Type','').split(';')[0].strip().lower()
    allowed = {'image/png','image/jpeg','image/gif','image/webp'} if kind=='image' else {'audio/silk','audio/amr','application/octet-stream','audio/ogg','audio/mpeg'}
    if mime not in allowed:
        raise ValueError('Unsupported media MIME')
    length = r.headers.get('Content-Length')
    if length is not None and (not length.isdigit() or int(length)>max_bytes):
        raise ValueError('Media size rejected')
    chunks=[]; size=0
    async for chunk in r.content.iter_chunked(65536):
        size += len(chunk)
        if size>max_bytes:
            raise ValueError('Media stream exceeds limit')
        chunks.append(chunk)
    data=b''.join(chunks)
    magic = {'image/png':data.startswith(b'\x89PNG\r\n\x1a\n'),
             'image/jpeg':data.startswith(b'\xff\xd8\xff'),
             'image/gif':data.startswith((b'GIF87a',b'GIF89a')),
             'image/webp':data.startswith(b'RIFF') and data[8:12]==b'WEBP'}
    if kind=='image' and not magic.get(mime,False):
        raise ValueError('Media MIME/signature mismatch')
    if kind=='audio' and not (b'#!SILK' in data[:16] or data.startswith((b'OggS',b'#!AMR',b'ID3',b'\xff\xfb'))):
        raise ValueError('Audio signature mismatch')
    return data


async def download(url,kind='image',max_bytes=MAX_BYTES):
    validate_url(url)
    resolver = PublicResolver()
    connector = aiohttp.TCPConnector(resolver=resolver,use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(connector=connector,trust_env=False,
                timeout=aiohttp.ClientTimeout(total=20,connect=5,sock_read=5)) as session:
            async with session.get(url,allow_redirects=False) as response:
                return await read_response(response,kind,max_bytes)
    finally:
        await resolver.close()
