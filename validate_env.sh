#!/bin/bash

##############################################################################
# Environment Validation Script for ground_truth.py
#
# Verifies Python version, dependencies, environment variables, network
# connectivity, and local system readiness for production deployment.
#
# Usage: ./validate_env.sh
#        ./validate_env.sh --strict (fail on warnings)
#        ./validate_env.sh --verbose (detailed output)
##############################################################################

set -u  # fail on undefined variables

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Color output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'  # No Color

# Options
STRICT=false
VERBOSE=false

if [[ "$@" == *"--strict"* ]]; then STRICT=true; fi
if [[ "$@" == *"--verbose"* ]]; then VERBOSE=true; fi

# Tracking
WARNINGS=0
ERRORS=0
CHECKS_PASSED=0

# Helper functions
log_info() {
    echo -e "${GREEN}✓${NC} $1"
    ((CHECKS_PASSED++))
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    ((WARNINGS++))
}

log_error() {
    echo -e "${RED}✗${NC} $1"
    ((ERRORS++))
}

log_verbose() {
    if [[ "$VERBOSE" == true ]]; then
        echo "  ℹ $1"
    fi
}

##############################################################################
# Section 1: Python Version
##############################################################################
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking Python version..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if ! command -v python3 &> /dev/null; then
    log_error "python3 not found in PATH"
else
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    log_verbose "Found Python: $(which python3)"
    log_verbose "Version: $PYTHON_VERSION"
    
    # Check minimum version (3.8)
    MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    
    if [[ $MAJOR -lt 3 ]] || [[ $MAJOR -eq 3 && $MINOR -lt 8 ]]; then
        log_error "Python 3.8+ required; found $PYTHON_VERSION"
    else
        log_info "Python $PYTHON_VERSION"
    fi
fi

##############################################################################
# Section 2: Required Dependencies
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking dependencies..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REQUIRED_PACKAGES=(
    "paho.mqtt"
    "pyotp"
    "sqlite3"
    "asyncio"
    "logging"
    "dataclasses"
)

for pkg in "${REQUIRED_PACKAGES[@]}"; do
    if python3 -c "import ${pkg}" 2>/dev/null; then
        log_info "Package '$pkg' installed"
    else
        log_error "Package '$pkg' not found; install with: pip install -r requirements.txt"
    fi
done

# Check requirements.txt exists
if [[ ! -f "requirements.txt" ]]; then
    log_warn "requirements.txt not found in $(pwd)"
else
    log_info "requirements.txt present"
    log_verbose "Contents: $(head -5 requirements.txt | tr '\n' ' ')..."
fi

##############################################################################
# Section 3: Environment Variables
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking environment variables..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Load .env if present
if [[ -f ".env" ]]; then
    log_info ".env file found"
    set -a
    source .env
    set +a
else
    log_warn ".env file not found; using only environment variables"
fi

# Required environment variables
REQUIRED_VARS=(
    "MQTT_BROKER_HOST"
    "MQTT_BROKER_PORT"
    "NOVA_TOKEN"
    "NOVA_TOTP_SEED"
    "NOVA_BASE_URL"
    "GROUND_TRUTH_DB_PATH"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var:-}" ]]; then
        log_error "$var not set"
    else
        value="${!var}"
        # Mask sensitive values
        if [[ "$var" == *"TOKEN"* ]] || [[ "$var" == *"SECRET"* ]] || [[ "$var" == *"PASSWORD"* ]]; then
            display_value="***MASKED***"
        else
            display_value="$value"
        fi
        log_info "$var = $display_value"
    fi
done

##############################################################################
# Section 4: Network Connectivity
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking network connectivity..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# MQTT Broker
if [[ -n "${MQTT_BROKER_HOST:-}" ]]; then
    log_verbose "Testing MQTT broker at $MQTT_BROKER_HOST:${MQTT_BROKER_PORT:-1883}"
    if timeout 3 nc -zv "${MQTT_BROKER_HOST}" "${MQTT_BROKER_PORT:-1883}" &>/dev/null; then
        log_info "MQTT broker reachable at $MQTT_BROKER_HOST:${MQTT_BROKER_PORT:-1883}"
    else
        log_error "MQTT broker unreachable at $MQTT_BROKER_HOST:${MQTT_BROKER_PORT:-1883}"
    fi
else
    log_warn "MQTT_BROKER_HOST not set; skipping MQTT connectivity check"
fi

# Nova Backend
if [[ -n "${NOVA_BASE_URL:-}" ]]; then
    log_verbose "Testing Nova at $NOVA_BASE_URL"
    NOVA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 "${NOVA_BASE_URL}/health" 2>/dev/null || echo "000")
    
    if [[ "$NOVA_STATUS" == "200" ]] || [[ "$NOVA_STATUS" == "401" ]]; then
        log_info "Nova reachable at $NOVA_BASE_URL (HTTP $NOVA_STATUS)"
    else
        log_error "Nova unreachable at $NOVA_BASE_URL (HTTP $NOVA_STATUS)"
    fi
else
    log_warn "NOVA_BASE_URL not set; skipping Nova connectivity check"
fi

# DNS Resolution
if ! command -v host &> /dev/null && ! command -v dig &> /dev/null && ! command -v nslookup &> /dev/null; then
    log_warn "DNS tools not found; cannot verify DNS resolution"
else
    log_info "DNS resolution tools available"
fi

##############################################################################
# Section 5: Database Accessibility
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking database..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [[ -z "${GROUND_TRUTH_DB_PATH:-}" ]]; then
    GROUND_TRUTH_DB_PATH="./ground_truth.db"
    log_warn "GROUND_TRUTH_DB_PATH not set; using default: $GROUND_TRUTH_DB_PATH"
fi

DB_DIR=$(dirname "$GROUND_TRUTH_DB_PATH")
if [[ ! -d "$DB_DIR" ]]; then
    log_error "Database directory does not exist: $DB_DIR"
elif [[ ! -w "$DB_DIR" ]]; then
    log_error "Database directory is not writable: $DB_DIR"
else
    log_info "Database directory writable: $DB_DIR"
fi

if [[ -f "$GROUND_TRUTH_DB_PATH" ]]; then
    if [[ -r "$GROUND_TRUTH_DB_PATH" ]] && [[ -w "$GROUND_TRUTH_DB_PATH" ]]; then
        DB_SIZE=$(stat -f%z "$GROUND_TRUTH_DB_PATH" 2>/dev/null || stat -c%s "$GROUND_TRUTH_DB_PATH" 2>/dev/null || echo "unknown")
        log_info "Database exists and is readable/writable ($DB_SIZE bytes)"
        
        # Check database integrity
        if sqlite3 "$GROUND_TRUTH_DB_PATH" "PRAGMA integrity_check;" 2>/dev/null | grep -q "ok"; then
            log_info "Database integrity check passed"
        else
            log_error "Database integrity check failed; database may be corrupted"
        fi
    else
        log_error "Database exists but is not readable/writable: $GROUND_TRUTH_DB_PATH"
    fi
else
    log_warn "Database does not exist yet: $GROUND_TRUTH_DB_PATH (will be created on startup)"
fi

##############################################################################
# Section 6: Cache Directory
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking cache directory..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

CACHE_DIR="${CACHE_DIR:-./.cache}"
if [[ ! -d "$CACHE_DIR" ]]; then
    log_warn "Cache directory does not exist: $CACHE_DIR (will create on startup)"
    mkdir -p "$CACHE_DIR" 2>/dev/null && log_info "Cache directory created: $CACHE_DIR" || \
        log_error "Cannot create cache directory: $CACHE_DIR"
elif [[ ! -w "$CACHE_DIR" ]]; then
    log_error "Cache directory is not writable: $CACHE_DIR"
else
    CACHE_SIZE=$(du -sh "$CACHE_DIR" 2>/dev/null | awk '{print $1}' || echo "unknown")
    log_info "Cache directory exists and is writable (size: $CACHE_SIZE)"
fi

##############################################################################
# Section 7: File Permissions
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking file permissions..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

REQUIRED_FILES=(
    "ground_truth.py"
    "requirements.txt"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        log_error "Required file not found: $file"
    elif [[ ! -r "$file" ]]; then
        log_error "Required file not readable: $file"
    else
        log_info "File accessible: $file"
    fi
done

##############################################################################
# Section 8: Disk Space
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Checking disk space..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

CURRENT_DIR=$(pwd)
DISK_USAGE=$(df -h "$CURRENT_DIR" 2>/dev/null | tail -1 | awk '{print $5}' || echo "unknown")
DISK_AVAILABLE=$(df -h "$CURRENT_DIR" 2>/dev/null | tail -1 | awk '{print $4}' || echo "unknown")

if [[ "$DISK_USAGE" != "unknown" ]]; then
    USAGE_PCT=${DISK_USAGE%\%}
    if (( USAGE_PCT > 90 )); then
        log_error "Disk usage critical: $DISK_USAGE ($DISK_AVAILABLE available)"
    elif (( USAGE_PCT > 80 )); then
        log_warn "Disk usage high: $DISK_USAGE ($DISK_AVAILABLE available)"
    else
        log_info "Disk usage normal: $DISK_USAGE ($DISK_AVAILABLE available)"
    fi
fi

##############################################################################
# Summary
##############################################################################
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo "Checks passed: ${GREEN}$CHECKS_PASSED${NC}"
echo "Warnings: ${YELLOW}$WARNINGS${NC}"
echo "Errors: ${RED}$ERRORS${NC}"

if [[ $ERRORS -gt 0 ]]; then
    echo ""
    echo -e "${RED}RESULT: VALIDATION FAILED${NC}"
    echo "Fix the errors above and re-run validation."
    exit 1
elif [[ $WARNINGS -gt 0 ]] && [[ "$STRICT" == true ]]; then
    echo ""
    echo -e "${YELLOW}RESULT: VALIDATION FAILED (strict mode)${NC}"
    echo "Fix the warnings above and re-run validation."
    exit 1
else
    echo ""
    echo -e "${GREEN}RESULT: VALIDATION PASSED${NC}"
    echo "Environment is ready for ground_truth.py deployment."
    exit 0
fi
