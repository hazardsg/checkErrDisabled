import unittest
from types import MappingProxyType
from unittest.mock import patch

import checkErrDisabled as checker


class CloudVisionMappingTests(unittest.TestCase):
    def test_inventory_accepts_cloudvision_mapping_values(self):
        inventory = {
            "switch-1": MappingProxyType(
                {"deviceType": "EOS", "modelName": "DCS-7280SR3"}
            ),
            "switch-2": MappingProxyType(
                {"deviceType": "EOS", "modelName": "vEOS-router"}
            ),
            "other": MappingProxyType(
                {"deviceType": "CVP", "modelName": "DCS-appliance"}
            ),
        }

        with patch.object(checker, "grpc_query", return_value=inventory):
            devices, inventory_count, eos_count = checker.get_physical_eos_devices(
                object()
            )

        self.assertEqual((3, 2), (inventory_count, eos_count))
        self.assertEqual(["switch-1"], list(devices))

    def test_empty_errdisabled_map_is_a_clean_result(self):
        with patch.object(checker, "grpc_query", return_value={}):
            interfaces = checker.get_errdisabled_interfaces(object(), "switch-1")

        self.assertEqual([], interfaces)

    def test_hitless_reload_interfaces_are_skipped(self):
        updates = MappingProxyType(
            {
                "Ethernet1": MappingProxyType(
                    {
                        "errdisableDescription": (
                            "Interface is disabled for the duration of hitless "
                            "reload."
                        )
                    }
                ),
                "Ethernet2": MappingProxyType(
                    {"errdisableDescription": "BPDU received on portfast port."}
                ),
                "Ethernet3": MappingProxyType(
                    {
                        "statusDetail": MappingProxyType(
                            {
                                "detail": MappingProxyType(
                                    {
                                        "encoded-detail": MappingProxyType(
                                            {
                                                "_key": MappingProxyType(
                                                    {"Name": "hitlessReload"}
                                                )
                                            }
                                        )
                                    }
                                )
                            }
                        )
                    }
                ),
            }
        )

        with patch.object(checker, "grpc_query", return_value=updates):
            interfaces = checker.get_errdisabled_interfaces(object(), "switch-1")

        self.assertEqual(
            [
                {
                    "interface": "Ethernet2",
                    "status": "errdisabled",
                    "cause": "BPDU received on portfast port.",
                }
            ],
            interfaces,
        )

    def test_telemetry_mappings_include_errors_and_skip_subsumed_ports(self):
        subsumed_detail = MappingProxyType(
            {
                "encoded-detail": MappingProxyType(
                    {
                        "_key": MappingProxyType({"Name": "subsumed", "Value": 5}),
                        "_value": "Interface is subsumed by Ethernet53/1",
                    }
                )
            }
        )
        updates = MappingProxyType(
            {
                "Ethernet53/1": MappingProxyType(
                    {
                        "intfId": "Ethernet53/1",
                        "errdisableDescription": "bpduguard",
                    }
                ),
                "Ethernet53/2": MappingProxyType(
                    {
                        "intfId": "Ethernet53/2",
                        "statusDetail": MappingProxyType(
                            {"detail": subsumed_detail}
                        ),
                        "errdisableDescription": "",
                    }
                ),
            }
        )

        with patch.object(checker, "grpc_query", return_value=updates):
            interfaces = checker.get_errdisabled_interfaces(object(), "switch-1")

        self.assertEqual(
            [
                {
                    "interface": "Ethernet53/1",
                    "status": "errdisabled",
                    "cause": "bpduguard",
                }
            ],
            interfaces,
        )


if __name__ == "__main__":
    unittest.main()
