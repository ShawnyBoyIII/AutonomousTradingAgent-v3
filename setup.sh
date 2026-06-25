#!/bin/bash
# Setup script for Autonomous Trading Agent
# Run this after cloning the repository

set -e

echo "🤖 Autonomous Trading Agent - Setup Script"
echo "=========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check Python version
echo "📋 Checking Python version..."
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
REQUIRED_VERSION="3.11"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then 
    echo -e "${RED}❌ Error: Python 3.11+ required, found $PYTHON_VERSION${NC}"
    exit 1
fi
echo -e "${GREEN}✅ Python $PYTHON_VERSION detected${NC}"

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ]; then
    echo -e "${RED}❌ Error: pyproject.toml not found. Are you in the right directory?${NC}"
    exit 1
fi

# Create virtual environment
echo ""
echo "📦 Creating virtual environment..."
if [ -d ".venv" ]; then
    echo -e "${YELLOW}⚠️  Virtual environment already exists${NC}"
    read -p "Delete and recreate? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf .venv
        python3 -m venv .venv
        echo -e "${GREEN}✅ Virtual environment recreated${NC}"
    fi
else
    python3 -m venv .venv
    echo -e "${GREEN}✅ Virtual environment created${NC}"
fi

# Install dependencies
echo ""
echo "📥 Installing dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
echo -e "${GREEN}✅ Dependencies installed${NC}"

# Create necessary directories
echo ""
echo "📁 Creating directories..."
mkdir -p state logs/burn_in
echo -e "${GREEN}✅ Directories created${NC}"

# Set permissions
echo ""
echo "🔒 Setting permissions..."
chmod 700 state/
chmod +x tradebot-local
chmod +x scripts/*.sh 2>/dev/null || true
echo -e "${GREEN}✅ Permissions set${NC}"

# Copy environment file
echo ""
echo "⚙️  Configuration..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${GREEN}✅ Created .env from example${NC}"
        echo -e "${YELLOW}⚠️  Edit .env to add your credentials (optional for paper trading)${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  .env already exists${NC}"
fi

# Run tests
echo ""
echo "🧪 Running tests..."
if .venv/bin/python -m pytest -q --tb=no 2>/dev/null | grep -q "passed"; then
    TEST_OUTPUT=$(.venv/bin/python -m pytest -q --tb=no 2>/dev/null)
    echo -e "${GREEN}✅ Tests passing: $TEST_OUTPUT${NC}"
else
    echo -e "${YELLOW}⚠️  Some tests may have failed${NC}"
    echo "Run '.venv/bin/python -m pytest -v' to check"
fi

# Verify installation
echo ""
echo "🔍 Verifying installation..."
if ./tradebot-local doctor >/dev/null 2>&1; then
    echo -e "${GREEN}✅ Installation verified${NC}"
else
    echo -e "${YELLOW}⚠️  Doctor check had issues, but installation may still work${NC}"
fi

# Create .gitignore entries
echo ""
echo "📝 Checking .gitignore..."
if ! grep -q "\.env" .gitignore 2>/dev/null; then
    echo "" >> .gitignore
    echo "# Environment variables" >> .gitignore
    echo ".env" >> .gitignore
    echo -e "${GREEN}✅ Added .env to .gitignore${NC}"
fi

echo ""
echo "=========================================="
echo -e "${GREEN}🎉 Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Review config:        nano config.yaml"
echo "  2. Edit environment:     nano .env"
echo "  3. Run health check:     ./tradebot-local doctor"
echo "  4. Start scanning:       ./tradebot-local scan --symbols SPY --why"
echo "  5. Read full guide:      cat GETTING_STARTED.md"
echo ""
echo "For automated trading:"
echo "  ./scripts/auto-burn-in.sh"
echo ""
echo -e "${YELLOW}⚠️  Remember: This is paper trading only - no real money at risk${NC}"
echo ""
