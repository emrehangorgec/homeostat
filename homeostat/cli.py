"""homeostat command line.

    homeostat observe                 print the current DeviceState and what the rules see
    homeostat run                     run the guardian loop
    homeostat faults                  list fault scenarios
    homeostat inject <fault>          inject one fault
    homeostat eval <fault>... -n 20   run an experiment and print the results table
    homeostat demo                    the whole loop against the simulator, no device needed
    homeostat m0                      probe what the connected device allows (milestone M0)
    homeostat report <experiment>...  write a self-contained HTML report for one or more experiments
    homeostat backend --reverse       serve the testbed page and point the kiosk at it
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from homeostat.config import HomeostatConfig
from homeostat.device.base import Device, DeviceUnreachable
from homeostat.eval import report
from homeostat.eval.runner import RunResult, run_scenario
from homeostat.faults.scenarios import FAULTS, supports_hooks, testbed_for
from homeostat.guardian.loop import IncidentReport
from homeostat.store.sqlite import Store
from homeostat.wiring import SimClock, build_guardian

SIM_BANNER = "SIMULATED DEVICE: these numbers exercise the code path, they are not device results."


def _device(args: argparse.Namespace, config: HomeostatConfig, clock: SimClock | None = None) -> Device:
    if args.sim:
        from homeostat.device.sim import SimDevice

        return SimDevice(config.target, marker=config.oracle.marker, clock=clock.time if clock else None)
    from homeostat.device.adb import AdbDevice

    return AdbDevice(args.serial)


def _load_config(args: argparse.Namespace) -> HomeostatConfig:
    path = Path(args.config)
    if not path.exists():
        sys.exit(f"config not found: {path} (copy config/homeostat.example.toml to homeostat.toml)")
    return _apply_overrides(args, HomeostatConfig.load(path))


def _apply_overrides(args: argparse.Namespace, config: HomeostatConfig) -> HomeostatConfig:
    if getattr(args, "arm", None):
        config.guardian.arm = args.arm
    if getattr(args, "model", None):
        config.diagnose.model = args.model
    if getattr(args, "provider", None):
        config.diagnose.provider = args.provider
    if getattr(args, "think", False):
        config.diagnose.think = True
    return config


def _print_incident(r: IncidentReport) -> None:
    final = r.final_verify
    verify = f"verify={final.strength}" if final else "no verify"
    print(
        f"[{time.strftime('%H:%M:%S')}] incident {r.id}: {r.symptoms} -> {r.incident_type or 'unclassified'} "
        f"-> {r.outcome} ({r.attempts} attempts, {r.closed_at - r.detected_at:.1f}s, {verify})"
    )
    for step in r.steps:
        print(f"    {step.proposal.source}: {step.proposal.action} -> {step.decision.verdict} "
              f"{step.decision.action} ({step.decision.reason})")


def cmd_observe(args: argparse.Namespace) -> None:
    config = _load_config(args)
    guardian = build_guardian(config, _device(args, config), store=Store(":memory:"))
    state = guardian.collector.collect()
    detection = guardian.rules.detect(state)
    print(state.model_dump_json(indent=2))
    print(f"\nsymptoms: {detection.symptoms or 'none'}")
    for m in detection.matches:
        print(f"rule {m.rule.id} -> {m.rule.action}: {m.evidence}")


def cmd_run(args: argparse.Namespace) -> None:
    config = _load_config(args)
    guardian = build_guardian(config, _device(args, config))
    print(f"guardian running, target {config.target.component}, mode {config.mode.value}; Ctrl+C to stop")
    try:
        guardian.run_forever(
            on_incident=_print_incident,
            on_link_lost=lambda e: print(f"[{time.strftime('%H:%M:%S')}] device link lost ({e}), waiting"),
        )
    except KeyboardInterrupt:
        pass


def cmd_faults(args: argparse.Namespace) -> None:
    for f in FAULTS.values():
        tag = " (simulator or homeostat kiosk only)" if f.hooked else ""
        print(f"{f.id:24} {f.category:10} {f.description}{tag}")


def cmd_inject(args: argparse.Namespace) -> None:
    config = _load_config(args)
    testbed = None
    if config.testbed is not None and not args.sim:
        from homeostat.testbed.client import HttpTestbed

        testbed = HttpTestbed(config.testbed.url)
    print(FAULTS[args.fault].inject(_device(args, config), config.target, testbed))


def cmd_backend(args: argparse.Namespace) -> None:
    from homeostat.testbed.backend import serve

    server, _ = serve(port=args.port, host=args.host)
    print(f"testbed backend on http://{args.host}:{args.port}/ (modes: POST /_control?mode=...)")
    if args.reverse:
        from homeostat.device.adb import AdbDevice

        device = AdbDevice(args.serial)
        import subprocess

        subprocess.run([device.adb, "-s", device.serial, "reverse", f"tcp:{args.port}", f"tcp:{args.port}"], check=True)
        url = f"http://127.0.0.1:{args.port}/"
        device.shell(f"am start -n com.homeostat.agent/.KioskActivity --es url {url}")
        print(f"adb reverse set; kiosk now shows {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _adb_reverse(device: Device, url: str) -> None:
    import subprocess
    from urllib.parse import urlparse

    port = urlparse(url).port or 80
    subprocess.run([device.adb, "-s", device.serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
                   check=True, capture_output=True)


def _run_experiment(args: argparse.Namespace, config: HomeostatConfig, device: Device, store: Store,
                    clock, sleep) -> list[RunResult]:
    guardian = build_guardian(config, device, store=store, clock=clock, sleep=sleep)
    testbed = None
    restore_link = None
    if config.testbed is not None and not args.sim:
        from homeostat.testbed.client import HttpTestbed

        testbed = HttpTestbed(config.testbed.url)

        def restore_link() -> None:
            # adb reverse mappings are lost whenever the USB link drops.
            _adb_reverse(device, config.testbed.url)
            print("  link restored, adb reverse set again")

        _adb_reverse(device, config.testbed.url)
    experiment = args.experiment or time.strftime("exp-%Y%m%d-%H%M%S")
    rng = random.Random(args.seed)
    results: list[RunResult] = []
    for fault_id in args.faults:
        fault = FAULTS[fault_id]
        if fault.hooked and not supports_hooks(device, config.target):
            print(f"skipping {fault_id}: needs the simulator or the homeostat kiosk as target")
            continue
        if fault.testbed and testbed_for(device, testbed) is None:
            print(f"skipping {fault_id}: needs the testbed backend ([testbed] in the config)")
            continue
        try:
            results += run_scenario(
                guardian, device, fault, args.n, experiment, arm=config.guardian.arm,
                rng=rng, clock=clock, sleep=sleep, testbed=testbed,
                on_link_restored=restore_link,
                detect_timeout_s=args.detect_timeout,
                on_run=lambda r: print(f"  {r.scenario_id:24} {r.outcome:14} "
                                       f"detect={r.detection_latency_s and round(r.detection_latency_s, 1)} "
                                       f"ttr={r.time_to_recovery_s and round(r.time_to_recovery_s, 1)}")
                if args.verbose else None,
            )
        except DeviceUnreachable as e:
            print(f"stopping: {e}")
            break
    print(f"\nexperiment {experiment}\n")
    print(report.scenario_table(results))
    print("\n" + report.matrix(results))
    return results


class _KeepAwake:
    """Keep Windows from sleeping while an experiment runs: sleep drops the USB link
    (it invalidated a whole scenario in m1-device-02). No system setting is changed."""

    def __enter__(self):
        if sys.platform == "win32":
            import ctypes

            es_continuous, es_system_required = 0x80000000, 0x00000001
            ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def cmd_eval(args: argparse.Namespace) -> None:
    with _KeepAwake():
        _cmd_eval(args)


def _cmd_eval(args: argparse.Namespace) -> None:
    config = _load_config(args)
    if args.sim:
        print(SIM_BANNER)
        clock = SimClock()
        device = _device(args, config, clock)
        _run_experiment(args, config, device, Store(config.store), clock.time, clock.sleep)
    else:
        _run_experiment(args, config, _device(args, config), Store(config.store), time.time, time.sleep)


def cmd_demo(args: argparse.Namespace) -> None:
    from homeostat.device.sim import SimDevice
    from homeostat.state.schema import ActivityRef
    from homeostat.verify.oracle import UiMarker

    config = HomeostatConfig(
        target=ActivityRef(
            package="com.homeostat.agent",
            activity="com.homeostat.agent.KioskActivity",
            process="com.homeostat.agent:kiosk",
        ),
    )
    config.oracle.marker = UiMarker(content_desc="homeostat-ready")
    config.oracle.heartbeat = True
    config = _apply_overrides(args, config)
    if config.guardian.arm != "rules_only":
        config.diagnose.provider = "scripted"  # the demo never calls a real model
    clock = SimClock()
    device = SimDevice(config.target, marker=config.oracle.marker, clock=clock.time)
    args.sim = True
    args.faults = args.faults or list(FAULTS)
    print(SIM_BANNER)
    _run_experiment(args, config, device, Store(args.store or ":memory:"), clock.time, clock.sleep)


def cmd_m0(args: argparse.Namespace) -> None:
    from homeostat import m0
    from homeostat.device.adb import AdbDevice

    from homeostat.device.base import DeviceUnreachable

    try:
        results = m0.run(AdbDevice(args.serial), disruptive=args.disruptive, hide_pkg=args.hide_pkg,
                         reboot=args.reboot)
    except DeviceUnreachable as e:
        sys.exit(f"device link lost during the probe ({e}); results would be wrong, rerun when the link is stable")
    print(m0.table(results))
    m0.save(results, Path(args.out))
    print(f"\nsaved to {args.out}")


def cmd_report(args: argparse.Namespace) -> None:
    from homeostat.eval.html import render, standalone

    store = Store(args.store)
    name = args.name or args.experiments[0].partition(":")[0]
    out = Path(args.out or f"docs/reports/{name}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    page = render(store, args.experiments, name=args.name)
    # A standalone page for GitHub Pages and browsers; a bare fragment for hosts that wrap it.
    out.write_text(page if args.fragment else standalone(page), encoding="utf-8")
    print(f"wrote {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="homeostat", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="homeostat.toml")
    parser.add_argument("--serial", help="adb device serial (default: the only connected device)")
    parser.add_argument("--sim", action="store_true", help="use the simulated device")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("observe").set_defaults(func=cmd_observe)
    p = sub.add_parser("run")
    p.add_argument("--arm", choices=["rules_only", "hybrid", "llm_only"])
    p.add_argument("--model", help="diagnostician model (default from the config: claude-opus-5)")
    p.set_defaults(func=cmd_run)
    sub.add_parser("faults").set_defaults(func=cmd_faults)
    p = sub.add_parser("inject")
    p.add_argument("fault", choices=list(FAULTS))
    p.set_defaults(func=cmd_inject)

    p = sub.add_parser("m0")
    p.add_argument("--disruptive", action="store_true", help="also toggle Wi-Fi off and on")
    p.add_argument("--hide-pkg", help="also hide and unhide this package")
    p.add_argument("--reboot", action="store_true", help="finally reboot through DevicePolicyManager")
    p.add_argument("--out", default="docs/m0_capabilities.json")
    p.set_defaults(func=cmd_m0)

    p = sub.add_parser("backend")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to serve the LAN (network faults)")
    p.add_argument("--reverse", action="store_true", help="adb reverse the port and point the kiosk at it")
    p.set_defaults(func=cmd_backend)

    p = sub.add_parser("report")
    p.add_argument("experiments", nargs="+",
                   help="experiment ids merged into one report; `id:scenario,scenario` keeps only those scenarios")
    p.add_argument("--name", help="report name (default: the experiment ids)")
    p.add_argument("--store", default="homeostat.sqlite")
    p.add_argument("--out", help="default: docs/reports/<experiment>.html")
    p.add_argument("--fragment", action="store_true", help="write the page body only, without the document shell")
    p.set_defaults(func=cmd_report)

    for name, func in (("eval", cmd_eval), ("demo", cmd_demo)):
        p = sub.add_parser(name)
        p.add_argument("faults", nargs="*" if name == "demo" else "+", choices=list(FAULTS), metavar="fault")
        p.add_argument("-n", type=int, default=20, help="runs per scenario (default 20)")
        p.add_argument("--experiment", help="experiment id (default: timestamp)")
        p.add_argument("--seed", type=int, default=None)
        p.add_argument("--detect-timeout", type=float, default=30.0)
        p.add_argument("-v", "--verbose", action="store_true")
        p.add_argument("--arm", choices=["rules_only", "hybrid", "llm_only"])
        p.add_argument("--model", help="diagnostician model (default from the config: claude-opus-5)")
        p.add_argument("--provider", choices=["claude", "ollama", "scripted"])
        p.add_argument("--think", action="store_true", help="ollama: let a reasoning model think first")
        if name == "demo":
            p.add_argument("--store", help="keep results in this SQLite file (default: in memory)")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
