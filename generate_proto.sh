#!/usr/bin/env bash
# Generate Python gRPC stubs from csi.proto
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "${SCRIPT_DIR}/generated"

python -m grpc_tools.protoc \
  --proto_path="${SCRIPT_DIR}/proto" \
  --python_out="${SCRIPT_DIR}/generated" \
  --grpc_python_out="${SCRIPT_DIR}/generated" \
  "${SCRIPT_DIR}/proto/csi.proto"

# grpc_tools emits a bare 'import csi_pb2' in the grpc file.
# Patch it to use the package-relative import so it works when
# the generated/ directory is a package (has __init__.py).
python - <<'EOF'
import re, pathlib
grpc_file = pathlib.Path("generated/csi_pb2_grpc.py")
content = grpc_file.read_text()
content = re.sub(
    r"^import csi_pb2\b",
    "from generated import csi_pb2",
    content,
    flags=re.MULTILINE,
)
grpc_file.write_text(content)
print("Patched import in csi_pb2_grpc.py")
EOF

touch "${SCRIPT_DIR}/generated/__init__.py"
echo "Proto generation complete. Files in ${SCRIPT_DIR}/generated/"
