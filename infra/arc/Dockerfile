# Pin both upstream images. Rebuild and re-run the sandbox smoke test on updates.
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS node
FROM ghcr.io/actions/actions-runner@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4

USER root
COPY --from=node /usr/local/ /usr/local/
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap socat gh jq python3-pip python3-venv build-essential \
        curl unzip zip rsync \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/forge-venv \
    && chown -R runner:runner /opt/forge-venv /usr/local/lib/node_modules /usr/local/bin

# A writable venv permits existing pip-based jobs without disabling PEP 668.
ENV PATH="/opt/forge-venv/bin:${PATH}"
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
USER runner
RUN python3 --version && node --version && npm --version && gh --version \
    && git --version && jq --version && bwrap --version && socat -V
