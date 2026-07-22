#!/usr/bin/env bash
# scripts/audit_gateway_consumers.sh
#
# Pre-flight checklist generator for the LiteLLM gateway hard-cap flip.
#
# When the shared UC gateway enables persistent virtual keys (database_url
# uncommented), any consumer that still posts a blank/invalid key starts
# 401ing. This script greps each known consumer's env/compose for the
# gateway key variables they declare and emits a table:
#
#   service | key present? | key var alias(es) | notes
#
# SCOPE: read-only audit. Does NOT mint keys, edit gateway config, or SSH
# by default. Live fleet scans are operator-driven (see SSH notes below).
#
# Usage:
#   # Against the checked-in mock fixture (CI / no SSH):
#   ./scripts/audit_gateway_consumers.sh --fixture-root tests/fixtures/gateway_consumers
#
#   # Against a local checkout tree of suite repos:
#   ./scripts/audit_gateway_consumers.sh --root /path/to/suite-checkouts
#
#   # Against a custom consumer map (TSV: name<TAB>relative_or_abs_path):
#   ./scripts/audit_gateway_consumers.sh --map /path/to/consumers.tsv --root /srv
#
# Exit codes:
#   0  — all scanned consumers that declare a gateway key var have a non-blank value
#   1  — at least one blank/missing key (or a consumer dir was unreadable)
#   2  — usage / bad flags
#
# SSH access notes (DO NOT embed hosts or credentials in this script):
#   Operators with fleet access typically land on the gateway host and the
#   per-service deploy hosts via their normal Tailscale / SSH config. To
#   scan a remote tree without copying secrets into git:
#
#     ssh <gateway-or-ops-host> 'cd /path/to/deploys && find . -name ".env*" -o -name "docker-compose*.yml"'
#     # Then either:
#     #  (a) rsync the compose/.env.example trees (never live .env) into a local
#     #      --root and re-run this script, or
#     #  (b) run this script ON the host with --root pointed at the deploy tree.
#
#   Prefer scanning .env.example / docker-compose*.yml / *.env.sample so live
#   secret values never enter the audit output. When a live .env must be
#   scanned, this script only reports PRESENT / BLANK / MISSING — never the
#   value itself (and never last-4).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Known consumers of the shared UC LiteLLM gateway (email-ops x2, contact-ops,
# meet, stable, cdr, knowledge-ops, buildlog, brand-ops, AO itself, ...).
# Map is "service_name|relative_subdir_under_--root". Override with --map.
# ---------------------------------------------------------------------------
DEFAULT_CONSUMERS='accounting-ops|accounting-ops
email-ops|email-ops
email-ops-cleaner|email-ops
contact-ops|contact-ops
meet|meet
stable|stable
cdr|cdr
knowledge-ops|knowledge-ops
buildlog|buildlog
brand-ops|brand-ops
blank-key-service|blank-key-service'

# Env / compose key names that mean "this service talks to the gateway with a key".
# Order is display preference for the "alias" column (first match wins as primary).
KEY_VARS=(
  LLM_PLATFORM_VIRTUAL_KEY
  LLM_GATEWAY_ADMIN_KEY
  LITELLM_API_KEY
  LITELLM_MASTER_KEY
  LITELLM_VIRTUAL_KEY
  LITELLM_PROXY_KEY
  OPENROUTER_API_KEY
  UC_LLM_API_KEY
  UC_GATEWAY_KEY
)

# Non-secret companion vars (presence helps confirm gateway routing is intended).
COMPANION_VARS=(
  LLM_GATEWAY_BASE_URL
  LITELLM_BASE_URL
  LITELLM_PROXY_URL
  AO_ROUTE_CHAT_THROUGH_GATEWAY
)

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \?//'
  exit 2
}

ROOT=""
MAP_FILE=""
FIXTURE_ROOT=""
VERBOSE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="${2:-}"; shift 2 ;;
    --fixture-root) FIXTURE_ROOT="${2:-}"; shift 2 ;;
    --map) MAP_FILE="${2:-}"; shift 2 ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown flag: $1" >&2; usage ;;
  esac
done

if [[ -n "$FIXTURE_ROOT" ]]; then
  ROOT="$FIXTURE_ROOT"
fi
if [[ -z "$ROOT" ]]; then
  # Default to the repo's mock fixture so bare runs need no SSH.
  ROOT="${REPO_ROOT}/tests/fixtures/gateway_consumers"
fi
if [[ ! -d "$ROOT" ]]; then
  echo "ERROR: root not a directory: $ROOT" >&2
  exit 2
fi
ROOT="$(cd "$ROOT" && pwd)"

# Load consumer map into parallel arrays.
SVC_NAMES=()
SVC_RELS=()

load_map() {
  local line name rel
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    case "$line" in
      \#*) continue ;;
    esac
    if [[ "$line" == *$'\t'* ]]; then
      name="${line%%$'\t'*}"
      rel="${line#*$'\t'}"
    elif [[ "$line" == *'|'* ]]; then
      name="${line%%|*}"
      rel="${line#*|}"
    else
      name="$line"
      rel="$line"
    fi
    # trim
    name="$(printf '%s' "$name" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    rel="$(printf '%s' "$rel" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [[ -z "$name" ]] && continue
    SVC_NAMES+=("$name")
    SVC_RELS+=("$rel")
  done
}

if [[ -n "$MAP_FILE" ]]; then
  [[ -f "$MAP_FILE" ]] || { echo "ERROR: map not found: $MAP_FILE" >&2; exit 2; }
  load_map < "$MAP_FILE"
else
  load_map <<EOF
$DEFAULT_CONSUMERS
EOF
fi

# ---------------------------------------------------------------------------
# Scan helpers — value is NEVER printed.
# Paths may contain spaces (e.g. "/Volumes/Studio Storage/..."); always quote.
# ---------------------------------------------------------------------------

SCAN_FILES=()

collect_scan_files() {
  local dir="$1"
  local tmp
  SCAN_FILES=()
  tmp="$(mktemp)"
  # One path per line. Paths with newlines are pathological; spaces are fine.
  find "$dir" -maxdepth 4 \( \
      -name 'docker-compose*.yml' -o \
      -name 'docker-compose*.yaml' -o \
      -name '.env' -o \
      -name '.env.*' -o \
      -name '*.env' -o \
      -name '*.env.example' -o \
      -name '*.env.sample' -o \
      -name 'env.example' \
    \) -type f 2>/dev/null | sort > "$tmp" || true
  while IFS= read -r f || [[ -n "$f" ]]; do
    [[ -n "$f" ]] && SCAN_FILES+=("$f")
  done < "$tmp"
  rm -f "$tmp"
}

# Strip quotes + trailing comment from a value; print cleaned value.
_clean_val() {
  local val="$1"
  val="${val%%#*}"
  # trim + strip one layer of matching quotes via sed (avoids bash quote hell)
  printf '%s' "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'$/\\1/"
}

# Classify a cleaned value: present | blank
_classify_val() {
  local val="$1"
  if [[ -z "$val" ]]; then
    echo blank
    return
  fi
  # Runtime-injected compose placeholders count as blank risk for the flip.
  case "$val" in
    \$\{*\})
      case "$val" in
        \$\{*:-*\})
          # val like ${FOO:-bar} -> extract bar (may be empty)
          local def="${val#*:-}"
          def="${def%\}}"
          def="$(_clean_val "$def")"
          if [[ -z "$def" ]]; then
            echo blank
          else
            echo present
          fi
          ;;
        *)
          echo blank
          ;;
      esac
      ;;
    *)
      echo present
      ;;
  esac
}

# stdout: PRESENT | BLANK | ABSENT for $1=VAR across SCAN_FILES.
var_status() {
  local var="$1"
  local f line rest val kind
  local seen_blank=0
  local seen_present=0
  local i
  for ((i = 0; i < ${#SCAN_FILES[@]}; i++)); do
    f="${SCAN_FILES[$i]}"
    [[ -f "$f" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      # strip leading whitespace
      line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//')"
      [[ -z "$line" ]] && continue
      case "$line" in
        \#*) continue ;;
      esac
      case "$line" in
        "${var}="* | "${var}:"*)
          rest="${line#${var}}"
          rest="${rest#=}"
          rest="${rest#:}"
          val="$(_clean_val "$rest")"
          kind="$(_classify_val "$val")"
          if [[ "$kind" == present ]]; then
            seen_present=1
          else
            seen_blank=1
          fi
          ;;
      esac
    done < "$f"
  done
  if [[ $seen_present -eq 1 ]]; then
    echo PRESENT
  elif [[ $seen_blank -eq 1 ]]; then
    echo BLANK
  else
    echo ABSENT
  fi
}

# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

printf '%-22s  %-12s  %-36s  %s\n' "SERVICE" "KEY?" "KEY VAR ALIAS(ES)" "NOTES"
printf '%-22s  %-12s  %-36s  %s\n' "----------------------" "------------" "------------------------------------" "-----"

failures=0
scanned=0

for i in "${!SVC_NAMES[@]}"; do
  name="${SVC_NAMES[$i]}"
  rel="${SVC_RELS[$i]}"
  dir="${ROOT}/${rel}"
  scanned=$((scanned + 1))

  if [[ ! -d "$dir" ]]; then
    printf '%-22s  %-12s  %-36s  %s\n' "$name" "MISSING_DIR" "-" "path not found under root"
    failures=$((failures + 1))
    continue
  fi

  collect_scan_files "$dir"
  if [[ ${#SCAN_FILES[@]} -eq 0 ]]; then
    printf '%-22s  %-12s  %-36s  %s\n' "$name" "NO_FILES" "-" "no compose/env under maxdepth 4"
    failures=$((failures + 1))
    continue
  fi

  if [[ $VERBOSE -eq 1 ]]; then
    echo "# $name files:" >&2
    printf '  %s\n' "${SCAN_FILES[@]}" >&2
  fi

  present_aliases=()
  blank_aliases=()
  for var in "${KEY_VARS[@]}"; do
    st="$(var_status "$var")"
    case "$st" in
      PRESENT) present_aliases+=("$var") ;;
      BLANK) blank_aliases+=("$var") ;;
    esac
  done

  companions=()
  for var in "${COMPANION_VARS[@]}"; do
    st="$(var_status "$var")"
    if [[ "$st" != "ABSENT" ]]; then
      companions+=("${var}=${st}")
    fi
  done

  note="-"
  if [[ ${#companions[@]} -gt 0 ]]; then
    note="companions: ${companions[*]}"
  fi

  if [[ ${#present_aliases[@]} -gt 0 ]]; then
    alias_str="${present_aliases[*]}"
    alias_str="${alias_str// /,}"
    if [[ ${#blank_aliases[@]} -gt 0 ]]; then
      blank_str="${blank_aliases[*]}"
      blank_str="${blank_str// /,}"
      printf '%-22s  %-12s  %-36s  %s\n' \
        "$name" "MIXED" "$alias_str" "BLANK also: ${blank_str}; ${note}"
      failures=$((failures + 1))
    else
      printf '%-22s  %-12s  %-36s  %s\n' "$name" "PRESENT" "$alias_str" "$note"
    fi
  elif [[ ${#blank_aliases[@]} -gt 0 ]]; then
    alias_str="${blank_aliases[*]}"
    alias_str="${alias_str// /,}"
    printf '%-22s  %-12s  %-36s  %s\n' \
      "$name" "BLANK" "$alias_str" "FIX before hard-cap flip; ${note}"
    failures=$((failures + 1))
  else
    printf '%-22s  %-12s  %-36s  %s\n' \
      "$name" "ABSENT" "-" "no gateway key vars declared; ${note}"
    # ABSENT is informational unless companions show gateway intent.
    for c in "${companions[@]+"${companions[@]}"}"; do
      case "$c" in
        *=PRESENT | *=BLANK)
          failures=$((failures + 1))
          break
          ;;
      esac
    done
  fi
done

echo
echo "Scanned ${scanned} consumer(s) under ${ROOT}"
if [[ $failures -gt 0 ]]; then
  echo "RESULT: ${failures} issue(s) — fix BLANK/MIXED/MISSING_DIR before enabling gateway database_url."
  exit 1
fi
echo "RESULT: OK — no blank gateway keys detected in scanned trees."
exit 0
