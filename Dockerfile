# Dockerfile for Postgres + pgvector
FROM postgres:16

# Install build tools and dependencies
RUN apt-get update && \
    apt-get install -y postgresql-server-dev-16 build-essential git && \
    rm -rf /var/lib/apt/lists/*

# Build and install pgvector
RUN git clone https://github.com/pgvector/pgvector.git /tmp/pgvector && \
    cd /tmp/pgvector && \
    make && make install && \
    rm -rf /tmp/pgvector
