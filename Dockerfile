# Stage 1: Build the Next.js static frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

# Stage 2: Build the Python backend and serve everything
FROM python:3.11-slim
WORKDIR /app

# Install Node.js v22, git, and globally install gitnexus
RUN apt-get update && apt-get install -y curl git build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g gitnexus@latest tree-sitter-kotlin \
    && rm -rf /var/lib/apt/lists/*

# Copy python backend requirements
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy python backend source
COPY backend/ ./backend/

# Copy built frontend from Stage 1
COPY --from=frontend-builder /app/dashboard/out ./dashboard/out

# Set environment variables for Hugging Face
ENV HOST=0.0.0.0
ENV PORT=7860

# Hugging Face Spaces expose port 7860
EXPOSE 7860

WORKDIR /app/backend

# Run FastAPI backend (which also serves the static Next.js frontend)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
