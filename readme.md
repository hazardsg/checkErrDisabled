# CVaaS Err-Disabled Port Checker

This Python CLI queries Arista CloudVision-as-a-Service (CVaaS) for
err-disabled Ethernet interfaces on physical EOS switches. It uses the
CloudVision gRPC Connector and a thread pool to check multiple switches
concurrently.

## Requirements

- Python 3.10 or newer
- Arista's `cloudvision` Python package (the tested version is pinned in
  `requirements.txt`)
- A CVaaS service account token or client certificate and key with permission
  to read device telemetry

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Usage

Token authentication:

```bash
python checkErrDisabled.py --apiserver <CVaaS_HOST>:443 --tokenFile <TOKEN_FILE>
```

Use the gRPC hostname with port `443`; omit `https://` and any Markdown link
syntax. The token file should contain only the service-account token text, with
no `Bearer ` prefix. Restrict it to the current user:

```bash
chmod 600 <TOKEN_FILE>
```

Certificate authentication:

```bash
python checkErrDisabled.py \
  --apiserver <CVaaS_HOST>:443 \
  --certFile <CERT_FILE> \
  --keyFile <KEY_FILE>
```

Add `--caFile <CA_FILE>` when the endpoint requires a custom CA certificate.

Use `--max-workers` to adjust parallel queries; the default is 10. The tool
checks EOS inventory entries whose model name contains `DCS` or `CCS`, matching
the physical-switch filter in the power-supply checker.

Both camel-case options such as `--tokenFile` and conventional dashed options
such as `--token-file` are supported.

## Output

The JSON result includes each matching switch. A switch with no err-disabled
interfaces has an empty `errdisabledInterfaces` list. Normal output is written
to standard output; timing and discovery diagnostics are written to standard
error.

```json
{
  "<device_id>": {
    "hostname": "<hostname>",
    "model": "DCS-<model>",
    "errdisabledInterfaces": [
      {
        "interface": "Ethernet1",
        "status": "errdisabled",
        "cause": "bpduguard"
      }
    ]
  }
}
```

Exit codes are:

- `0`: discovery and all device queries succeeded
- `1`: at least one device query failed; successful device results are retained
- `2`: device discovery failed or no physical EOS devices matched

## How It Works

The script reads device inventory from `DatasetInfo/Devices` in the `analytics`
dataset, selects physical EOS switches, and queries each device in a worker
thread. Each worker uses its own gRPC client so a failed query is reported only
for that device. Results are sorted and emitted as machine-readable JSON.

## Telemetry Notes

The script queries `Sysdb/interface/errdisable/status/errdisabledIntf` and
reports actionable interfaces in that map. It excludes entries marked
`subsumed`, which are child breakout lanes controlled by a parent interface. It
also excludes interfaces temporarily disabled during hitless reload because EOS
recovers them without operator intervention. A switch with no remaining entries
is reported with an empty interface list.

CloudVision decodes telemetry maps as immutable mapping objects. The parser
accepts these mappings, groups split path fragments by interface, and selects a
cause from `errdisableDescription`, reason/cause fields, or the status detail
map, in that order.

EOS Sysdb field names can vary by release. When a known err-disabled port is
missing, compare results with `show interfaces status errdisabled`, confirm the
status tree in CloudVision, and update `INTERFACE_STATUS_PATH` if needed.

Keep tokens, certificates, and private keys out of version control. `venv/`,
`token.txt`, and Python cache files are ignored locally.

## Development

Run the focused unit tests and syntax check from this directory:

```bash
python -m unittest discover -s tests -v
python -m py_compile checkErrDisabled.py
```

Use four spaces for indentation, `snake_case` for Python names, and type hints
for function contracts. Comments and docstrings should explain CloudVision data
shapes and parsing decisions rather than restating individual statements.

## Troubleshooting

`UNAUTHENTICATED` means the request reached CloudVision but the credentials were
rejected before device discovery completed. Confirm the token is active,
belongs to this CVaaS tenant, and is copied into the token file without a
`Bearer ` prefix. Do not share the token value when asking for help.

## References

- [CloudVision gRPC API examples](https://aristanetworks.github.io/cloudvision-apis/examples/gRPC)
- [Arista CloudVision Python library](https://github.com/aristanetworks/cloudvision-python)
