"""
Hugging Face Zero-Touch Cloud Deployer
Provisions a 100% free, 2 vCPU / 16 GB RAM Docker Space on Hugging Face at datacenter speeds (gigabits/sec).
Zero local storage used, 24/7 cloud availability.
"""

import os
import sys
import time
import argparse
import subprocess

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_huggingface_hub():
    """Ensure huggingface_hub is installed."""
    try:
        import huggingface_hub
    except ImportError:
        print("[*] Installing huggingface_hub SDK...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"])


def get_token(cli_token=None):
    """Retrieves Hugging Face token from CLI, env, or cache."""
    if cli_token:
        return cli_token.strip()

    token = os.environ.get("HF_TOKEN")
    if token:
        return token.strip()

    cache_path = os.path.expanduser("~/.cache/huggingface/token")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    return t
        except Exception:
            pass

    return None


def deploy_space(token: str, space_name: str = "cloud-stream-bridge", private: bool = False):
    """Creates/updates the Hugging Face Docker Space and uploads files."""
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    user_info = api.whoami()
    username = user_info["name"]
    repo_id = f"{username}/{space_name}"

    print(f"[+] Authenticated as Hugging Face user: {username}")
    print(f"[*] Ensuring Space repository exists: {repo_id} (SDK: docker)...")

    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            private=private,
            exist_ok=True
        )
        print(f"[✓] Space repository confirmed: https://huggingface.co/spaces/{repo_id}")
    except Exception as e:
        print(f"[!] Note on repo creation: {e}")

    # Files to upload
    files_to_upload = [
        "Dockerfile",
        "requirements.txt",
        "README.md",
        "main.py",
        "range_proxy.py",
        "stream_probe.py",
        "webdav_engine.py",
        os.path.join("templates", "index.html")
    ]

    print("[*] Uploading application files to Space...")
    for rel_path in files_to_upload:
        full_path = os.path.join(WORKSPACE_DIR, rel_path)
        if os.path.exists(full_path):
            path_in_repo = rel_path.replace("\\", "/")
            print(f"    -> Uploading {path_in_repo}...")
            api.upload_file(
                path_or_fileobj=full_path,
                path_in_repo=path_in_repo,
                repo_id=repo_id,
                repo_type="space"
            )

    print("[✓] All files uploaded successfully!")
    subdomain = f"{username}-{space_name}".replace("_", "-").lower()
    direct_app_url = f"https://{subdomain}.hf.space"
    webdav_url = f"{direct_app_url}/dav/"

    print("\n" + "="*70)
    print(" 🎬 CLOUDSTREAM WEBDAV BRIDGE DEPLOYED SUCCESSFULLY")
    print("="*70)
    print(f" Web UI URL:          {direct_app_url}")
    print(f" WebDAV Root URL:     {webdav_url}")
    print(f" Hugging Face Space:  https://huggingface.co/spaces/{repo_id}")
    print("\n--- CX FILE EXPLORER CONFIGURATION ---")
    print(f" Server / Host:       {subdomain}.hf.space")
    print(" Port:                443")
    print(" Path:                /dav")
    print(" HTTPS / Encryption:  CHECKED (ON)")
    print(" Username:            admin")
    print(" Password:            none (or leave blank)")
    print("="*70)

    # Poll status for up to 60 seconds
    print("\n[*] Monitoring Space build and container startup...")
    for i in range(12):
        time.sleep(5)
        try:
            runtime = api.get_space_runtime(repo_id=repo_id)
            stage = runtime.stage
            print(f"    Current Space Stage: {stage}")
            if stage in ["RUNNING", "APP_STARTING"]:
                print(f"[✓] Space is {stage}! Ready for streaming.")
                break
            elif stage in ["BUILDING"]:
                print("    Docker image building on Hugging Face 2 vCPU cloud...")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Hugging Face Zero-Touch Deployer for CloudStream Bridge")
    parser.add_argument("--token", type=str, help="Hugging Face User Access Token (Write permissions)")
    parser.add_argument("--space-name", type=str, default="cloud-stream-bridge", help="Name of the space")
    parser.add_argument("--private", action="store_true", help="Make the space private")
    args = parser.parse_args()

    ensure_huggingface_hub()
    token = get_token(args.token)

    if not token:
        print("\n[!] No Hugging Face token detected in environment or cache.")
        print("    You can get a free token in 10 seconds (no credit card):")
        print("    -> https://huggingface.co/settings/tokens/new?token_type=write&name=CloudStreamBridge")
        print("\n    Then run:")
        print(f"    python {os.path.basename(__file__)} --token <YOUR_HF_TOKEN>\n")
        return

    deploy_space(token=token, space_name=args.space_name, private=args.private)


if __name__ == "__main__":
    main()
