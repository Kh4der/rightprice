"""
Production, behind Caddy on a VPS.

Caddy terminates TLS and proxies to gunicorn over plain HTTP on the internal
Docker network, so Django has to be told to trust the forwarded proto header.
Without SECURE_PROXY_SSL_HEADER it decides every request is insecure, refuses to
set Secure cookies, and redirect-loops against SECURE_SSL_REDIRECT.
"""

from .base import *
from .base import env

DEBUG = False

# No defaults here on purpose: an unset ALLOWED_HOSTS in production should stop
# the boot, not quietly accept any Host header.
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Start conservatively. A mistaken year-long preload can lock out the site and
# every subdomain long after a configuration error is fixed. Increase these only
# after HTTPS has been observed working end-to-end in production.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
