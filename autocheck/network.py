"""论文中的不可信链接：仅访问公网 HTTP(S)，连接固定到已校验的 IP。"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

import urllib3


class BlockedURL(ValueError):
    """地址不满足网络检查的访问边界。"""


def _is_public_ip(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return False
    if ip.version == 6:
        # 不允许映射/隧道/转换地址绕过 IPv4 访问限制。
        return (ip in ipaddress.ip_network("2000::/3")
                and ip not in ipaddress.ip_network("2002::/16")
                and ip not in ipaddress.ip_network("2001::/32"))
    return ip not in ipaddress.ip_network("192.0.0.0/24")


def _public_target(url):
    if re.search(r'[\x00-\x20\x7f\\]', url):
        raise BlockedURL("invalid URL characters")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BlockedURL("only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise BlockedURL("URLs containing credentials are not allowed")
        host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    except BlockedURL:
        raise
    except (ValueError, UnicodeError) as error:
        raise BlockedURL("invalid URL") from error
    if port not in (80, 443):
        raise BlockedURL("only ports 80 and 443 are allowed")
    if (not host or '%' in host or ('.' not in host and ':' not in host)
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
        raise BlockedURL("local hostnames are not allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        addresses = [item[4][0] for item in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM)]
    else:
        addresses = [str(literal)]
    if not addresses or not all(_is_public_ip(address) for address in addresses):
        raise BlockedURL("destination includes a non-public IP address")
    return parsed, host, port, str(ipaddress.ip_address(addresses[0]))


def request_public_url(url, headers):
    """只请求一跳，返回状态码和 Location；不读取响应正文、不自动跳转。"""
    parsed, host, port, address = _public_target(url)
    request_headers = dict(headers)
    authority = f"[{host}]" if ':' in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    request_headers["Host"] = authority if port == default_port else f"{authority}:{port}"
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query

    # 直接连接校验过的 IP；不再次解析域名，也不自动使用环境代理或 .netrc。
    # HTTPS 保留原始主机名用于 SNI 和证书校验。
    options = dict(port=port, timeout=urllib3.Timeout(connect=15, read=15))
    if parsed.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            address, server_hostname=host, assert_hostname=host,
            cert_reqs="CERT_REQUIRED", **options)
    else:
        pool = urllib3.HTTPConnectionPool(address, **options)
    try:
        response = pool.urlopen("GET", target, headers=request_headers,
                                redirect=False, retries=False, preload_content=False)
        try:
            return response.status, response.headers.get("Location")
        finally:
            response.close()
    finally:
        pool.close()
