"""Narrow command bridge used by Hermes; no arbitrary shell or interactive auth."""
import argparse
import json
from pathlib import Path
from mailorganizer import runtime


def main():
    parser = argparse.ArgumentParser(description="Hermes command bridge for Mail Organizer")
    parser.add_argument("action", choices=["status", "start", "stop", "check", "report", "prepare", "tasks"])
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "runtime.json")
    args = parser.parse_args()
    if args.action in {"status", "start", "stop", "check"}:
        action = "once" if args.action == "check" else args.action
        return runtime.main([action, "--config", str(args.config)])
    from mailorganizer.cli import main as cli_main
    try:
        config = runtime.load_config(args.config)
    except (ValueError, OSError, KeyError):
        print(json.dumps({"status":"configuration_required"}))
        return 2
    return cli_main([args.action, "--root", config["archive_root"]])


if __name__ == "__main__":
    raise SystemExit(main())
