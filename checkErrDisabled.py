"""Report err-disabled physical switch interfaces from Arista CloudVision."""

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import sys
import time
from typing import Any

from cloudvision.Connector.grpc_client import GRPCClient, create_query


DEVICE_LIST_PATH = ["DatasetInfo", "Devices"]
INTERFACE_STATUS_PATH = [
    "Sysdb",
    "interface",
    "errdisable",
    "status",
    "errdisabledIntf",
]

# A lane such as Ethernet53/2 is decoded as separate path parts: Ethernet53, 2.
INTERFACE_START = re.compile(r"^(?:Ethernet|Et)\d+$", re.IGNORECASE)

DeviceInfo = Mapping[str, Any]
InterfaceResult = dict[str, str]
QueryResults = dict[str, Any]


def positive_int(value: str) -> int:
    """Parse a command-line value that must be a positive integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args() -> argparse.Namespace:
    """Parse and validate command-line authentication and worker options."""
    parser = argparse.ArgumentParser(
        description="Find error-disabled Ethernet interfaces in CloudVision"
    )
    parser.add_argument(
        "--apiserver",
        required=True,
        help="CloudVision gRPC endpoint",
    )
    parser.add_argument(
        "--tokenFile",
        "--token-file",
        dest="token_file",
        help="Service account token file",
    )
    parser.add_argument(
        "--certFile",
        "--cert-file",
        dest="cert_file",
        help="Client certificate",
    )
    parser.add_argument(
        "--keyFile",
        "--key-file",
        dest="key_file",
        help="Client private key",
    )
    parser.add_argument(
        "--caFile",
        "--ca-file",
        dest="ca_file",
        help="CA certificate",
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=10,
        help="Maximum concurrent switch queries (default: 10)",
    )
    args = parser.parse_args()

    if bool(args.cert_file) != bool(args.key_file):
        parser.error("--certFile and --keyFile must be provided together")
    if args.token_file and args.cert_file:
        parser.error("provide either --tokenFile or the certificate/key pair")
    if not args.token_file and not args.cert_file:
        parser.error("provide --tokenFile or both --certFile and --keyFile")
    return args


def grpc_query(
    args: argparse.Namespace,
    dataset: str,
    path_elements: Sequence[str],
) -> QueryResults:
    """Run a current-state gRPC query and merge its notification updates."""
    query = create_query([(path_elements, [])], dataset)
    results: QueryResults = {}
    with GRPCClient(
        args.apiserver,
        certs=args.cert_file,
        key=args.key_file,
        ca=args.ca_file,
        token=args.token_file,
    ) as client:
        for batch in client.get([query]):
            for notification in batch.get("notifications", []):
                results.update(notification.get("updates", {}))
    return results


def describe_error(exc: Exception) -> str:
    """Return a useful message for common gRPC failures."""
    code_method = getattr(exc, "code", None)
    try:
        code = code_method() if callable(code_method) else None
    except Exception:
        # Error reporting must not replace the original failure with a new one.
        code = None

    code_name = getattr(code, "name", "")
    if code_name == "UNAUTHENTICATED":
        return (
            "CloudVision rejected authentication. Verify that --tokenFile contains "
            "the raw, active service-account token for this tenant, or provide a "
            "valid client certificate/key pair."
        )
    if code_name == "PERMISSION_DENIED":
        return (
            "CloudVision authenticated the request but denied access; check "
            "service-account permissions."
        )
    return str(exc)


def get_physical_eos_devices(
    args: argparse.Namespace,
) -> tuple[dict[str, DeviceInfo], int, int]:
    """Return EOS inventory entries whose model identifies a physical switch."""
    inventory = grpc_query(args, "analytics", DEVICE_LIST_PATH)

    # The Connector decodes CloudVision maps as FrozenDict objects. Mapping keeps
    # this compatible with both those values and ordinary dictionaries in tests.
    eos_devices = {
        device_id: info
        for device_id, info in inventory.items()
        if isinstance(info, Mapping) and info.get("deviceType") == "EOS"
    }
    physical_devices = {
        device_id: info
        for device_id, info in eos_devices.items()
        if any(
            model in str(info.get("modelName", "")).upper()
            for model in ("DCS", "CCS")
        )
    }
    return physical_devices, len(inventory), len(eos_devices)


def _path_parts(value: object) -> list[str]:
    """Split a decoded CloudVision path or key into non-empty components."""
    return [part for part in str(value).strip("/").split("/") if part]


def _find_interface(path_parts: Sequence[str]) -> tuple[str | None, list[str]]:
    """Extract an Ethernet interface and return any remaining field path."""
    for index, part in enumerate(path_parts):
        if not INTERFACE_START.fullmatch(part):
            continue
        end = index + 1
        while end < len(path_parts) and path_parts[end].isdigit():
            end += 1
        return "/".join(path_parts[index:end]), list(path_parts[end:])
    return None, []


def _wrap_path(path_parts: Sequence[str], value: Any) -> Any:
    """Rebuild the field path beneath an interface around its decoded value."""
    for part in reversed(path_parts):
        value = {part: value}
    return value


def collect_interface_records(
    updates: Mapping[str, Any],
) -> dict[str, list[Any]]:
    """Group all telemetry fragments by their Ethernet interface.

    CloudVision may place the interface in an update key or within a nested map.
    Keeping every fragment lets later parsing find descriptions and status details
    without depending on one EOS telemetry layout.
    """
    records: dict[str, list[Any]] = {}

    def walk(path_parts: list[str], value: Any) -> None:
        interface, remainder = _find_interface(path_parts)
        if interface:
            payload = _wrap_path(remainder, value)
            records.setdefault(interface, []).append(payload)
            return
        if isinstance(value, Mapping):
            for key, nested_value in value.items():
                walk(path_parts + _path_parts(key), nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                walk(path_parts, nested_value)

    for key, value in updates.items():
        walk(_path_parts(key), value)
    return records


def _normalized(value: object) -> str:
    """Normalize telemetry field names for case-insensitive comparisons."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _reason_text(value: Any) -> str | None:
    """Extract useful text from a scalar or a CloudVision enum-shaped map."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        ignored_values = {"", "none", "null", "unknown", "na"}
        return text if _normalized(text) not in ignored_values else None
    if isinstance(value, Mapping):
        for key in ("Name", "name", "value", "Value"):
            if key in value:
                text = _reason_text(value[key])
                if text:
                    return text
    return None


def _mapping_value(mapping: object, wanted_key: str) -> Any:
    """Get a map value after normalizing underscores, case, and punctuation."""
    if not isinstance(mapping, Mapping):
        return None
    normalized_key = _normalized(wanted_key)
    for key, value in mapping.items():
        if _normalized(key) == normalized_key:
            return value
    return None


def _contains_normalized_signal(value: Any, signal: str) -> bool:
    """Search nested telemetry keys and text for a normalized signal."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if signal in _normalized(key) or _contains_normalized_signal(
                child, signal
            ):
                return True
    elif isinstance(value, list):
        return any(_contains_normalized_signal(child, signal) for child in value)
    elif isinstance(value, str):
        return signal in _normalized(value)
    return False


def has_subsumed_signal(value: Any) -> bool:
    """Return whether telemetry marks a child lane as subsumed by another port."""
    return _contains_normalized_signal(value, "subsumed")


def has_hitless_reload_signal(value: Any) -> bool:
    """Return whether telemetry describes a transient hitless reload state."""
    return _contains_normalized_signal(value, "hitlessreload")


def find_errdisabled_cause(payloads: Sequence[Any]) -> str | None:
    """Find the most specific cause available in interface telemetry.

    Explicit errdisable descriptions take precedence over generic reason/cause
    fields, followed by the encoded entries in the status detail map.
    """
    candidates: list[tuple[int, str]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_name = _normalized(key)
                if key_name == "errdisabledescription":
                    text = _reason_text(child)
                    if text:
                        candidates.append((0, text))
                elif "reason" in key_name or "cause" in key_name:
                    text = _reason_text(child)
                    if text:
                        candidates.append((1, text))
                elif key_name == "detail" and isinstance(child, Mapping):
                    # Detail map keys are encoded. The decoded _key and _value
                    # fields carry the readable reason and its explanation.
                    for detail_entry in child.values():
                        if not isinstance(detail_entry, Mapping):
                            continue
                        key_data = _mapping_value(detail_entry, "_key")
                        cause_name = _reason_text(
                            _mapping_value(key_data, "name")
                        )
                        cause_detail = _reason_text(
                            _mapping_value(detail_entry, "_value")
                        )
                        cause_parts = (cause_name, cause_detail)
                        cause = ": ".join(part for part in cause_parts if part)
                        if cause:
                            candidates.append((2, cause))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for payload in payloads:
        walk(payload)
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: candidate[0])[1]


def get_errdisabled_interfaces(
    args: argparse.Namespace,
    device_id: str,
) -> list[InterfaceResult]:
    """Return reportable err-disabled interfaces for one CloudVision device."""
    updates = grpc_query(args, device_id, INTERFACE_STATUS_PATH)
    if not updates:
        # An empty map is the normal CloudVision representation for no entries.
        return []

    records = collect_interface_records(updates)
    if not records:
        path = "/".join(INTERFACE_STATUS_PATH)
        raise RuntimeError(f"No Ethernet interface records found under {path}")

    errdisabled: list[InterfaceResult] = []
    for interface, payloads in records.items():
        # Breakout child lanes can appear in this map as inactive/subsumed. They
        # are not independent err-disabled ports and should not trigger an alert.
        if any(has_subsumed_signal(payload) for payload in payloads):
            continue

        # Hitless reload temporarily disables interfaces and EOS recovers them
        # without operator intervention, so these records are not actionable.
        if any(has_hitless_reload_signal(payload) for payload in payloads):
            continue

        # Presence in errdisabledIntf establishes the state; EOS releases do not
        # expose a consistent status field that needs an additional check here.
        result = {"interface": interface, "status": "errdisabled"}
        cause = find_errdisabled_cause(payloads)
        if cause:
            result["cause"] = cause
        errdisabled.append(result)
    return sorted(errdisabled, key=lambda item: item["interface"])


def check_device(
    args: argparse.Namespace,
    device_id: str,
    info: DeviceInfo,
) -> dict[str, Any]:
    """Build the output record for one physical switch."""
    return {
        "hostname": info.get("hostname", device_id),
        "model": info.get("modelName", ""),
        "errdisabledInterfaces": get_errdisabled_interfaces(args, device_id),
    }


def main(args: argparse.Namespace) -> int:
    """Discover devices, query them concurrently, and print JSON results."""
    start = time.monotonic()
    try:
        devices, inventory_count, eos_count = get_physical_eos_devices(args)
    except Exception as exc:
        error = f"Device discovery failed: {describe_error(exc)}"
        print(json.dumps({"error": error}, indent=2), file=sys.stderr)
        return 2

    if not devices:
        message = "No physical EOS devices matched the DCS/CCS model filter."
        print(json.dumps({"error": message}, indent=2))
        print(
            f"Inventory records: {inventory_count}; EOS devices: {eos_count}",
            file=sys.stderr,
        )
        return 2

    results: dict[str, dict[str, Any]] = {}
    worker_count = min(args.max_workers, len(devices))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_device = {
            executor.submit(check_device, args, device_id, info): (device_id, info)
            for device_id, info in devices.items()
        }
        for future in as_completed(future_to_device):
            device_id, info = future_to_device[future]
            try:
                results[device_id] = future.result()
            except Exception as exc:
                # Keep one failed device query from discarding successful results.
                results[device_id] = {
                    "hostname": info.get("hostname", device_id),
                    "model": info.get("modelName", ""),
                    "error": describe_error(exc),
                }

    print(json.dumps(results, indent=2, sort_keys=True))
    duration = time.monotonic() - start
    print(f"\nCompleted in {duration:.2f} seconds", file=sys.stderr)
    return 1 if any("error" in result for result in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main(parse_args()))
