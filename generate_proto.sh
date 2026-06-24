#!/usr/bin/env bash
# Generate Python gRPC stubs from csi.proto
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="${SCRIPT_DIR}/luks_csi_driver/generated"
mkdir -p "${OUTDIR}"

python -m grpc_tools.protoc \
  --proto_path="${SCRIPT_DIR}/proto" \
  --python_out="${OUTDIR}" \
  --grpc_python_out="${OUTDIR}" \
  "${SCRIPT_DIR}/proto/csi.proto"

# grpc_tools emits a bare 'import csi_pb2' in the grpc file.
# Patch it to use the package-relative import so it works when
# the generated/ directory is a package (has __init__.py).
python - <<'EOF'
import re, pathlib
grpc_file = pathlib.Path("luks_csi_driver/generated/csi_pb2_grpc.py")
content = grpc_file.read_text()
content = re.sub(
    r"^import csi_pb2\b",
    "from luks_csi_driver.generated import csi_pb2",
    content,
    flags=re.MULTILINE,
)
grpc_file.write_text(content)
print("Patched import in csi_pb2_grpc.py")
EOF

touch "${OUTDIR}/__init__.py"
echo "Proto generation complete. Files in ${OUTDIR}/"
