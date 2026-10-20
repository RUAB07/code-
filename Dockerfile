FROM mcr.microsoft.com/devcontainers/javascript-node:1-20-bookworm

WORKDIR /workspace

RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @nestjs/cli

CMD ["sleep", "infinity"]
