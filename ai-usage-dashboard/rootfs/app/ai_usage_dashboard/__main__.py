"""CLI: python -m ai_usage_dashboard {validate-config, collect, publish}."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import collector as collector_mod
from . import config as config_mod
from . import mqtt as mqtt_mod
from .http_client import SafeHttpClient


def _load_fixtures(fixtures_dir: str | None) -> dict:
    if not fixtures_dir or not os.path.isdir(fixtures_dir):
        return {}
    out: dict = {}
    for name in os.listdir(fixtures_dir):
        if not name.endswith(".json"):
            continue
        # fixture file: <provider>__<account_id>.json  (provider may contain _)
        stem = name[: -len(".json")]
        with open(os.path.join(fixtures_dir, name), encoding="utf-8") as fh:
            out[stem.replace("__", "/")] = json.load(fh)
    return out


def cmd_validate_config(args) -> int:
    problems = config_mod.validate_file(args.config)
    try:
        cfg = config_mod.load_config(args.config)
        print(f"OK: {args.config}: {len(cfg['accounts'])} account(s)")
        for acct in cfg["accounts"]:
            cred = acct.credential.describe()
            print(f"  - {acct.key} ({acct.display_name}) credential={cred}")
    except (config_mod.ConfigError, OSError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if problems:
        print("warnings (live mode needs these; fixture mode does not):")
        for problem in problems:
            print(f"  ! {problem}")
    return 0


def _run_collect(args) -> tuple[list, dict]:
    cfg = config_mod.load_config(args.config)
    fixtures = _load_fixtures(args.fixtures)
    fixture_mode = bool(args.fixtures) or os.environ.get("AIUD_FIXTURES") == "1"
    http = SafeHttpClient(timeout=args.timeout)
    collector = collector_mod.Collector(
        http,
        fixtures=fixtures,
        fixture_mode=fixture_mode,
        state_path=args.state_file,
    )
    snapshots = collector.collect_all(cfg["accounts"])
    return snapshots, cfg


def cmd_collect(args) -> int:
    if not args.once:
        print("only --once is supported", file=sys.stderr)
        return 2
    try:
        snapshots, _ = _run_collect(args)
    except (config_mod.ConfigError, OSError) as exc:
        print(f"collect failed: {exc}", file=sys.stderr)
        return 1
    doc = {"snapshots": [s.to_dict() for s in snapshots]}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        print(f"wrote {args.output}")
    else:
        print(json.dumps(doc, indent=2))
    unsupported = [s.key for s in snapshots if s.status.value == "unsupported"]
    if unsupported:
        print("note: unsupported providers (no stable documented API): "
              + ", ".join(unsupported), file=sys.stderr)
    bad = [s.key for s in snapshots if s.status.value in ("auth_error", "error")]
    if bad:
        print("errors for: " + ", ".join(bad), file=sys.stderr)
        return 1
    return 0


def cmd_publish(args) -> int:
    if not args.once:
        print("only --once is supported", file=sys.stderr)
        return 2
    try:
        snapshots, cfg = _run_collect(args)
    except (config_mod.ConfigError, OSError) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    mqtt_cfg = cfg["mqtt"]
    prefix = (
        mqtt_cfg.get("discovery_prefix") or mqtt_mod.DISCOVERY_PREFIX
    )
    if args.dry_run or not cfg["mqtt"].get("host"):
        if not args.dry_run:
            print("no mqtt.host configured; emitting dry-run JSON instead",
                  file=sys.stderr)
        doc = mqtt_mod.build_dry_run(snapshots, discovery_prefix=prefix)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
            print(f"wrote {args.output}")
        else:
            print(json.dumps(doc, indent=2))
        return 0
    try:
        result = mqtt_mod.publish_live(
            snapshots,
            host=mqtt_cfg.get("host", "localhost"),
            port=int(mqtt_cfg.get("port", 1883)),
            username=mqtt_cfg.get("username"),
            password=os.environ.get(mqtt_cfg.get("password_env", "")) or None,
            discovery_prefix=prefix,
        )
    except RuntimeError as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-usage-dashboard")
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate-config", help="validate a config file")
    p_validate.add_argument("--config", default="examples/config.example.yaml")
    p_validate.set_defaults(func=cmd_validate_config)

    p_collect = sub.add_parser("collect", help="collect account snapshots")
    p_collect.add_argument("--once", action="store_true", required=True)
    p_collect.add_argument("--config", default="examples/config.example.yaml")
    p_collect.add_argument("--fixtures", default=None)
    p_collect.add_argument("--state-file", default=None)
    p_collect.add_argument("--timeout", type=float, default=15.0)
    p_collect.add_argument("--output", default=None)
    p_collect.set_defaults(func=cmd_collect)

    p_publish = sub.add_parser("publish", help="publish snapshots via MQTT/discovery")
    p_publish.add_argument("--once", action="store_true", required=True)
    p_publish.add_argument("--config", default="examples/config.example.yaml")
    p_publish.add_argument("--fixtures", default=None)
    p_publish.add_argument("--state-file", default=None)
    p_publish.add_argument("--timeout", type=float, default=15.0)
    p_publish.add_argument("--dry-run", action="store_true")
    p_publish.add_argument("--output", default=None)
    p_publish.set_defaults(func=cmd_publish)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
