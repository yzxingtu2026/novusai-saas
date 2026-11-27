#!/bin/sh
set -e

# Generate _app.config.js from public runtime environment variables.
# This allows the frontend API URL (and other GLOB config) to be set via
# docker-compose.yml ENVIRONMENT at container startup, instead of being
# baked into the image at build time.

if [ -n "${FRONTEND_VITE_GLOB_API_URL:-}" ]; then
  export VITE_GLOB_API_URL="$FRONTEND_VITE_GLOB_API_URL"
fi

CONFIG="{"
FIRST=true

PUBLIC_KEYS=$(env | sed -nE 's/^(VITE_GLOB_[A-Za-z0-9_]+|VITE_PLATFORM_DOMAINS)=.*/\1/p' | sort)
for KEY in $PUBLIC_KEYS; do
  VALUE=$(printenv "$KEY" | sed 's/\\/\\\\/g; s/"/\\"/g')
  if [ "$FIRST" = true ]; then
    FIRST=false
  else
    CONFIG="$CONFIG,"
  fi
  CONFIG="$CONFIG\"$KEY\":\"$VALUE\""
done
CONFIG="$CONFIG}"

cat > /usr/share/nginx/html/_app.config.js << JS_EOF
window._VBEN_ADMIN_PRO_APP_CONF_=$CONFIG;Object.freeze(window._VBEN_ADMIN_PRO_APP_CONF_);Object.defineProperty(window,"_VBEN_ADMIN_PRO_APP_CONF_",{configurable:false,writable:false});
JS_EOF

printf '[entrypoint] _app.config.js generated with keys: '
printf '%s' "$PUBLIC_KEYS" | tr '\n' ' '
printf '\n'

exec nginx -g 'daemon off;'
