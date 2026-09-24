#!/usr/bin/env bash
# land.sh — land ONLY the named paths onto origin/main via plumbing.
# Works from any dirty checkout, worktree, or branch. Never touches HEAD,
# never stashes, never stages anything outside the given paths, never
# deletes a worktree (the hourly reaper owns reclamation).
#
# usage: land.sh -m "message" [--repo PATH] [--remove PATH ...] <path>...
#   <path>...      add/update these paths from the working tree.
#   --remove PATH  remove PATH from origin/main in the same commit (scoped
#                  deletes and renames). Operates on the temp index only,
#                  never the working tree. At least one add path OR one
#                  --remove is required.
set -euo pipefail

msg="" repo=""
paths=()
removes=()
while [ $# -gt 0 ]; do
    case "$1" in
        -m) msg="${2:?-m needs a message}"; shift 2 ;;
        --repo) repo="${2:?--repo needs a path}"; shift 2 ;;
        --remove) removes+=("${2:?--remove needs a path}"); shift 2 ;;
        *) paths+=("$1"); shift ;;
    esac
done
[ -n "$msg" ] || { echo "land: commit message required (-m)" >&2; exit 2; }
[ ${#paths[@]} -gt 0 ] || [ ${#removes[@]} -gt 0 ] || { echo "land: at least one explicit path or --remove required" >&2; exit 2; }
for p in "${paths[@]}" "${removes[@]}"; do
    case "$p" in
        .|./|-A|--all|:/|'*') echo "land: refuse repo-wide pathspec '$p' — name the files" >&2; exit 2 ;;
    esac
done

repo="${repo:-$(git rev-parse --show-toplevel)}"
cd "$repo"
git fetch -q origin main

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANONYMIZE="${ANONYMIZE_DATA_ROOT:-$SCRIPT_DIR/../../anonymize-data/run.sh}"
VG_TMP=""
cleanup() {
    rm -f "$GIT_INDEX_FILE" "$GLCONF"
    if [ -n "$VG_TMP" ] && [ -d "$VG_TMP" ]; then
        [ ! -d "$VG_TMP/in" ] || find "$VG_TMP/in" -type f -exec shred -u {} + 2>/dev/null
        [ ! -f "$VG_TMP/policy.json" ] || shred -u "$VG_TMP/policy.json" 2>/dev/null
        rm -rf "$VG_TMP"
    fi
}

# value-gate: fail-closed representation-aware scan of staged content against
# blocked literal values (one per line, case-insensitive) via anonymize-data.
# Refusal happens BEFORE commit-tree; shreddable temp dir under 0700 umask.
run_value_gate() {
    local blocked_file="${LAND_BLOCKED_VALUES:-$HOME/.config/land/blocked-values}"
    if [ ! -s "$blocked_file" ]; then
        echo "value-gate: no blocked values configured, skipped" >&2
        return 0
    fi
    if [ "${LAND_SKIP_VALUE_GATE:-0}" = "1" ]; then
        echo "value-gate: SKIPPED by LAND_SKIP_VALUE_GATE=1" >&2
        return 0
    fi
    VG_TMP="$(umask 077 && mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/land-vg.XXXXXX")" \
        || { echo "value-gate: REFUSED — cannot create temp dir; set LAND_SKIP_VALUE_GATE=1 to override" >&2; exit 3; }
    mkdir -p "$VG_TMP/in"; : > "$VG_TMP/map"
    python3 - "$blocked_file" "$VG_TMP/policy.json" <<'PYEOF'
import json, sys
rules = []
seen = set()
with open(sys.argv[1]) as f:
    for line in f:
        v = line.rstrip("\n")
        if not v.strip() or v.casefold() in seen:
            continue
        seen.add(v.casefold())
        rules.append({"rule_id": f"blocked-{len(rules)+1}", "type": "secret",
                      "value": v, "match": "literal", "case_sensitive": False})
with open(sys.argv[2], "w") as out:
    json.dump({"version": 1, "sensitive_values": rules, "protected_values": []}, out)
PYEOF
    local i=0 p blob tmpname
    while IFS= read -r p; do
        [ -n "$p" ] || continue
        blob="$(git rev-parse -q --verify "$tree:$p" 2>/dev/null)" || continue
        i=$((i + 1))
        case "$p" in *.json|*.csv|*.sqlite) ext="" ;; *) ext=".txt" ;; esac
        tmpname="$(printf '%03d' "$i")-$(basename "$p" | tr '/' '_')$ext"
        git cat-file blob "$blob" > "$VG_TMP/in/$tmpname"
        printf '%s\t%s\n' "$tmpname" "$p" >> "$VG_TMP/map"
    done < <(git diff --name-only origin/main "$tree")
    local rc=0 repl
    bash "$ANONYMIZE" anonymize --input "$VG_TMP/in" --policy "$VG_TMP/policy.json" \
        --output "$VG_TMP/out" >/dev/null 2>&1 || rc=$?
    repl="$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))["replacements_applied"])
except Exception:
    print("ERR")
' "$VG_TMP/out/report.json" 2>/dev/null)"
    if [ "$rc" -ne 0 ] || [ "$repl" = "ERR" ] || ! [ "$repl" -eq "$repl" ] 2>/dev/null; then
        echo "value-gate: REFUSED — anonymize-data check failed ($rc); set LAND_SKIP_VALUE_GATE=1 to override" >&2
        exit 3
    fi
    if [ "$repl" -gt 0 ]; then
        local hits=() t orig
        while IFS= read -r t; do
            [ -f "$VG_TMP/out/corpus/$t" ] || continue
            if ! cmp -s "$VG_TMP/in/$t" "$VG_TMP/out/corpus/$t"; then
                orig="$(awk -F'\t' -v t="$t" '$1 == t {print $2; exit}' "$VG_TMP/map")"
                hits+=("${orig:-$t}")
            fi
        done < <(cut -f1 "$VG_TMP/map")
        echo "value-gate: REFUSED — blocked value found in: ${hits[*]:-unknown}" >&2
        exit 3
    fi
}

export GIT_INDEX_FILE="$(mktemp)"
GLCONF="$(mktemp --suffix=.toml)"
trap cleanup EXIT
cat > "$GLCONF" <<'EOF'
[extend]
useDefault = true
disabledRules = ["generic-api-key", "curl-auth-header", "curl-auth-user"]
EOF

for attempt in 1 2 3; do
    git read-tree origin/main
    [ ${#paths[@]} -gt 0 ] && git add -A -- "${paths[@]}"
    [ ${#removes[@]} -gt 0 ] && git rm -rf --cached --ignore-unmatch -q -- "${removes[@]}"
    tree=$(git write-tree)
    if [ "$tree" = "$(git rev-parse origin/main^{tree})" ]; then
        echo "land: no change vs origin/main for named paths — nothing to push"
        exit 0
    fi
    # value gate first: fail-closed representation-aware blocked-value scan.
    run_value_gate
    # pre-push guard: inspect only what this land adds relative to origin/main
    added=$(git diff --no-color -U0 origin/main "$tree" | grep '^+' | grep -v '^+++' || true)
    changed=$(git diff --name-only origin/main "$tree")
    if [ -f "$repo/.land-deny" ]; then
        while IFS= read -r deny_re || [ -n "$deny_re" ]; do
            case "$deny_re" in ''|'#'*) continue ;; esac
            deny_hits=$(printf '%s\n' "$changed" | grep -E -- "$deny_re" || true)
            if [ -n "$deny_hits" ]; then
                printf 'land: REFUSED — path(s) match .land-deny pattern %s:\n%s\n' "$deny_re" "$deny_hits" >&2
                exit 3
            fi
        done < "$repo/.land-deny"
    fi
    if printf '%s\n' "$added" | grep -Eq '\b[0-9A-Fa-f]{32}-[0-9A-Fa-f]{2}\b'; then
        echo "land: REFUSED — CrowdStrike CID pattern in added content" >&2
        exit 3
    fi
    blocked_file="${LAND_BLOCKED_VALUES:-$HOME/.config/land/blocked-values}"
    if [ -s "$blocked_file" ] && printf '%s\n' "$added" | grep -F -q -f "$blocked_file"; then
        echo "land: REFUSED — blocked literal value present" >&2
        exit 3
    fi
    if command -v gitleaks >/dev/null 2>&1; then
        gl_rc=0
        printf '%s\n' "$added" | gitleaks stdin --no-banner --redact --exit-code 3 --config "$GLCONF" >&2 || gl_rc=$?
        if [ "$gl_rc" -eq 3 ]; then
            echo "land: REFUSED — gitleaks found a secret" >&2
            exit 3
        fi
        [ "$gl_rc" -eq 0 ] || echo "land: gitleaks error, secret scan incomplete" >&2
    fi
    sha=$(git commit-tree "$tree" -p origin/main -m "$msg")
    if git push -q origin "$sha:main" 2>/dev/null; then
        git fetch -q origin main
        git merge-base --is-ancestor "$sha" origin/main
        echo "landed $sha on origin/main:"
        git show --stat --oneline "$sha" | sed -n '2,$p'
        exit 0
    fi
    echo "land: push race (attempt $attempt), re-fetching" >&2
    git fetch -q origin main
done
echo "land: push failed after 3 attempts" >&2
exit 1
