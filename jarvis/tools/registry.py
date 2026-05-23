"""Tools registry - SSRF protection utilities."""

import ipaddress
from urllib.parse import urlparse


def _is_ssrf_url(url: str) -> bool:
    """Check if URL is potentially an SSRF attack vector.

    Returns True if URL should be blocked (is SSRF attempt).
    """
    try:
        parsed = urlparse(url)

        # Block file:// and other non-http schemes
        if parsed.scheme not in ['http', 'https']:
            return True

        # Extract hostname
        hostname = parsed.hostname
        if not hostname:
            return True

        # Try to resolve to IP
        try:
            import socket
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)

            # Block private/internal IPs
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                return True

            # Block localhost variations
            if hostname.lower() in ['localhost', '127.0.0.1', '::1', '0.0.0.0']:
                return True

            # Block internal domains
            if hostname.endswith('.local') or hostname.endswith('.internal'):
                return True

        except (socket.gaierror, ValueError):
            # Can't resolve - block it
            return True

        return False

    except Exception:
        # On any error, block it
        return True


__all__ = ['_is_ssrf_url']
