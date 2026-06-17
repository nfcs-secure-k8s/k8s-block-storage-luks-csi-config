# python:3.13.13-slim-trixie
FROM python@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286 AS builder

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    cryptsetup \
    e2fsprogs \
    xfsprogs \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.19
ENV UV_PYTHON_DOWNLOADS=0
ENV UV_COMPILE_BYTECODE=1
RUN uv sync --frozen --no-dev --no-install-project --no-editable --no-cache

COPY proto/ proto/
COPY generate_proto.sh .
COPY luks_csi_driver/ luks_csi_driver/

RUN uv run bash generate_proto.sh
RUN uv sync --frozen --no-dev --no-editable --no-cache

RUN rm -rf /usr/local/lib/python3.13/{idlelib,turtledemo,ensurepip} \
    && find /usr/local -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local -name '*.pyc' -delete \
    && find /app/.venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /app/.venv -name '*.pyc' -delete

FROM gcr.io/distroless/cc-debian13@sha256:58d6ed71fe4166ab62568b10ae5850a81f8df314cfa5aef1c45bf67bd8cf0e1e

WORKDIR /app

COPY --from=builder /usr/local /usr/local
COPY --from=builder /usr/lib /usr/lib
COPY --from=builder /usr/sbin/cryptsetup /usr/sbin/blkid /usr/sbin/blockdev /usr/sbin/dmsetup /usr/sbin/
COPY --from=builder /usr/bin/mount /usr/bin/umount /usr/bin/mountpoint /usr/sbin/
COPY --from=builder /usr/sbin/mkfs.ext4 /usr/sbin/mkfs.xfs /usr/sbin/

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
ENV CSI_ENDPOINT=/csi/csi.sock
ENV CSI_MODE=all

ENTRYPOINT ["luks_csi_driver"]
