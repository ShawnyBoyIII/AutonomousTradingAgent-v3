#!/bin/bash
# Security Hardening Script for Trading Bot
# Run this to apply recommended security fixes

set -e

echo "=========================================="
echo "Trading Bot Security Hardening"
echo "=========================================="
echo ""

cd /Users/shawndlima/Documents/AutonomousTradingAgentcopy

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "Step 1: Checking current state..."
echo "----------------------------------------"

# Check if state directory exists
if [ -d "state" ]; then
    echo -e "${GREEN}✓${NC} State directory exists"
    
    # Fix database permissions
    for db in state/*.db; do
        if [ -f "$db" ]; then
            current_perms=$(stat -f "%Lp" "$db" 2>/dev/null || stat -c "%a" "$db" 2>/dev/null)
            if [ "$current_perms" != "600" ]; then
                echo -e "${YELLOW}⚠${NC}  Fixing permissions on $db (was $current_perms)"
                chmod 600 "$db"
            else
                echo -e "${GREEN}✓${NC} $db already has correct permissions (600)"
            fi
        fi
    done
    
    # Fix token file permissions
    if [ -f "state/robinhood_tokens.json" ]; then
        current_perms=$(stat -f "%Lp" "state/robinhood_tokens.json" 2>/dev/null || stat -c "%a" "state/robinhood_tokens.json" 2>/dev/null)
        if [ "$current_perms" != "600" ]; then
            echo -e "${YELLOW}⚠${NC}  Fixing permissions on state/robinhood_tokens.json (was $current_perms)"
            chmod 600 "state/robinhood_tokens.json"
        else
            echo -e "${GREEN}✓${NC} Token file already has correct permissions (600)"
        fi
    fi
else
    echo -e "${YELLOW}⚠${NC}  State directory doesn't exist yet (will be created on first run)"
fi

echo ""
echo "Step 2: Checking log directory..."
echo "----------------------------------------"

if [ -d "logs" ]; then
    echo -e "${GREEN}✓${NC} Log directory exists"
    
    # Ensure logs are not world-readable if they contain PII
    for log in logs/**/*.jsonl; do
        if [ -f "$log" ]; then
            current_perms=$(stat -f "%Lp" "$log" 2>/dev/null || stat -c "%a" "$log" 2>/dev/null)
            if [ "$current_perms" != "600" ]; then
                echo -e "${YELLOW}⚠${NC}  Fixing permissions on $log (was $current_perms)"
                chmod 600 "$log" 2>/dev/null || true
            fi
        fi
    done
else
    echo -e "${YELLOW}⚠${NC}  Log directory doesn't exist yet"
fi

echo ""
echo "Step 3: Checking environment variables..."
echo "----------------------------------------"

# Check for required env vars
if [ -z "$ROBINHOOD_USERNAME" ]; then
    echo -e "${YELLOW}⚠${NC}  ROBINHOOD_USERNAME not set (required for V3)"
else
    echo -e "${GREEN}✓${NC} ROBINHOOD_USERNAME is set"
fi

if [ -z "$ROBINHOOD_PASSWORD" ]; then
    echo -e "${YELLOW}⚠${NC}  ROBINHOOD_PASSWORD not set (required for V3)"
else
    echo -e "${GREEN}✓${NC} ROBINHOOD_PASSWORD is set (hidden)"
fi

if [ -z "$ROBINHOOD_MFA_SECRET" ]; then
    echo -e "${YELLOW}⚠${NC}  ROBINHOOD_MFA_SECRET not set (recommended for V3)"
else
    echo -e "${GREEN}✓${NC} ROBINHOOD_MFA_SECRET is set (hidden)"
fi

echo ""
echo "Step 4: Checking for secrets in code..."
echo "----------------------------------------"

# Simple check for common secret patterns
if grep -r "password.*=.*['\"]" trading_bot/ --include="*.py" | grep -v "test_" | grep -v "__pycache__" | grep -v "loader.py" | grep -v "settings.py" | grep -v ".pyc" > /dev/null 2>&1; then
    echo -e "${RED}✗${NC}  Potential hardcoded password found:"
    grep -r "password.*=.*['\"]" trading_bot/ --include="*.py" | grep -v "test_" | grep -v "__pycache__" | grep -v "loader.py" | grep -v "settings.py"
else
    echo -e "${GREEN}✓${NC} No hardcoded passwords detected in source"
fi

echo ""
echo "Step 5: Checking git configuration..."
echo "----------------------------------------"

# Check if .env is in .gitignore
if grep -q "\.env" .gitignore; then
    echo -e "${GREEN}✓${NC} .env files are in .gitignore"
else
    echo -e "${RED}✗${NC}  WARNING: .env files NOT in .gitignore!"
    echo "   Adding .env to .gitignore..."
    echo ".env" >> .gitignore
    echo ".env.local" >> .gitignore
    echo ".env.*.local" >> .gitignore
    echo -e "${GREEN}✓${NC} Added .env patterns to .gitignore"
fi

# Check for committed secrets
if git log --all --source --full-history -S "password" --pickaxe-regex -p 2>/dev/null | head -20 > /dev/null; then
    echo -e "${YELLOW}⚠${NC}  Git history may contain passwords - run 'git log -p | grep -i password' to verify"
else
    echo -e "${GREEN}✓${NC} No obvious secrets in recent git history"
fi

echo ""
echo "Step 6: Installing security tools..."
echo "----------------------------------------"

# Check for detect-secrets
if command -v detect-secrets &> /dev/null; then
    echo -e "${GREEN}✓${NC} detect-secrets is installed"
else
    echo -e "${YELLOW}⚠${NC}  detect-secrets not installed"
    echo "   Install with: pip install detect-secrets"
    echo "   Then run: detect-secrets scan > .secrets.baseline"
fi

# Check for bandit
if command -v bandit &> /dev/null; then
    echo -e "${GREEN}✓${NC} bandit is installed"
else
    echo -e "${YELLOW}⚠${NC}  bandit not installed"
    echo "   Install with: pip install bandit"
    echo "   Then run: bandit -r trading_bot/"
fi

echo ""
echo "=========================================="
echo "Security Hardening Summary"
echo "=========================================="
echo ""
echo "Fixed:"
echo "  - Database file permissions (600)"
echo "  - Token file permissions (600)"
echo "  - Gitignore for .env files"
echo ""
echo "Still needed:"
echo "  - Token encryption (add TOKEN_ENCRYPTION_KEY to .env)"
echo "  - Dashboard authentication"
echo "  - SQLCipher for database encryption"
echo ""
echo "Run 'detect-secrets scan' to check for committed secrets"
echo "Run 'bandit -r trading_bot/' for Python security scan"
echo ""
echo "See docs/SECURITY_REVIEW.md for full details"
echo "=========================================="
