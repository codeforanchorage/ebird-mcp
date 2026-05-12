#!/bin/bash
# Bootstrap S3 + DynamoDB backend for Terraform remote state.

set -euo pipefail

echo "Setting up S3 backend for Terraform..."

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-west-2")

if [ -z "$AWS_ACCOUNT_ID" ]; then
    echo "Could not get AWS account ID. Run 'aws configure' first."
    exit 1
fi

BUCKET_NAME="ebird-mcp-tfstate-${AWS_ACCOUNT_ID}-${AWS_REGION}"
TABLE_NAME="terraform-state-lock"

echo "AWS Account: $AWS_ACCOUNT_ID"
echo "AWS Region:  $AWS_REGION"
echo "S3 Bucket:   $BUCKET_NAME"
echo "DDB Table:   $TABLE_NAME"
echo ""

if ! aws s3api head-bucket --bucket "$BUCKET_NAME" 2>/dev/null; then
    if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$AWS_REGION"
    else
        aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$AWS_REGION" \
            --create-bucket-configuration LocationConstraint="$AWS_REGION"
    fi
    aws s3api put-bucket-versioning --bucket "$BUCKET_NAME" \
        --versioning-configuration Status=Enabled
    aws s3api put-bucket-encryption --bucket "$BUCKET_NAME" \
        --server-side-encryption-configuration \
        '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    echo "S3 bucket created."
else
    echo "S3 bucket already exists."
fi

if ! aws dynamodb describe-table --table-name "$TABLE_NAME" --region "$AWS_REGION" 2>/dev/null; then
    aws dynamodb create-table \
        --table-name "$TABLE_NAME" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "$AWS_REGION"
    aws dynamodb wait table-exists --table-name "$TABLE_NAME" --region "$AWS_REGION"
    echo "DynamoDB lock table created."
else
    echo "DynamoDB table already exists."
fi

BACKEND_TF_PATH="terraform/aws/backend.tf"
cat > "$BACKEND_TF_PATH" <<EOF
terraform {
  backend "s3" {
    bucket         = "$BUCKET_NAME"
    key            = "terraform.tfstate"
    region         = "$AWS_REGION"
    dynamodb_table = "$TABLE_NAME"
    encrypt        = true
  }
}
EOF

echo ""
echo "Wrote $BACKEND_TF_PATH"
echo ""
echo "Next:"
echo "  cd terraform/aws"
echo "  terraform init -migrate-state    # only if you already had local state"
echo ""
