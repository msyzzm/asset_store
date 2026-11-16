#!/usr/bin/env bash
# Pull and rebuild the asset library when the web UI asks (the "更新" button).
#
# Runs on the Docker host as root, started by systemd (see install.sh):
#   asset-updater.path         when $DATA_DIR/update.request appears -> this script
#   asset-updater-check.timer  hourly -> this script --check (is there anything new?)
# The web container only writes update.request ("check" or "update") and reads
# update.status, which this script writes. Output of the last update: update.log.
set -uo pipefail

: "${REPO_DIR:?set REPO_DIR (see /etc/asset-updater.env)}"
: "${DATA_DIR:?set DATA_DIR (see /etc/asset-updater.env)}"
GIT_USER=${GIT_USER:-root}
COMPOSE=${COMPOSE:-docker compose}
INSTALLED_SELF=${INSTALLED_SELF:-/usr/local/lib/asset-updater/asset-updater.sh}

REQUEST="$DATA_DIR/update.request"
STATUS="$DATA_DIR/update.status"
LOG="$DATA_DIR/update.log"
DEPLOYED="$DATA_DIR/update.deployed"  # the commit the running containers were built from
STOP="$DATA_DIR/update.stop"  # the web UI's "停止": honoured until the containers are recreated
FETCH_TIMEOUT=${FETCH_TIMEOUT:-300}
STOP_GRACE=${STOP_GRACE:-20}  # seconds of SIGTERM before a stopped build is killed outright

current="" latest="" deployed="" behind="null" pending="" checked="null" started="null" finished="null" step=""
build_pid=""         # process group of a running build, so the TERM trap can take it down too
stopped_by_user=""   # set when update.stop actually ended the run: other failures stay errors

git_() {
    # As the repo's owner: their git credentials (HOME: systemd sets none), and no
    # "dubious ownership" refusal. GIT_LIMIT=seconds bounds a network call.
    local limit=()
    if [ -n "${GIT_LIMIT:-}" ] && command -v timeout > /dev/null; then limit=(timeout "$GIT_LIMIT"); fi
    if [ "$GIT_USER" = root ] || [ "$(id -un)" = "$GIT_USER" ]; then
        "${limit[@]}" git -C "$REPO_DIR" -c safe.directory="$REPO_DIR" "$@"
    else
        local home
        home=$(getent passwd "$GIT_USER" | cut -d: -f6)
        "${limit[@]}" runuser -u "$GIT_USER" -- env HOME="$home" git -C "$REPO_DIR" "$@"
    fi
}

json_str() {
    local s
    s=$(printf '%s' "$1" | tr -d '\000-\010\013\014\016-\037')
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    s=${s//$'\t'/ }
    s=${s//$'\r'/}
    s=${s//$'\n'/\\n}
    printf '"%s"' "$s"
}

json_or_null() {
    if [ -n "$1" ]; then json_str "$1"; else printf 'null'; fi
}

write_status() {  # state, error, log
    local state=$1 error=${2:-} log=${3:-} items="" line tmp
    while IFS= read -r line; do
        [ -n "$line" ] && items="${items:+$items,}$(json_str "$line")"
    done <<< "$pending"
    tmp="$STATUS.tmp.$$"
    printf '{"state":%s,"step":%s,"current":%s,"latest":%s,"deployed":%s,"behind":%s,"pending":[%s],"checked":%s,"started":%s,"finished":%s,"error":%s,"log":%s}\n' \
        "$(json_str "$state")" "$(json_or_null "$step")" "$(json_or_null "$current")" "$(json_or_null "$latest")" "$(json_or_null "$deployed")" "$behind" "$items" \
        "$checked" "$started" "$finished" "$(json_or_null "$error")" "$(json_or_null "$log")" > "$tmp" \
        && chmod 644 "$tmp" && mv -f "$tmp" "$STATUS"
}

read_versions() {  # after a fetch: where we are, where upstream is, what's in between
    current=$(git_ rev-parse --short HEAD 2>/dev/null)
    latest=$(git_ rev-parse --short '@{u}' 2>/dev/null)
    behind=$(git_ rev-list --count 'HEAD..@{u}' 2>/dev/null || echo null)
    pending=$(git_ log --format='%h %s' 'HEAD..@{u}' 2>/dev/null | head -n 50)
    deployed=$(head -c 64 "$DEPLOYED" 2>/dev/null | tr -dc '0-9A-Za-z')
    checked=$(date +%s)
}

stop_asked() {
    [ -f "$STOP" ]
}

go_on() {  # between steps: false (and remembered) once the web UI asked to stop
    if stop_asked; then
        stopped_by_user=1
        return 1
    fi
}

set_step() {  # fetch | pull | build | restart: the page offers "停止" before restart only
    step=$1
    if [ "$step" = restart ]; then rm -f "$STOP"; fi  # too late now: don't show "stopping"
    write_status running || true
}

in_repo_stoppable() {  # run "$@" in the repo, in its own process group: update.stop interrupts it
    local waited=0 rc
    set -m
    (cd "$REPO_DIR" && exec "$@") &
    build_pid=$!
    set +m
    while kill -0 "$build_pid" 2> /dev/null; do
        if stop_asked; then
            stopped_by_user=1
            if [ "$waited" -lt "$STOP_GRACE" ]; then
                kill -TERM -- "-$build_pid" 2> /dev/null
            else
                kill -KILL -- "-$build_pid" 2> /dev/null
            fi
            waited=$((waited + 1))
        fi
        sleep 1
    done
    wait "$build_pid"
    rc=$?
    build_pid=""
    return "$rc"
}

do_check() {
    read_versions
    started=$(date +%s)  # lets the page notice a check that hangs
    write_status checking
    if GIT_LIMIT=$FETCH_TIMEOUT git_ fetch --quiet > "$LOG.check" 2>&1; then
        read_versions
        write_status idle
    else
        write_status error "检查更新失败：git fetch 出错（网络或凭据）" "$(tail -n 20 "$LOG.check")"
    fi
}

do_update() {
    read_versions
    started=$(date +%s)
    finished="null"
    step=fetch
    write_status running
    # Killed (systemd timeout, stop): say so instead of leaving "running" behind.
    trap 'if [ -n "$build_pid" ]; then kill -TERM -- "-$build_pid" 2> /dev/null; fi; finished=$(date +%s); write_status error "更新被中断（超时或服务被停止），详情见 update.log" "$(tail -n 20 "$LOG")"; exit 1' TERM INT
    {
        echo "== $(date '+%F %T') update from $current (built: ${deployed:-unknown})"
        # A stop is checked between steps and interrupts the build; once the containers
        # are being recreated it is too late (the site could be left down).
        GIT_LIMIT=$FETCH_TIMEOUT git_ fetch --quiet && go_on \
            && set_step pull && git_ pull --ff-only && go_on \
            && set_step build && in_repo_stoppable $COMPOSE build && go_on \
            && set_step restart && (cd "$REPO_DIR" && $COMPOSE up -d --remove-orphans)
    } > "$LOG" 2>&1
    local ok=$? last_step=$step
    trap - TERM INT
    read_versions
    finished=$(date +%s)
    step=""
    if [ "$ok" -ne 0 ] && [ -n "$stopped_by_user" ] && [ "$last_step" != restart ]; then
        write_status stopped "" "$(tail -n 20 "$LOG")"
    elif [ "$ok" -eq 0 ]; then
        # Only a successful build moves this: a pull without a build leaves a rebuild pending.
        printf '%s\n' "$current" > "$DEPLOYED"
        deployed=$current
        # The script in the repo may have changed: next runs use the new one.
        if [ -w "$(dirname "$INSTALLED_SELF")" ]; then
            install -m 755 "$REPO_DIR/deploy/updater/asset-updater.sh" "$INSTALLED_SELF" 2>/dev/null
        fi
        write_status done "" "$(tail -n 20 "$LOG")"
    else
        write_status error "更新失败（git pull 或 docker compose 出错），详情见数据卷里的 update.log" "$(tail -n 20 "$LOG")"
    fi
}

main() {
    if command -v flock > /dev/null; then  # one run at a time (timer and button together)
        exec 9> "$DATA_DIR/update.lock"
        flock 9
    fi
    rm -f "$STOP"  # pressed while nothing ran: not meant for this run
    local action=""
    if [ -f "$REQUEST" ]; then
        action=$(tr -dc 'a-z' < "$REQUEST")
        rm -f "$REQUEST"
    elif [ "${1:-}" = "--check" ]; then
        action=check
    fi
    case "$action" in
        update) do_update ;;
        check) do_check ;;
        "") ;;
        *) read_versions; write_status error "不认识的请求：$action" ;;
    esac
    rm -f "$STOP"
}

main "$@"
