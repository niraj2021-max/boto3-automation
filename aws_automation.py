"""
AWS Resource Automation Tool
-----------------------------
A command-line menu that automates common AWS operations with boto3
instead of clicking through the AWS Console.

  1. Create an S3 bucket
  2. Upload a file to S3
  3. Launch EC2 instance(s)
  4. Stop an EC2 instance
  5. Terminate an EC2 instance (asks for confirmation)
  6. List existing resources (S3 buckets + EC2 instances)
  0. Exit

Requires AWS credentials (run `aws configure`, or set AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY) and a valid region such as us-east-1.
"""

import os
import re
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

DEFAULT_REGION = "us-east-1"
REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")          # us-east-1, ap-south-1, eu-north-1
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")  # S3 naming rules
DEMO_TAG = "boto3-automation-demo"
MAX_INSTANCES = 5                                                 # safety cap per launch

# Created in main() once the region has been validated.
REGION = DEFAULT_REGION
s3_client = ec2_client = ec2_resource = None


# ----------------------------------------------------------------- helpers
def resolve_region():
    """Return (region, note). Rejects values such as 'Global' that are not real regions."""
    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or boto3.session.Session().region_name
    )
    if not region:
        return DEFAULT_REGION, f"No region configured - using {DEFAULT_REGION}."
    if not REGION_PATTERN.match(region):
        print(f"❌ '{region}' is not a valid AWS region.")
        print("   Fix it with:  aws configure set region us-east-1")
        sys.exit(1)
    return region, ""


def get_latest_al2023_ami():
    """Look up the newest Amazon Linux 2023 AMI so no AMI ID is hard-coded."""
    images = ec2_client.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-*-x86_64"]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    if not images:
        raise RuntimeError(f"No Amazon Linux 2023 AMI found in {REGION}.")
    images.sort(key=lambda x: x["CreationDate"], reverse=True)
    return images[0]["ImageId"]


def name_tag(instance):
    return next((t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"), "(no name)")


def get_instance(instance_id):
    """Return the instance description dict, or None (after printing why)."""
    try:
        reservations = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as e:
        print(f"❌ Could not find instance '{instance_id}': {e}")
        return None
    return reservations[0]["Instances"][0] if reservations else None


# --------------------------------------------------------------- menu actions
def create_bucket():
    bucket_name = input("Enter a globally-unique bucket name: ").strip().lower()
    if not BUCKET_PATTERN.match(bucket_name):
        print("❌ Invalid name: use 3-63 lowercase letters, numbers, dots or hyphens.")
        return
    try:
        if REGION == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        print(f"✅ Bucket '{bucket_name}' created in {REGION}.")
    except ClientError as e:
        print(f"❌ Could not create bucket: {e}")


def upload_file():
    bucket_name = input("Bucket name: ").strip()
    # Windows' "Copy as path" wraps the path in quotes - strip them.
    file_path = os.path.expanduser(input("Local file path to upload: ").strip().strip("\"'"))
    if not os.path.isfile(file_path):
        print(f"❌ File not found: {file_path}")
        return
    default_key = re.split(r"[\\/]", file_path)[-1]           # works for C:\... and /home/...
    key = input(f"S3 object key [Enter = {default_key}]: ").strip() or default_key
    try:
        s3_client.upload_file(file_path, bucket_name, key)
        print(f"✅ Uploaded '{file_path}' to s3://{bucket_name}/{key}")
    except (ClientError, OSError) as e:
        print(f"❌ Upload failed: {e}")


def launch_instance():
    instance_type = input("Instance type [t3.micro]: ").strip() or "t3.micro"
    try:
        count = int(input("How many instances? [1]: ").strip() or "1")
    except ValueError:
        print("❌ Please enter a whole number.")
        return
    if not 1 <= count <= MAX_INSTANCES:
        print(f"❌ Choose between 1 and {MAX_INSTANCES} instances.")
        return
    try:
        ami_id = get_latest_al2023_ami()
        instances = ec2_resource.create_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            MinCount=count,
            MaxCount=count,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": DEMO_TAG}]}
            ],
        )
        for inst in instances:
            print(f"✅ Launching instance {inst.id} ({instance_type}, AMI {ami_id})")
    except (ClientError, RuntimeError) as e:
        print(f"❌ Launch failed: {e}")


def stop_instance():
    instance_id = input("Instance ID to stop: ").strip()
    inst = get_instance(instance_id)
    if not inst:
        return
    print(f"   {instance_id} [{name_tag(inst)}] is currently {inst['State']['Name']}")
    try:
        ec2_client.stop_instances(InstanceIds=[instance_id])
        print(f"🟡 Stopping instance {instance_id}...")
    except ClientError as e:
        print(f"❌ Stop failed: {e}")


def terminate_instance():
    instance_id = input("Instance ID to terminate: ").strip()
    inst = get_instance(instance_id)
    if not inst:
        return
    # Show what is about to be destroyed so the wrong ID is easy to spot.
    print(f"   {instance_id} [{name_tag(inst)}] type={inst['InstanceType']} state={inst['State']['Name']}")
    if input("Type 'yes' to confirm termination (cannot be undone): ").strip().lower() != "yes":
        print("Cancelled.")
        return
    try:
        ec2_client.terminate_instances(InstanceIds=[instance_id])
        print(f"🔴 Terminating instance {instance_id}...")
    except ClientError as e:
        print(f"❌ Terminate failed: {e}")


def list_resources():
    print(f"\n--- S3 Buckets (all regions) ---")
    try:
        buckets = s3_client.list_buckets()["Buckets"]
        if not buckets:
            print("  (none)")
        for b in buckets:
            print(f"  - {b['Name']} (created {b['CreationDate']})")
    except ClientError as e:
        print(f"  ❌ Could not list buckets: {e}")

    print(f"\n--- EC2 Instances in {REGION} (excluding terminated) ---")
    try:
        found = False
        for reservation in ec2_client.describe_instances()["Reservations"]:
            for i in reservation["Instances"]:
                if i["State"]["Name"] == "terminated":
                    continue
                found = True
                print(f"  - {i['InstanceId']} [{name_tag(i)}] type={i['InstanceType']} state={i['State']['Name']}")
        if not found:
            print("  (none)")
    except ClientError as e:
        print(f"  ❌ Could not list instances: {e}")
    print()


MENU = """
=== AWS Resource Automation Tool ===
1. Create an S3 bucket
2. Upload a file to S3
3. Launch EC2 instance(s)
4. Stop an EC2 instance
5. Terminate an EC2 instance
6. List existing resources (S3 + EC2)
0. Exit
"""

ACTIONS = {
    "1": create_bucket,
    "2": upload_file,
    "3": launch_instance,
    "4": stop_instance,
    "5": terminate_instance,
    "6": list_resources,
}


def main():
    global REGION, s3_client, ec2_client, ec2_resource

    try:                                   # lets the ✅/❌ symbols print on older Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    REGION, note = resolve_region()
    if note:
        print(note)
    s3_client = boto3.client("s3", region_name=REGION)
    ec2_client = boto3.client("ec2", region_name=REGION)
    ec2_resource = boto3.resource("ec2", region_name=REGION)

    try:
        who = boto3.client("sts", region_name=REGION).get_caller_identity()
    except NoCredentialsError:
        print("❌ No AWS credentials found. Run `aws configure` first.")
        sys.exit(1)
    except (ClientError, BotoCoreError) as e:
        print(f"❌ AWS rejected the credentials or could not be reached: {e}")
        sys.exit(1)
    print(f"Connected as {who['Arn']} in {REGION}")

    while True:
        print(MENU)
        try:
            choice = input("Select an option: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        if choice == "0":
            print("Goodbye!")
            break
        action = ACTIONS.get(choice)
        if not action:
            print("Invalid option, try again.")
            continue
        try:
            action()
        except BotoCoreError as e:         # network / endpoint problems
            print(f"❌ Could not reach AWS: {e}")
        time.sleep(0.3)


if __name__ == "__main__":
    main()
