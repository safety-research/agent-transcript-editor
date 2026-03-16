#!/bin/bash
set -e

cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}       Agent Transcript Editor${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Check for .env file
if [ ! -f backend/.env ]; then
    echo -e "${YELLOW}⚠  No backend/.env found. Creating from example...${NC}"
    cp backend/.env.example backend/.env
    echo -e "${YELLOW}   Edit backend/.env to add your ANTHROPIC_API_KEY${NC}"
fi

# Check for venv
if [ ! -d backend/venv ]; then
    echo -e "${YELLOW}⚠  No venv found. Creating...${NC}"
    python3 -m venv backend/venv
    echo -e "${GREEN}✓  Created venv${NC}"

    echo -e "${BLUE}►  Installing Python dependencies...${NC}"
    backend/venv/bin/pip install -q -r backend/requirements.txt
    echo -e "${GREEN}✓  Installed Python dependencies${NC}"
fi

# Install npm dependencies (always run to ensure devDependencies like vite are present)
if [ ! -d node_modules ]; then
    echo -e "${BLUE}►  Installing npm dependencies...${NC}"
else
    echo -e "${BLUE}►  Checking npm dependencies...${NC}"
fi
npm install || { echo -e "${RED}✗  npm install failed${NC}"; exit 1; }
echo -e "${GREEN}✓  npm dependencies ready${NC}"

echo ""
echo -e "${GREEN}►  Starting backend on http://localhost:8000${NC}"
echo -e "${GREEN}►  Starting frontend on http://localhost:5173${NC}"
echo ""
echo -e "${YELLOW}   Press Ctrl+C to stop both servers${NC}"
echo ""

# Trap to kill both processes on exit
trap 'kill 0' EXIT

# Start backend
backend/venv/bin/python backend/main.py &

# Start frontend
npm run dev &

# Wait for both
wait
