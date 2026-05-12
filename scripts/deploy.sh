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

show_usage() {
    echo "Usage: $0 --environment <staging|prod> [--tfworkspace <name>]"
    echo ""
    echo "Options:"
    echo "  --environment, -e   Deployment environment: staging or prod (required)"
    echo "  --tfworkspace, -w   Terraform workspace (default: ebird-mcp-staging|ebird-mcp-prod)"
    echo "  --help, -h          Show this help"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment|-e) ENVIRONMENT="$2"; shift 2 ;;
        --tfworkspace|-w) TF_WORKSPACE="$2"; shift 2 ;;
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

echo -e "${YELLOW}Step 2: Packaging Lambda code...${NC}"
PACKAGE_DIR=".deploy"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

cp -r core "$PACKAGE_DIR/"
cp -r plugins "$PACKAGE_DIR/"
cp -r server "$PACKAGE_DIR/"
mkdir -p "$PACKAGE_DIR/custom_plugins"
cp requirements.txt "$PACKAGE_DIR/" 2>/dev/null || true

echo "Installing Python dependencies for Lambda target..."
if command -v uv &> /dev/null; then
    echo "Using uv..."
    if ! uv pip install -r requirements.txt \
        --target "$PACKAGE_DIR/" \
        --python-platform x86_64-manylinux2014 \
        --python-version 3.11 \
        --no-compile; then
        echo -e "${RED}uv install failed${NC}"; exit 1
    fi
else
    echo "uv not found, using pip..."
    if ! pip install -r requirements.txt -t "$PACKAGE_DIR/" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.11 \
        --only-binary :all: \
        --no-compile; then
        echo -e "${RED}pip install failed (ensure pip>=23.0)${NC}"; exit 1
    fi
fi

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
