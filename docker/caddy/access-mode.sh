#!/bin/sh
set -eu
refuse() { echo "Conflicting access settings: $*; see docs/operations/ingress.md" >&2; exit 1; }
case "$LG_SCHEME" in http|https) ;; *) refuse LG_SCHEME ;; esac
case "$LG_PUBLIC_DOMAIN" in ""|*[!a-zA-Z0-9.-]*) refuse LG_PUBLIC_DOMAIN ;; esac
case "$LG_PUBLIC_DOMAIN" in *[!0-9.]*) ;; *) refuse "use a DNS application domain; 127.0.0.1 is a local root alias" ;; esac
case "${LG_PUBLIC_PORT_SUFFIX:-}" in
  "") ;;
  :*) case "${LG_PUBLIC_PORT_SUFFIX#:}" in ""|*[!0-9]*) refuse LG_PUBLIC_PORT_SUFFIX ;; esac ;;
  *) refuse LG_PUBLIC_PORT_SUFFIX ;;
esac
case "$LG_ACCESS_MODE" in
  local)
    [ "$LG_LISTEN_SCHEME" = dual ] && [ "$LG_TLS_ISSUER" = internal ] || refuse "local requires both HTTP and self-signed HTTPS"
    ;;
  public)
    [ "$LG_SCHEME" = https ] && [ "$LG_LISTEN_SCHEME" = https ] && [ "$LG_TLS_ISSUER" = acme ] || refuse "public requires trusted HTTPS; include compose.public.yaml"
    case "$LG_PUBLIC_DOMAIN" in localhost|*.localhost|127.*|*:*|"") refuse "public requires a DNS domain" ;; esac
    ;;
  proxy)
    # Reject IPv4 and all bracketed/expanded IPv6 wildcard spellings.
    bind=${LG_BIND_HOST:-127.0.0.1}
    case "$bind" in
      0.0.0.0) refuse "proxy requires a loopback or specific-interface LG_BIND_HOST" ;;
      *:*) case "$bind" in
        *[!0:\[\]]*) ;;
        *) refuse "proxy requires a loopback or specific-interface LG_BIND_HOST" ;;
      esac ;;
    esac
    [ "$LG_LISTEN_SCHEME" = http ] && [ "$LG_TLS_ISSUER" = none ] || refuse "behind another gateway, use HTTP only; include compose.proxy.yaml"
    [ "$LG_HTTPS_PUBLISHED" = false ] || refuse "proxy requires compose.proxy.yaml"
    [ -n "$LG_TRUSTED_PROXIES" ] || refuse "proxy requires LG_TRUSTED_PROXIES"
    ;;
  *) refuse "LG_ACCESS_MODE must be local, public or proxy" ;;
esac
exec "$@"
