#!/bin/sh
set -eu
refuse() { echo "Conflicting access settings: $*; see docs/operations/ingress.md" >&2; exit 1; }
case "$LG_ACCESS_MODE" in local|public|proxy) ;; *) refuse "LG_ACCESS_MODE must be local, public or proxy" ;; esac
case "$LG_SCHEME" in http|https) ;; *) refuse LG_SCHEME ;; esac
case "$LG_PUBLIC_DOMAIN" in ""|*[!a-zA-Z0-9.-]*) refuse LG_PUBLIC_DOMAIN ;; esac
case "$LG_PUBLIC_DOMAIN" in *[!0-9.]*) ;; *) refuse "use a DNS application domain; 127.0.0.1 is a local root alias" ;; esac
case "${LG_PUBLIC_PORT_SUFFIX:-}" in
  "") ;;
  :*)
    case "${LG_PUBLIC_PORT_SUFFIX#:}" in ""|*[!0-9]*) refuse LG_PUBLIC_PORT_SUFFIX ;; esac
    port=$(printf '%s' "${LG_PUBLIC_PORT_SUFFIX#:}" | sed 's/^0*//')
    [ -n "$port" ] && [ "${#port}" -le 5 ] && [ "$port" -le 65535 ] || refuse LG_PUBLIC_PORT_SUFFIX
    ;;
  *) refuse LG_PUBLIC_PORT_SUFFIX ;;
esac
case "$LG_ACCESS_MODE" in
  local)
    [ "$LG_LISTEN_SCHEME" = dual ] || refuse "local requires both HTTP and HTTPS"
    case "$LG_TLS_ISSUER" in internal|files) ;; *) refuse "local requires LG_TLS_ISSUER internal or files" ;; esac
    ;;
  public)
    [ "$LG_SCHEME" = https ] && [ "$LG_LISTEN_SCHEME" = https ] || refuse "public requires trusted HTTPS; include compose.public.yaml"
    case "$LG_PUBLIC_DOMAIN" in localhost|*.localhost|127.*|*:*|"") refuse "public requires a DNS domain" ;; esac
    case "$LG_TLS_ISSUER" in acme|files) ;; *) refuse "public requires LG_TLS_ISSUER acme or files" ;; esac
    ;;
  proxy)
    # Reject native and IPv4-mapped wildcard spellings, including compressed IPv6.
    bind=${LG_BIND_HOST:-127.0.0.1}
    case "$bind" in
      0.0.0.0) refuse "proxy requires a loopback or specific-interface LG_BIND_HOST" ;;
      *:*) case "$bind" in
        *[!0:.\[\]]*) ;;
        *) refuse "proxy requires a loopback or specific-interface LG_BIND_HOST" ;;
      esac ;;
    esac
    if printf '%s\n' "$bind" | grep -Eiq '^\[?([0:]*::[0:]*ffff:(0+:0+|0\.0\.0\.0)|(0+:){5}ffff:(0+:0+|0\.0\.0\.0|:|:0+|0+::))\]?$'; then
      refuse "proxy requires a loopback or specific-interface LG_BIND_HOST"
    fi
    [ "$LG_LISTEN_SCHEME" = http ] || refuse "behind another gateway, use HTTP only; include compose.proxy.yaml"
    [ "$LG_HTTPS_PUBLISHED" = false ] || refuse "proxy requires compose.proxy.yaml"
    [ -n "$LG_TRUSTED_PROXIES" ] || refuse "proxy requires LG_TRUSTED_PROXIES"
    ;;
esac
# Validate before expanding values into Caddyfile expressions and JSON. Only full
# DNS/IPv4 origins are accepted, with canonical decimal ports and no URL suffix.
origin() {
  origin_value=$2
  case "$origin_value" in
    http://*) origin_scheme=http ;;
    https://*) origin_scheme=https ;;
    *) refuse "$1 must be an http or https origin" ;;
  esac
  origin_authority=${origin_value#*://}
  case "$origin_authority" in ""|*[!a-zA-Z0-9.:-]*) refuse "$1 must contain a hostname and optional port only" ;; esac
  printf '%s\n' "$origin_authority" | awk '
    /^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[1-9][0-9]*)?$/ {
      n = split($0, a, ":")
      if (n == 2 && (length(a[2]) > 5 || a[2] > 65535)) exit 1
      n = split(a[1], labels, ".")
      for (i = 1; i <= n; i++)
        if (length(labels[i]) > 63 || labels[i] !~ /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/) exit 1
      exit 0
    }
    { exit 1 }
  ' || refuse "$1 must contain a hostname and optional port only (1-65535)"
  origin_host=${origin_authority%%:*}
  origin_authority=$(printf '%s' "$origin_authority" | tr '[:upper:]' '[:lower:]')
  origin_host=$(printf '%s' "$origin_host" | tr '[:upper:]' '[:lower:]')
  # Browsers omit the protocol's default port from Host.
  case "$origin_scheme:$origin_authority" in
    http:*:80) origin_authority=${origin_authority%:80} ;;
    https:*:443) origin_authority=${origin_authority%:443} ;;
  esac
  origin_value=$origin_scheme://$origin_authority
  [ "$LG_ACCESS_MODE" != public ] || [ "$origin_scheme" = https ] || refuse "$1 requires HTTPS in public mode"
}

seen_authorities=' '
for app in CONSOLE LITELLM LANGFUSE S3 RUSTFS; do
  case "$app" in
    CONSOLE) default_host=$LG_PUBLIC_DOMAIN; value=${LG_CONSOLE_URL:-} ;;
    LITELLM) default_host=litellm.$LG_PUBLIC_DOMAIN; value=${LG_LITELLM_URL:-} ;;
    LANGFUSE) default_host=langfuse.$LG_PUBLIC_DOMAIN; value=${LG_LANGFUSE_URL:-} ;;
    S3) default_host=s3.$LG_PUBLIC_DOMAIN; value=${LG_S3_URL:-} ;;
    RUSTFS) default_host=rustfs.$LG_PUBLIC_DOMAIN; value=${LG_RUSTFS_URL:-} ;;
  esac
  origin "LG_${app}_URL" "${value:-$LG_SCHEME://$default_host${LG_PUBLIC_PORT_SUFFIX:-}}"
  case "$seen_authorities" in *" $origin_authority "*) refuse "duplicate application authority at LG_${app}_URL" ;; esac
  seen_authorities="$seen_authorities$origin_authority "
  # Existing hostname routes are interfaces. An override cannot claim another
  # application's hostname.
  for route in CONSOLE LITELLM LANGFUSE S3 RUSTFS; do
    [ "$route" != "$app" ] || continue
    case "$route" in
      CONSOLE) reserved=$LG_PUBLIC_DOMAIN${LG_PUBLIC_PORT_SUFFIX:-} ;;
      *) reserved=$(printf '%s' "$route" | tr '[:upper:]' '[:lower:]').$LG_PUBLIC_DOMAIN ;;
    esac
    reserved=$(printf '%s' "$reserved" | tr '[:upper:]' '[:lower:]')
    if [ "$route" = CONSOLE ]; then
      case "$LG_SCHEME:$reserved" in http:*:80|https:*:443) reserved=${reserved%:*} ;; esac
      [ "$origin_authority" != "$reserved" ] || refuse "LG_${app}_URL conflicts with the console route"
    else
      [ "$origin_host" != "$reserved" ] || refuse "LG_${app}_URL conflicts with the $route route"
    fi
  done
  export "LG_${app}_URL=$origin_value" "LG_${app}_AUTHORITY=$origin_authority"
done

# Bootstrap runs this same validation once, without Caddy or Docker, and trusts its result.
if [ "${1:-}" = --origins ]; then
  printf '{"LG_CONSOLE_URL":"%s","LG_LITELLM_URL":"%s","LG_LANGFUSE_URL":"%s","LG_S3_URL":"%s","LG_RUSTFS_URL":"%s"}\n' \
    "$LG_CONSOLE_URL" "$LG_LITELLM_URL" "$LG_LANGFUSE_URL" "$LG_S3_URL" "$LG_RUSTFS_URL"
  exit 0
fi
exec "$@"
