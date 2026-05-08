FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    cryptsetup \
    e2fsprogs \
    xfsprogs \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ proto/
COPY generate_proto.sh .
RUN bash generate_proto.sh

COPY driver.py controller.py node.py luks.py k8s.py vault.py device.py main.py ./

ENV CSI_ENDPOINT=/csi/csi.sock
ENV CSI_MODE=all

ENTRYPOINT ["python", "main.py"]
