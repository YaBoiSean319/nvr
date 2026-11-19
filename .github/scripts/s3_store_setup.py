"""Give the CI S3 store (moto's server) an account for the tests: a user with an access key and S3 rights, and the test bucket (#402).

The server is started with INITIAL_NO_AUTH_ACTION_COUNT=3, so the first three requests (the user, its policy and its key) are let through
unsigned, and every request after them must be signed with a key the server knows. The bucket is made with the new key, which proves the
key works before any test runs. Prints the key id and secret, separated by a space."""
from __future__ import annotations

import json
import sys

import boto3


def main(endpoint: str, bucket: str) -> None:
    iam = boto3.client("iam", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="setup", aws_secret_access_key="setup")
    iam.create_user(UserName="nvr-test")
    iam.put_user_policy(UserName="nvr-test", PolicyName="s3", PolicyDocument=json.dumps(
        {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": "*"}]}))
    key = iam.create_access_key(UserName="nvr-test")["AccessKey"]
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id=key["AccessKeyId"], aws_secret_access_key=key["SecretAccessKey"])
    s3.create_bucket(Bucket=bucket)
    print(key["AccessKeyId"], key["SecretAccessKey"])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
