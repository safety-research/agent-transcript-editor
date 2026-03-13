# Multi-stage build: frontend + backend

# Stage 1: Build frontend
FROM node:22-slim AS frontend-build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --silent
COPY src/ src/
COPY public/ public/
COPY index.html tsconfig*.json vite.config.ts eslint.config.js ./
RUN npm run build

# Stage 2: Backend + serve frontend
FROM python:3.12-slim
WORKDIR /app

# Install Python deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend
COPY backend/ backend/
COPY SOUL.md EXECUTOR.md ./

# Copy built frontend
COPY --from=frontend-build /app/dist/ dist/

# Create transcripts directory
RUN mkdir -p transcripts/default

EXPOSE 8000

# Run backend (serves API; frontend served by reverse proxy or separately)
CMD ["python", "backend/main.py"]
