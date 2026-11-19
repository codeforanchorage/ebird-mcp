#!/bin/bash
# eBird MCP Deployment Script.
#
# Builds the Lambda deployment package and applies the Terraform stack
# in terraform/aws/.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

ENVIRONMENT=""
TF_WORKSPACE=""
ALLOW_DIRTY=0

show_usage() {
    echo "Usage: $0 --environment <staging|prod> [--tfworkspace <name>] [--allow-dirty]"
    echo ""
    echo "Options:"
    echo "  --environment, -e   Deployment environment: staging or prod (required)"
    echo "  --tfworkspace, -w   Terraform workspace (default: ebird-mcp-staging|ebird-mcp-prod)"
    echo "  --allow-dirty       Package the working tree even with uncommitted changes"
    echo "                      (emergencies only; the build is stamped as dirty)"
    echo "  --help, -h          Show this help"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
        --tfworkspace|-w) TF_WORKSPACE="$2"; shift 2 ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        --help|-h) show_usage; exit 0 ;;
        *) echo -e "${RED}Unknown argument '${1}'${NC}"; show_usage; exit 1 ;;
    esac
done

if [ -z "$ENVIRONMENT" ]; then
    echo -e "${RED}--environment is required${NC}"; show_usage; exit 1
fi
if [ "$ENVIRONMENT" != "staging" ] && [ "$ENVIRONMENT" != "prod" ]; then
    echo -e "${RED}Invalid environment '${ENVIRONMENT}'. Must be 'staging' or 'prod'.${NC}"
    exit 1
fi
if [ -z "$TF_WORKSPACE" ]; then
    TF_WORKSPACE="ebird-mcp-${ENVIRONMENT}"
fi

echo -e "${GREEN}eBird MCP Deployment [${ENVIRONMENT}] (workspace: ${TF_WORKSPACE})${NC}"
echo "================================"
echo ""

if [ ! -f "config.yaml" ]; then
    echo -e "${RED}config.yaml not found${NC}"
    echo "Run: cp config-example.yaml config.yaml"
    echo "Then edit config.yaml and set plugins.ebird.api_key to your eBird key."
    exit 1
fi

if command -v python3 &> /dev/null; then PYTHON=python3
elif command -v python &> /dev/null; then PYTHON=python
else echo -e "${RED}python not found${NC}"; exit 1; fi

if ! command -v terraform &> /dev/null; then
    echo -e "${RED}terraform not found - https://www.terraform.io/downloads${NC}"; exit 1
fi

# The package is built from the files on disk, so what is on disk must be
# a commit: otherwise an uncommitted edit (possibly another session's,
# half finished) goes to AWS and the live code matches nothing in git.
# Untracked files count; ignored files (config.yaml, the taxonomy bundle)
# do not.
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=$(git status --porcelain 2>/dev/null || true)
if [ -n "$GIT_DIRTY" ]; then
    if [ "$ALLOW_DIRTY" -ne 1 ]; then
        echo -e "${RED}The working tree has uncommitted changes:${NC}"
        echo "$GIT_DIRTY" | head -20
        echo ""
        echo "Commit or stash them, then deploy. To package the tree as it is"
        echo "(emergencies only), re-run with --allow-dirty."
        exit 1
    fi
    echo -e "${YELLOW}--allow-dirty: packaging uncommitted changes${NC}"
fi

echo -e "${YELLOW}Step 1: Validating configuration...${NC}"

$PYTHON - <<'EOF'
import sys, yaml
with open("config.yaml") as f:
    config = yaml.safe_load(f) or {}
plugins = config.get("plugins", {})
enabled = [n for n, c in plugins.items() if isinstance(c, dict) and c.get("enabled")]
if len(enabled) != 1:
    print(f"Expected exactly 1 enabled plugin, found {len(enabled)}: {enabled}", file=sys.stderr)
    sys.exit(1)
ebird = plugins.get("ebird", {})
if ebird.get("enabled") and (not ebird.get("api_key") or ebird["api_key"] == "REPLACE_ME"):
    print("plugins.ebird.api_key is missing or still set to REPLACE_ME", file=sys.stderr)
    print("Get a free key at https://ebird.org/api/keygen", file=sys.stderr)
    sys.exit(1)
print(f"Configuration valid: '{enabled[0]}' plugin enabled")
EOF

if [ $? -ne 0 ]; then exit 1; fi
echo ""

echo -e "${YELLOW}Step 1.5: Refreshing bundled eBird taxonomy...${NC}"
# Best-effort: a failure here doesn't block the deploy. The plugin falls
# through to the live eBird API when the bundle is missing — slower on
# quota but still correct.
if $PYTHON scripts/refresh_taxonomy.py; then
    echo -e "${GREEN}Taxonomy bundle refreshed${NC}"
else
    echo -e "${YELLOW}Taxonomy refresh failed; deploying with whatever taxonomy.json is on disk (plugin will fall back to live API if missing)${NC}"
fi
echo ""

echo -e "${YELLOW}Step 2: Packaging Lambda code...${NC}"
PACKAGE_DIR=".deploy"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# core/ and server/ are NOT copied: they come from the opencontext-core
# requirement, which the install step below places next to plugins/.
cp -r plugins "$PACKAGE_DIR/"
mkdir -p "$PACKAGE_DIR/custom_plugins"
cp requirements.txt "$PACKAGE_DIR/" 2>/dev/null || true

echo "Installing Python dependencies for Lambda target..."
# With uv, install the exact versions in uv.lock -- the ones CI runs the
# tests against (.github/workflows/tests.yml syncs from the same lock).
# requirements.txt only gives lower bounds (httpx>=0.27.0), so installing
# from it resolved afresh on every deploy and shipped library versions
# that nothing had tested. To take newer versions: `uv lock --upgrade`,
# run the tests, commit uv.lock, then deploy.
# uv may be on PATH or only installed as a Python module.
UV=""
if command -v uv &> /dev/null; then
    UV="uv"
elif "$PYTHON" -m uv --version &> /dev/null; then
    UV="$PYTHON -m uv"
fi

if [ -n "$UV" ]; then
    echo "Using uv to install the locked dependencies..."
    LOCKED_REQS="$PACKAGE_DIR/.requirements-locked.txt"
    # --locked fails if uv.lock is out of date with pyproject.toml.
    if ! $UV export --locked --no-dev --no-emit-project --no-hashes \
        --output-file "$LOCKED_REQS" > /dev/null; then
        echo -e "${RED}uv.lock is missing or out of date with pyproject.toml${NC}"
        echo "Run 'uv lock', run the tests, commit uv.lock, then deploy."
        exit 1
    fi
    if ! $UV pip install -r "$LOCKED_REQS" \
        --no-deps \
        --target "$PACKAGE_DIR/" \
        --python-platform x86_64-manylinux2014 \
        --python-version 3.14 \
        --no-compile; then
        echo -e "${RED}uv install failed${NC}"; exit 1
    fi
    rm -f "$LOCKED_REQS"
else
    echo -e "${YELLOW}uv not found: falling back to pip and requirements.txt.${NC}"
    echo -e "${YELLOW}Versions are NOT taken from uv.lock and may differ from CI.${NC}"
    # opencontext-core comes from a git tag, so pip has to BUILD it,
    # and pip refuses to build anything while --platform/
    # --python-version are set. It is pure Python: install it in a
    # second step with no platform flags and --no-deps (its own
    # dependencies are in requirements.txt and arrive with this one).
    BINARY_REQS="$PACKAGE_DIR/.requirements-binary.txt"
    CORE_REQ=$(grep -E '^opencontext-core[ @]' requirements.txt | tr -d '\r' || true)
    grep -vE '^opencontext-core[ @]' requirements.txt > "$BINARY_REQS"
    if ! pip install -r "$BINARY_REQS" -t "$PACKAGE_DIR/" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.14 \
        --only-binary :all: \
        --no-compile; then
        echo -e "${RED}pip install failed (ensure pip>=23.0)${NC}"; exit 1
    fi
    rm -f "$BINARY_REQS"
    if [ -n "$CORE_REQ" ]; then
        if ! pip install "$CORE_REQ" -t "$PACKAGE_DIR/" --no-deps --no-compile; then
            echo -e "${RED}opencontext-core install failed${NC}"; exit 1
        fi
    fi
fi

# The handler string is server.adapters.aws_lambda.lambda_handler, so a
# package without these files is a Lambda that cannot start.
for required in core/mcp_server.py server/adapters/aws_lambda.py; do
    if [ ! -f "$PACKAGE_DIR/$required" ]; then
        echo -e "${RED}$required is missing from the package. Is opencontext-core in requirements.txt?${NC}"
        exit 1
    fi
done

# Record what this package was built from, so "what is live?" can be
# answered from the zip: the commit, whether the tree was dirty, and
# whether the versions came from uv.lock.
BUILD_LOCKED=false
[ -n "$UV" ] && BUILD_LOCKED=true
BUILD_DIRTY=false
[ -n "$GIT_DIRTY" ] && BUILD_DIRTY=true
printf '{"commit": "%s", "dirty": %s, "locked": %s}\n' \
    "$GIT_COMMIT" "$BUILD_DIRTY" "$BUILD_LOCKED" > "$PACKAGE_DIR/BUILD_INFO.json"
echo "Build: commit ${GIT_COMMIT:0:7} dirty=${BUILD_DIRTY} locked=${BUILD_LOCKED}"

ZIP_FILE="lambda-deployment.zip"
rm -f "$ZIP_FILE"
if command -v zip &> /dev/null; then
    (cd "$PACKAGE_DIR" && zip -rq "../$ZIP_FILE" .)
else
    # Python zipfile - works everywhere Python is, no Windows long-path issues.
    $PYTHON - <<EOF
import os, zipfile
src = r"$PACKAGE_DIR"
dst = r"$ZIP_FILE"
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(src):
        for name in files:
            full = os.path.join(root, name)
            arcname = os.path.relpath(full, src)
            zf.write(full, arcname)
EOF
fi

echo -e "${GREEN}Lambda package created: $ZIP_FILE${NC}"
echo ""

echo -e "${YELLOW}Step 3: Deploying with Terraform...${NC}"
cp "$ZIP_FILE" terraform/aws/lambda-deployment.zip
cp config.yaml terraform/aws/config.yaml

if [ ! -d "terraform/aws/.terraform" ]; then
    echo "Initializing Terraform..."
    (cd terraform/aws && terraform init)
fi

cd terraform/aws

echo "Selecting Terraform workspace: ${TF_WORKSPACE}"
terraform workspace select "$TF_WORKSPACE" 2>/dev/null || terraform workspace new "$TF_WORKSPACE"

AWS_REGION=$($PYTHON -c "import yaml; print(yaml.safe_load(open('config.yaml')).get('aws', {}).get('region', 'us-west-2'))")

echo -e "${YELLOW}Planning Terraform changes...${NC}"
if ! terraform plan \
    -out=tfplan \
    -var-file="${ENVIRONMENT}.tfvars" \
    -var="aws_region=$AWS_REGION" \
    -var="config_file=config.yaml"; then
    echo -e "${RED}Terraform plan failed${NC}"; exit 1
fi

echo ""
echo -e "${YELLOW}Apply the planned changes to AWS?${NC}"
echo "   Environment: ${ENVIRONMENT}"
echo "   Workspace:   ${TF_WORKSPACE}"
echo "   Region:      ${AWS_REGION}"
echo ""
read -r -p "Proceed? (yes/no): " CONFIRM
if [ "$CONFIRM" != "yes" ] && [ "$CONFIRM" != "y" ]; then
    echo -e "${YELLOW}Cancelled.${NC}"
    rm -f tfplan
    exit 0
fi

echo -e "${YELLOW}Applying...${NC}"
terraform apply tfplan
rm -f tfplan

API_GATEWAY_URL=$(terraform output -raw api_gateway_url 2>/dev/null || echo "")
cd ../..

echo ""
echo -e "${GREEN}Deployment complete!${NC}"
echo ""
echo "MCP endpoint URL:"
echo -e "${GREEN}$API_GATEWAY_URL${NC}"
echo ""
echo "Connect via Claude Connectors:"
echo "  1. Settings -> Connectors -> Add custom connector"
echo "  2. Name: eBird MCP   URL: $API_GATEWAY_URL"
