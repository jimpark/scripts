#!/usr/bin/env python3
"""Configure the Claude Code VS Code extension to use models on AWS Bedrock.

configure-vscode-bedrock.py wires the Claude Code extension up to AWS Bedrock by
writing the right keys into your VS Code settings.json. Before touching
anything it checks the AWS CLI, verifies your AWS profile, and runs a live
Bedrock invoke-model call against each model you've chosen — so a misconfigured
profile, an incomplete Anthropic "First Time Use" form, or a typo'd inference
profile ID is caught up front rather than the next time you open the editor.

The settings file is written safely: the existing file is copied to a
'.bak' sibling first, then the new content is written to a temp file in the same
directory and atomically renamed into place. If anything fails the original is
left untouched, and you can always restore from the backup.

Values can come from flags or interactive prompts. Any value not passed as a
flag is prompted for (with a default), unless --non-interactive is given, in
which case the default is used. Pass --dry-run to see exactly what would be
written without changing the file.

The keys written are:
    claudeCode.environmentVariables   array of {name, value} env-var objects
    claudeCode.selectedModel          one of: sonnet / opus / haiku

NOTE: The model IDs below are *examples* and go stale as new models ship. List
what's actually available to you with:
    aws bedrock list-inference-profiles --region <REGION> --profile <PROFILE>

Examples:
    configure-vscode-bedrock.py
    configure-vscode-bedrock.py --profile dev --region us-east-1 --default-model opus
    configure-vscode-bedrock.py --non-interactive --skip-validation --dry-run
    configure-vscode-bedrock.py --sonnet global.anthropic.claude-sonnet-4-6

Exit status:
    0   success (settings written, or a dry run)
    1   an error (AWS check failed, validation failed, settings unparseable)
    2   usage error (bad/missing arguments; handled by argparse)
    130 interrupted (Ctrl+C)
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile

__version__ = "1.0.0"

# VS Code settings.json keys contributed by the Claude Code extension. If the
# extension renames these, update them here.
ENV_SETTING_KEY = "claudeCode.environmentVariables"
MODEL_SETTING_KEY = "claudeCode.selectedModel"

MODEL_CHOICES = ("sonnet", "opus", "haiku")

# Example defaults only — verify against `aws bedrock list-inference-profiles`.
DEFAULT_REGION = "us-east-1"
DEFAULT_SONNET = "global.anthropic.claude-sonnet-4-6"
DEFAULT_OPUS = "global.anthropic.claude-opus-4-8"
DEFAULT_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def run_command(command, error_msg, exit_on_fail=True):
    """Run a command and return its stdout (stripped). Exit on failure if asked."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"\n[ERROR] {error_msg}")
        print(f"Details: {exc.stderr.strip()}")
        if exit_on_fail:
            sys.exit(1)
        return None
    except FileNotFoundError:
        print(f"\n[ERROR] Command not found. {error_msg}")
        if exit_on_fail:
            sys.exit(1)
        return None


def check_aws_cli():
    """Confirm the AWS CLI is installed and is v2 (needed for --cli-binary-format)."""
    version = run_command(["aws", "--version"], "AWS CLI is not installed or not on PATH.")
    # e.g. "aws-cli/2.15.0 Python/3.11 ..."
    if version and not version.startswith("aws-cli/2"):
        print(f"[WARNING] Expected AWS CLI v2 but found: {version}")
        print("          The live model test uses a v2-only flag and may fail.")


def get_vscode_settings_path() -> str:
    """Return the path to VS Code's user settings.json for the current OS."""
    system = platform.system()
    if system == "Windows":
        base_path = os.environ.get("APPDATA")
        if not base_path:
            print("[ERROR] APPDATA environment variable not found.")
            sys.exit(1)
        return os.path.join(base_path, "Code", "User", "settings.json")
    if system == "Darwin":  # macOS
        return os.path.expanduser("~/Library/Application Support/Code/User/settings.json")
    return os.path.expanduser("~/.config/Code/User/settings.json")  # Linux


def validate_aws_profile(profile: str):
    """Verify the AWS profile can authenticate (sts get-caller-identity)."""
    print(f"\n--- Validating AWS profile '{profile}' ---")
    run_command(
        ["aws", "sts", "get-caller-identity", "--profile", profile],
        f"Failed to authenticate with AWS profile '{profile}'. "
        "Check your ~/.aws/credentials and ~/.aws/config.",
    )
    print("[SUCCESS] AWS authentication verified.")


def validate_bedrock_access(profile: str, region: str, model_ids):
    """Live invoke-model call against each unique model ID to prove access."""
    print(f"\n--- Validating Bedrock access in {region} ---")

    # A temp payload file avoids cross-shell string-escaping issues (PowerShell
    # vs Bash) that plague inline --body JSON.
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Say hello"}],
    }
    in_fd, in_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(in_fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    out_fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(out_fd)

    try:
        for model_id in dict.fromkeys(model_ids):  # de-dupe, preserve order
            print(f"Invoking {model_id} ...")
            run_command(
                [
                    "aws", "bedrock-runtime", "invoke-model",
                    "--model-id", model_id,
                    "--body", f"file://{in_path}",
                    "--region", region,
                    "--profile", profile,
                    "--cli-binary-format", "raw-in-base64-out",
                    out_path,
                ],
                f"Bedrock invocation failed for '{model_id}'. Ensure the Anthropic "
                "First Time Use (FTU) form is complete for this region, the model "
                "ID is correct, and your IAM principal has 'bedrock:InvokeModel'.",
            )
        print("[SUCCESS] Bedrock model access verified.")
    finally:
        for path in (in_path, out_path):
            if os.path.exists(path):
                os.remove(path)


def load_settings(settings_path: str) -> dict:
    """Read and parse settings.json, returning {} if it doesn't exist yet."""
    if not os.path.exists(settings_path):
        print("[INFO] settings.json not found; a new one will be created.")
        return {}
    with open(settings_path, "r", encoding="utf-8") as f:
        content = f.read()
    if not content.strip():
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        print(f"[ERROR] Could not parse {settings_path} as plain JSON.")
        print("        VS Code allows comments (// ...) in settings.json, but this")
        print("        script can't preserve them. Add the following keys manually:")
        print()
        print(json.dumps({ENV_SETTING_KEY: "...", MODEL_SETTING_KEY: "..."}, indent=4))
        sys.exit(1)


def build_env_vars(profile, region, sonnet, opus, haiku):
    """Build the claudeCode.environmentVariables array (list of {name, value})."""
    return [
        {"name": "AWS_PROFILE", "value": profile},
        {"name": "AWS_REGION", "value": region},
        {"name": "CLAUDE_CODE_USE_BEDROCK", "value": "1"},
        {"name": "ANTHROPIC_DEFAULT_SONNET_MODEL", "value": sonnet},
        {"name": "ANTHROPIC_DEFAULT_OPUS_MODEL", "value": opus},
        {"name": "ANTHROPIC_DEFAULT_HAIKU_MODEL", "value": haiku},
    ]


def write_settings_atomically(settings_path: str, settings: dict):
    """Back up the existing file, then atomically replace it with `settings`.

    The existing file is copied to '<file>.bak' first. New content is written to
    a temp file in the same directory and os.replace()'d into place (atomic on
    the same filesystem). On any failure the temp file is removed and the
    original — plus its backup — are left intact.
    """
    dir_name = os.path.dirname(settings_path)
    os.makedirs(dir_name, exist_ok=True)

    if os.path.exists(settings_path):
        backup_path = settings_path + ".bak"
        shutil.copy2(settings_path, backup_path)
        print(f"[INFO] Backed up existing settings to {backup_path}")

    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
            f.write("\n")
        os.replace(tmp_path, settings_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def resolve(value, prompt_text, default, interactive):
    """Return `value` if set, else prompt (interactive) or fall back to default."""
    if value is not None:
        return value
    if not interactive:
        return default
    return input(f"{prompt_text} [{default}]: ").strip() or default


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="configure-vscode-bedrock.py",
        description="Configure the Claude Code VS Code extension to use AWS Bedrock.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Any value not given as a flag is prompted for (unless\n"
            "--non-interactive). Model IDs are examples and go stale; list real\n"
            "ones with: aws bedrock list-inference-profiles --region <REGION>\n"
        ),
    )
    parser.add_argument("--profile", help="AWS profile name (default: prompt, then 'default')")
    parser.add_argument("--region", help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument("--sonnet", help=f"Sonnet inference profile ID (default: {DEFAULT_SONNET})")
    parser.add_argument("--opus", help=f"Opus inference profile ID (default: {DEFAULT_OPUS})")
    parser.add_argument("--haiku", help=f"Haiku inference profile ID (default: {DEFAULT_HAIKU})")
    parser.add_argument("--default-model", choices=MODEL_CHOICES,
                        help="Which model the extension selects by default (default: sonnet)")
    parser.add_argument("--settings-path",
                        help="Override the VS Code settings.json path (useful for testing).")
    parser.add_argument("--skip-validation", action="store_true",
                        help="Skip all live AWS calls (profile + Bedrock checks).")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Never prompt; use flags and defaults only.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change; write nothing.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    interactive = not args.non_interactive

    print("======================================================")
    print("   Claude Code + AWS Bedrock Configurator")
    print("======================================================")

    # Gather configuration (flags > prompts > defaults).
    profile = resolve(args.profile, "Enter your AWS profile name", "default", interactive)
    region = resolve(args.region, "Enter your AWS region (e.g. us-east-1, eu-west-2)",
                     DEFAULT_REGION, interactive)
    sonnet = resolve(args.sonnet, "Sonnet model ID", DEFAULT_SONNET, interactive)
    opus = resolve(args.opus, "Opus model ID", DEFAULT_OPUS, interactive)
    haiku = resolve(args.haiku, "Haiku model ID", DEFAULT_HAIKU, interactive)
    default_model = resolve(args.default_model, "Default model (sonnet/opus/haiku)",
                            "sonnet", interactive).lower()
    if default_model not in MODEL_CHOICES:
        print(f"[ERROR] Default model must be one of: {', '.join(MODEL_CHOICES)}.")
        return 1

    # Validate against AWS (unless skipped).
    if args.skip_validation:
        print("\n[INFO] Skipping AWS validation (--skip-validation).")
    else:
        check_aws_cli()
        validate_aws_profile(profile)
        validate_bedrock_access(profile, region, [sonnet, opus, haiku])

    # Build the settings to write.
    settings_path = args.settings_path or get_vscode_settings_path()
    env_vars = build_env_vars(profile, region, sonnet, opus, haiku)

    if args.dry_run:
        print("\n--- Dry run: settings.json would be updated ---")
        print(f"Target: {settings_path}")
        print(json.dumps({ENV_SETTING_KEY: env_vars, MODEL_SETTING_KEY: default_model}, indent=4))
        print("\n[INFO] No changes written (--dry-run).")
        return 0

    print("\n--- Updating VS Code settings ---")
    print(f"Target settings file: {settings_path}")
    settings = load_settings(settings_path)
    settings[ENV_SETTING_KEY] = env_vars
    settings[MODEL_SETTING_KEY] = default_model
    write_settings_atomically(settings_path, settings)
    print("[SUCCESS] VS Code settings updated.")

    print("\n======================================================")
    print("Configuration complete.")
    print("Reload your VS Code window to apply: open the command palette")
    print("(Ctrl+Shift+P / Cmd+Shift+P) and run 'Developer: Reload Window'.")
    print("======================================================")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[ABORTED] Interrupted by user.")
        raise SystemExit(130)
