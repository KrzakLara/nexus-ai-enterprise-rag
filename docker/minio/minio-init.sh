#!/bin/sh
# One-shot MinIO initialisation (LOCAL DEVELOPMENT): private bucket and a bucket-scoped access key
# for the application. Idempotent; safe to re-run.
set -eu
i=0
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo "MinIO did not become ready" >&2; exit 1; fi
  sleep 1
done
mc mb --ignore-existing "local/$S3_BUCKET"
mc anonymous set none "local/$S3_BUCKET"          # never public
cat > /tmp/nexus-app-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::$S3_BUCKET"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::$S3_BUCKET/*"]}
  ]
}
EOF
mc admin policy create local nexus-app /tmp/nexus-app-policy.json >/dev/null 2>&1 || true
mc admin user add local "$S3_APP_ACCESS_KEY" "$S3_APP_SECRET_KEY"
mc admin policy attach local nexus-app --user "$S3_APP_ACCESS_KEY" >/dev/null 2>&1 || true
echo "MinIO ready: private bucket $S3_BUCKET, access key $S3_APP_ACCESS_KEY limited to it."
