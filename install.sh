#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEST=/opt/remnanode-traffic-guard
DATA=/var/lib/remnanode-traffic-guard
GUARD=remnanode-traffic-guard
PANEL=remnanode-traffic-panel
WEB_USER=traffic-guard-web
OLD_SERVICE=${OLD_SERVICE:-chanvpn-london-guard-v3.service}
BACKUP="/var/backups/remnanode-traffic-guard/$(date -u +%Y%m%dT%H%M%S)-$$"
changed=0
protected_before=0
fail() { printf 'ERROR: %s\n' "$*" >&2; return 1; }
docker_local() { docker --host unix:///var/run/docker.sock "$@"; }
compose() { docker_local compose --project-directory "$DEST" --env-file "$DEST/.env" -f "$DEST/compose.yaml" "$@"; }
on_error() {
    local code=$?
    trap - ERR
    if (( changed )); then
        if (( protected_before )); then docker_local stop --time 2 "$TARGET_CONTAINER" >/dev/null 2>&1 || true; fi
        printf 'Installation failed. Inspect Docker/logs. Backup: %s\n' "$BACKUP" >&2
    fi
    exit "$code"
}
trap on_error ERR
[[ $(uname -s) == Linux ]] || fail 'Run on a Linux server.'
[[ $EUID == 0 ]] || fail 'Run with sudo/root.'
[[ -r /etc/os-release ]] || fail '/etc/os-release is required.'
. /etc/os-release
[[ $ID == ubuntu || $ID == debian ]] || fail 'Automatic dependency setup supports Ubuntu and Debian.'
command -v systemctl >/dev/null || fail 'A systemd host is required.'
install_missing() {
    local package missing=()
    for package in "$@"; do
        if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -qx 'install ok installed'; then missing+=("$package"); fi
    done
    if (( ${#missing[@]} )); then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
}
install_missing ca-certificates curl python3 util-linux passwd
exec 9>/run/remnanode-traffic-guard-install.lock
flock -n 9 || fail 'Another installation is running.'
docker_repository() {
    [[ ${VERSION_CODENAME:-} =~ ^[a-z0-9]+$ ]] || fail 'Distribution codename missing.'
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL --retry 3 "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    if ! grep -Rqs "download.docker.com/linux/$ID" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
        cat > /etc/apt/sources.list.d/traffic-guard-docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    fi
    apt-get update
}
if ! command -v docker >/dev/null || { ! docker_local info >/dev/null 2>&1 && ! command -v dockerd >/dev/null; }; then
    printf 'Docker not found: installing Engine, Buildx and Compose.\n'
    docker_repository
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
elif ! docker_local compose version >/dev/null 2>&1; then
    printf 'Docker is present; installing the missing Compose plugin.\n'
    apt-get update
    if apt-cache show docker-compose-v2 >/dev/null 2>&1; then install_missing docker-compose-v2
    else docker_repository; install_missing docker-compose-plugin; fi
fi
if ! docker_local buildx version >/dev/null 2>&1; then
    apt-get update
    if apt-cache show docker-buildx >/dev/null 2>&1; then install_missing docker-buildx
    else docker_repository; install_missing docker-buildx-plugin; fi
fi
systemctl enable docker >/dev/null
if ! docker_local info >/dev/null 2>&1; then systemctl start docker; fi
[[ -S /var/run/docker.sock ]] || fail 'Local rootful Docker socket not found.'
docker_local info >/dev/null
docker_local compose version >/dev/null
if [[ -f $DEST/.env ]]; then
    while IFS='=' read -r key value; do
        case "$key" in
            TARGET_CONTAINER) TARGET_CONTAINER=${TARGET_CONTAINER:-$value} ;;
            INTERFACE) INTERFACE=${INTERFACE:-$value} ;;
            LIMIT_BYTES) LIMIT_BYTES=${LIMIT_BYTES:-$value} ;;
            WEB_PORT) WEB_PORT=${WEB_PORT:-$value} ;;
            WEB_PATH) WEB_PATH=${WEB_PATH:-$value} ;;
            WEB_BIND) WEB_BIND=${WEB_BIND:-$value} ;;
            WEB_UID|WEB_GID|''|'#'*) ;;
            *) fail "Unknown saved setting: $key" ;;
        esac
    done < "$DEST/.env"
fi
TARGET_CONTAINER=${TARGET_CONTAINER:-remnanode}
INTERFACE=${INTERFACE:-eth0}
LIMIT_BYTES=${LIMIT_BYTES:-145000000000}
WEB_PORT=${WEB_PORT:-47479}
WEB_BIND=${WEB_BIND:-0.0.0.0}
WEB_PATH=${WEB_PATH:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}
[[ $TARGET_CONTAINER =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'Invalid target name.'
[[ $TARGET_CONTAINER != "$GUARD" && $TARGET_CONTAINER != "$PANEL" ]] || fail 'Target cannot be the guard or panel.'
[[ $INTERFACE =~ ^[a-zA-Z0-9_.:-]+$ ]] || fail 'Invalid interface.'
[[ $LIMIT_BYTES =~ ^[1-9][0-9]{0,17}$ ]] || fail 'Invalid byte limit.'
[[ $OLD_SERVICE =~ ^[a-zA-Z0-9_.@-]+$ ]] || fail 'Invalid old service name.'
[[ -r /sys/class/net/$INTERFACE/statistics/rx_bytes ]] || fail 'Interface not found. Set INTERFACE=ens3 if needed.'
if [[ -t 0 ]]; then
    printf '\nWeb panel settings. HTTP does not encrypt passwords or sessions.\n'
    read -r -p "Web port [$WEB_PORT]: " answer
    WEB_PORT=${answer:-$WEB_PORT}
    read -r -p "Secret path [$WEB_PATH]: " answer
    WEB_PATH=${answer:-$WEB_PATH}
    if [[ -f $DEST/secrets/web-auth.json ]]; then read -r -s -p 'New panel password (Enter to keep current): ' answer
    else read -r -s -p 'Panel password (at least 12 characters): ' answer; fi
    printf '\n'
    if [[ -n $answer ]]; then
        WEB_PASSWORD=$answer
        read -r -s -p 'Repeat password: ' repeated
        printf '\n'
        [[ $WEB_PASSWORD == "$repeated" ]] || fail 'Passwords do not match.'
        unset answer repeated
    fi
fi
if [[ -n ${WEB_PASSWORD_FILE:-} ]]; then
    [[ -f $WEB_PASSWORD_FILE ]] || fail 'WEB_PASSWORD_FILE not found.'
    IFS= read -r WEB_PASSWORD < "$WEB_PASSWORD_FILE" || [[ -n ${WEB_PASSWORD:-} ]]
fi
[[ $WEB_PORT =~ ^[0-9]{2,5}$ ]] && (( 10#$WEB_PORT >= 1024 && 10#$WEB_PORT <= 65535 )) || fail 'Web port must be 1024-65535.'
[[ $WEB_PATH =~ ^[a-zA-Z0-9_-]{20,96}$ ]] || fail 'Secret path must be 20-96 characters (letters, digits, _ or -).'
python3 -c 'import ipaddress,sys; ipaddress.IPv4Address(sys.argv[1])' "$WEB_BIND"
if [[ -n ${WEB_PASSWORD:-} ]]; then
    (( ${#WEB_PASSWORD} >= 12 && ${#WEB_PASSWORD} <= 256 )) || fail 'Password must be 12-256 characters.'
elif [[ ! -f $DEST/secrets/web-auth.json ]]; then fail 'Provide a password interactively or set WEB_PASSWORD_FILE.'; fi
for container in "$GUARD" "$PANEL"; do
    if docker_local inspect "$container" >/dev/null 2>&1; then
        owner=$(docker_local inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")
        [[ $owner == remnanode-traffic-guard ]] || fail "Container name belongs to another project: $container"
    fi
done
current_port=$(docker_local port "$PANEL" 8080/tcp 2>/dev/null || true)
if ! grep -q ":$WEB_PORT$" <<< "$current_port"; then
    python3 -c 'import socket,sys; s=socket.socket(); s.bind((sys.argv[1],int(sys.argv[2]))); s.close()' "$WEB_BIND" "$WEB_PORT"
fi
if ! id "$WEB_USER" >/dev/null 2>&1; then useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$WEB_USER"; fi
WEB_UID=$(id -u "$WEB_USER")
WEB_GID=$(id -g "$WEB_USER")
[[ $WEB_UID != 0 ]] || fail 'Web user must not be root.'
for file in guard.py history.py web.py test_guard.py test_web.py Dockerfile compose.yaml .dockerignore; do [[ -f $SOURCE/$file ]] || fail "Missing source: $file"; done
[[ -d $SOURCE/static ]] || fail 'Missing panel assets.'
mkdir -p -m 700 "$BACKUP" "$DEST"
mkdir -p "$DATA" "$DATA/commands" "$DEST/secrets"
chown root:"$WEB_GID" "$DATA" "$DEST/secrets"
chmod 0750 "$DATA" "$DEST/secrets"
chown "$WEB_UID:$WEB_GID" "$DATA/commands"
chmod 0770 "$DATA/commands"
if [[ -f $DATA/state.json ]]; then
    protected_before=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(int(not (s.get("mode")=="waiting" and s.get("passive_wait",False))))' "$DATA/state.json" 2>/dev/null || printf 1)
fi
if [[ -f $DEST/compose.yaml ]]; then cp -a "$DEST/." "$BACKUP/installation"; fi
if docker_local inspect "$TARGET_CONTAINER" >/dev/null 2>&1; then docker_local inspect --format 'Restart={{.HostConfig.RestartPolicy.Name}} Running={{.State.Running}}' "$TARGET_CONTAINER" > "$BACKUP/target-before.txt"
else printf 'Target container is not created yet; guard will wait for it.\n'; fi
if [[ $SOURCE != "$DEST" ]]; then
    for file in guard.py history.py web.py test_guard.py test_web.py Dockerfile compose.yaml .dockerignore README.md VALIDATION.md DESIGN.md install.sh; do
        [[ ! -f $SOURCE/$file ]] || install -m 600 "$SOURCE/$file" "$DEST/$file"
    done
    cp -a "$SOURCE/static" "$DEST/"
fi
if [[ -n ${WEB_PASSWORD:-} ]]; then
    printf '%s\n' "$WEB_PASSWORD" | python3 "$DEST/web.py" hash-password > "$DEST/secrets/web-auth.json.tmp"
    chmod 0440 "$DEST/secrets/web-auth.json.tmp"
    chown root:"$WEB_GID" "$DEST/secrets/web-auth.json.tmp"
    mv "$DEST/secrets/web-auth.json.tmp" "$DEST/secrets/web-auth.json"
    unset WEB_PASSWORD
fi
chmod 0440 "$DEST/secrets/web-auth.json"
chown root:"$WEB_GID" "$DEST/secrets/web-auth.json"
printf 'TARGET_CONTAINER=%s\nINTERFACE=%s\nLIMIT_BYTES=%s\nWEB_PORT=%s\nWEB_PATH=%s\nWEB_BIND=%s\nWEB_UID=%s\nWEB_GID=%s\n' "$TARGET_CONTAINER" "$INTERFACE" "$LIMIT_BYTES" "$WEB_PORT" "$WEB_PATH" "$WEB_BIND" "$WEB_UID" "$WEB_GID" > "$DEST/.env"
export TARGET_CONTAINER INTERFACE LIMIT_BYTES WEB_PORT WEB_PATH WEB_BIND WEB_UID WEB_GID
compose config --quiet
printf 'Building images and testing before switching services...\n'
compose build --pull
compose run --rm --no-deps --entrypoint python guard -c 'import guard; print(guard.sample(guard.config()))'
changed=1
if [[ $OLD_SERVICE != none ]]; then
    service_state=$(systemctl show --property=LoadState --value "$OLD_SERVICE" 2>/dev/null || true)
    if [[ -n $service_state && $service_state != not-found ]]; then
        systemctl cat "$OLD_SERVICE" > "$BACKUP/old-service-unit.txt"
        systemctl is-enabled "$OLD_SERVICE" > "$BACKUP/old-service-enabled.txt" 2>&1 || true
        systemctl disable --now "$OLD_SERVICE"
        if systemctl is-active --quiet "$OLD_SERVICE"; then fail 'Old guard is still active.'; fi
    fi
fi
compose stop guard web
cp -a "$DATA" "$BACKUP/data"
find "$DATA" -maxdepth 1 -type f -exec chown root:"$WEB_GID" {} + -exec chmod 0640 {} +
if [[ ! -f $DATA/state.json && ! -f $DATA/fault.json ]]; then compose run --rm --no-deps guard init; fi
compose up -d --no-build
for ((attempt=0; attempt<45; attempt++)); do
    health=$(docker_local inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$GUARD")
    panel_running=$(docker_local inspect --format '{{.State.Running}}' "$PANEL")
    if [[ $health == healthy && $panel_running == true ]]; then
        probe_host=$WEB_BIND
        [[ $probe_host != 0.0.0.0 ]] || probe_host=127.0.0.1
        http_code=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://$probe_host:$WEB_PORT/")
        [[ $http_code == 404 ]] || fail 'Panel hidden-path check failed.'
        http_code=$(curl --silent --output /dev/null --write-out '%{http_code}' "http://$probe_host:$WEB_PORT/$WEB_PATH")
        [[ $http_code == 200 ]] || fail 'Panel login check failed.'
        server_ip=$(hostname -I | awk '{print $1}')
        printf '\nInstalled. Panel: http://%s:%s/%s\n' "${server_ip:-SERVER_IP}" "$WEB_PORT" "$WEB_PATH"
        printf 'Traffic before installation is unknown. Guard observes until 03:00 Moscow; node keeps its current state.\n'
        printf 'Directory: %s\nBackup: %s\nLogs: docker logs --tail 100 %s\n' "$DEST" "$BACKUP" "$GUARD"
        printf 'HTTP is unencrypted. The port can be scanned; the secret path does not replace the password.\n'
        exit 0
    fi
    sleep 2
done
compose logs --tail 60 guard web
false
