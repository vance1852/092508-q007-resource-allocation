from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock
from collection_logistics.errors import Conflict, Forbidden, InvalidState, ResourceConstraint
from collection_logistics.planning import AllocationRequest, RiskPoint, allocate_capacity, latest_streak, pick_lots_fefo
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.risk import DemandBucket, inventory_coverage, mark_to_risk, traffic_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_uses_first_close_as_base(self) -> None:
        streak = latest_streak([
            RiskPoint("2026-09-18", Decimal("108")),
            RiskPoint("2026-09-19", Decimal("105")),
            RiskPoint("2026-09-20", Decimal("102")),
            RiskPoint("2026-09-21", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 4)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["dispatch_id"], "first")
        self.assertEqual(rows[0]["allocated_units"], "70.000")
        self.assertEqual(rows[1]["allocated_units"], "30.000")

    def test_inventory_coverage_and_traffic_gap(self) -> None:
        coverage = inventory_coverage(
            [{"center_id": "receiving-vault", "preservation_resource_kind": "tow-truck", "available_units": "250"}],
            [DemandBucket("receiving-vault", "tow-truck", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = traffic_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["traffic_gap"], "30.000")

    def test_mark_to_risk_groups_deterministically(self) -> None:
        result = mark_to_risk(
            [{"position_id": "p1", "risk_index": "HUMIDITY", "quantity_units": "100", "baseline_value": "105"}],
            {"HUMIDITY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class CollectionLogisticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
        self.service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
        self.service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def risk_record(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{day}", "index_value": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_risk_record_revisions_preserve_history(self) -> None:
        first = self.risk_record(23, "98")
        second = self.service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": "2026-09-23", "index_value": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["risk_record_id"], second["risk_record_id"])
        rows = self.connection.execute("SELECT * FROM risk_index_risk_records ORDER BY risk_record_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_risk_record_id"], rows[0]["risk_record_id"])

    def test_dispatch_request_replay_and_payload_conflict(self) -> None:
        payload = {"dispatch_id": "nom-1", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_dispatch("dispatch", payload)
        self.assertEqual(first, self.service.submit_dispatch("dispatch", payload))
        changed = dict(payload, requested_units="81000")
        with self.assertRaises(Conflict):
            self.service.submit_dispatch("dispatch", changed)

    def test_outage_reduces_allocation_and_deployment_consumes_inventory(self) -> None:
        self.service.announce_restriction("risk", "transfer-east-1", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_dispatch("dispatch", {"dispatch_id": f"nom-{number}", "corridor_id": "transfer-east-1", "specimen_event_id": f"specimen_event-{number}", "duty_date": "2026-09-25", "requested_units": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")
        self.assertEqual(allocation["available_units"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_units"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "STANDARD", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", {"deployment_id": "deployment-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-key-1"})
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(deployment["lots"][0]["preservation_resource_lot_id"], "lot-1")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "STANDARD", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_pick_lots_fefo_spans_multiple_lots(self) -> None:
        picks, shortfall = pick_lots_fefo(
            Decimal("100"),
            [("lot-a", Decimal("30")), ("lot-b", Decimal("50")), ("lot-c", Decimal("40"))],
        )
        self.assertEqual(shortfall, Decimal("0.000"))
        self.assertEqual(picks, [("lot-a", Decimal("30.000")), ("lot-b", Decimal("50.000")), ("lot-c", Decimal("20.000"))])

    def _submit_and_allocate(self, dispatch_id: str, requested: str, grade: str, key: str) -> None:
        self.service.submit_dispatch("dispatch", {"dispatch_id": dispatch_id, "corridor_id": "transfer-east-1", "specimen_event_id": "spec-1", "duty_date": "2026-09-25", "requested_units": requested, "priority": 10, "required_grade": grade, "idempotency_key": key})
        self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")

    def _lot(self, lot_id: str, grade: str, quantity: str, **overrides: object) -> None:
        payload = {"preservation_resource_lot_id": lot_id, "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": grade, "quantity_units": quantity, "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"}
        payload.update(overrides)
        self.service.add_inventory_lot("dispatch", payload)

    def test_low_temp_task_rejects_standard_box_with_specific_constraint(self) -> None:
        self._submit_and_allocate("nom-alcohol", "40000", "LOW_TEMP", "key-alcohol")
        self._lot("paper-box", "STANDARD", "40000")
        with self.assertRaises(ResourceConstraint) as caught:
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-alcohol", "expected_revision": 2, "idempotency_key": "deploy-1"})
        details = caught.exception.details
        self.assertEqual(details["required_grade"], "LOW_TEMP")
        self.assertEqual(details["shortage_units"], "40000.000")
        rejected = details["rejected_lots"][0]
        self.assertEqual(rejected["preservation_resource_lot_id"], "paper-box")
        self.assertIn("preservation_grade", {item["constraint"] for item in rejected["violations"]})

    def test_low_temp_task_accepts_low_temp_box(self) -> None:
        self._submit_and_allocate("nom-alcohol", "40000", "LOW_TEMP", "key-alcohol")
        self._lot("cold-box", "LOW_TEMP", "40000")
        deployment = self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-alcohol", "expected_revision": 2, "idempotency_key": "deploy-1"})
        self.assertEqual(deployment["lots"][0]["preservation_resource_lot_id"], "cold-box")
        self.assertEqual(deployment["required_grade"], "LOW_TEMP")

    def test_standard_task_may_use_higher_grade_box(self) -> None:
        self._submit_and_allocate("nom-paper", "40000", "STANDARD", "key-paper")
        self._lot("cold-box", "LOW_TEMP", "40000")
        deployment = self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-paper", "expected_revision": 2, "idempotency_key": "deploy-1"})
        self.assertEqual(deployment["lots"][0]["preservation_resource_lot_id"], "cold-box")

    def test_lot_outside_origin_center_cannot_ship(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("far-box", "STANDARD", "40000", center_id="receiving-vault-b")
        with self.assertRaises(ResourceConstraint) as caught:
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        constraints = {item["constraint"] for item in caught.exception.details["rejected_lots"][0]["violations"]}
        self.assertIn("origin_center", constraints)

    def test_expired_lot_is_rejected(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("stale-box", "STANDARD", "40000", expires_at="2026-09-20", received_at="2026-01-01T00:00:00Z")
        with self.assertRaises(ResourceConstraint) as caught:
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        constraints = {item["constraint"] for item in caught.exception.details["rejected_lots"][0]["violations"]}
        self.assertIn("expiry", constraints)

    def test_non_available_lot_is_rejected(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("held-box", "STANDARD", "40000", state="quarantined")
        with self.assertRaises(ResourceConstraint) as caught:
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        constraints = {item["constraint"] for item in caught.exception.details["rejected_lots"][0]["violations"]}
        self.assertIn("lot_state", constraints)

    def test_multi_lot_fefo_combines_nearest_expiry_first(self) -> None:
        self._submit_and_allocate("nom-1", "60000", "STANDARD", "key-1")
        self._lot("fresh", "STANDARD", "40000", expires_at="2027-01-01", received_at="2026-09-20T06:00:00Z")
        self._lot("near", "STANDARD", "40000", expires_at="2026-12-01", received_at="2026-09-21T06:00:00Z")
        deployment = self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        chosen = [row["preservation_resource_lot_id"] for row in deployment["lots"]]
        self.assertEqual(chosen, ["near", "fresh"])
        self.assertEqual(deployment["lots"][0]["allocated_units"], "40000.000")
        self.assertEqual(deployment["lots"][1]["allocated_units"], "20000.000")
        self.assertEqual(self.service.inventory_lot("near")["available_units"], "0.000")
        self.assertEqual(self.service.inventory_lot("near")["state"], "depleted")
        self.assertEqual(self.service.inventory_lot("fresh")["available_units"], "20000.000")

    def test_retry_same_request_is_idempotent_and_does_not_deduct_twice(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("box-1", "STANDARD", "40000")
        command = {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"}
        first = self.service.dispatch_deployment("dispatch", command)
        second = self.service.dispatch_deployment("dispatch", command)
        self.assertEqual(first, second)
        self.assertEqual(self.service.inventory_lot("box-1")["available_units"], "0.000")
        deployments = self.connection.execute("SELECT COUNT(*) c FROM deployments").fetchone()["c"]
        self.assertEqual(deployments, 1)
        detail_rows = self.connection.execute("SELECT COUNT(*) c FROM deployment_lot_allocations").fetchone()["c"]
        self.assertEqual(detail_rows, 1)

    def test_idempotency_key_with_different_payload_conflicts(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("box-1", "STANDARD", "40000")
        self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        with self.assertRaises(Conflict):
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-2", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})

    def test_failed_match_rolls_back_all_inventory_changes(self) -> None:
        self._submit_and_allocate("nom-1", "60000", "STANDARD", "key-1")
        self._lot("box-1", "STANDARD", "30000")
        self._lot("box-2", "STANDARD", "20000")
        with self.assertRaises(ResourceConstraint):
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        self.assertEqual(self.service.inventory_lot("box-1")["available_units"], "30000")
        self.assertEqual(self.service.inventory_lot("box-2")["available_units"], "20000")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) c FROM deployments").fetchone()["c"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) c FROM traffic_audit_events WHERE event_type='deployment.dispatched'").fetchone()["c"], 0)

    def test_concurrent_deployments_only_one_succeeds(self) -> None:
        import tempfile
        import threading
        from pathlib import Path
        from collection_logistics.storage import connect
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.sqlite3"
            connection_a = connect(db_path)
            connection_b = connect(db_path)
            service_a = CollectionLogisticsService(connection_a, self.clock)
            service_b = CollectionLogisticsService(connection_b, self.clock)
            for user_id, role in (("dispatch", "dispatcher"), ("plan", "planner"), ("risk", "risk"), ("audit", "auditor")):
                try:
                    service_a.create_user(user_id, user_id, role)
                except Conflict:
                    pass
            service_a.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
            service_a.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
            service_a.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})
            service_a.submit_dispatch("dispatch", {"dispatch_id": "nom-a", "corridor_id": "transfer-east-1", "specimen_event_id": "spec-a", "duty_date": "2026-09-25", "requested_units": "30000", "priority": 10, "required_grade": "STANDARD", "idempotency_key": "key-a"})
            service_a.submit_dispatch("dispatch", {"dispatch_id": "nom-b", "corridor_id": "transfer-east-1", "specimen_event_id": "spec-b", "duty_date": "2026-09-25", "requested_units": "30000", "priority": 20, "required_grade": "STANDARD", "idempotency_key": "key-b"})
            service_a.allocate("dispatch", "transfer-east-1", "2026-09-25")
            service_a.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "box-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "STANDARD", "quantity_units": "40000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
            errors: list[Exception] = []

            def run(service: CollectionLogisticsService, dispatch_id: str, deployment_id: str, key: str) -> None:
                try:
                    service.dispatch_deployment("dispatch", {"deployment_id": deployment_id, "dispatch_id": dispatch_id, "expected_revision": 2, "idempotency_key": key})
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(service_a, "nom-a", "dep-a", "deploy-a")),
                threading.Thread(target=run, args=(service_b, "nom-b", "dep-b", "deploy-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            # 两单共需 60000 而兼容库存 40000：写锁串行化后恰好一单成功，一单因库存不足回滚。
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ResourceConstraint)
            self.assertEqual(service_a.inventory_lot("box-1")["available_units"], "10000.000")
            succeeded = connection_a.execute("SELECT COUNT(*) c FROM deployments").fetchone()["c"]
            self.assertEqual(succeeded, 1)
            connection_a.close()
            connection_b.close()

    def test_audit_records_selection_basis_quantity_changes_and_actor(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("box-1", "STANDARD", "40000", expires_at="2027-01-01")
        self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"})
        import json as _json
        row = self.connection.execute(
            "SELECT actor_id,payload_json FROM traffic_audit_events WHERE event_type='deployment.dispatched' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        self.assertEqual(row["actor_id"], "dispatch")
        self.assertEqual(payload["policy"], "FEFO_NEAREST_EXPIRY")
        self.assertEqual(payload["requirements"]["required_grade"], "STANDARD")
        change = payload["lot_changes"][0]
        self.assertEqual(change["preservation_resource_lot_id"], "box-1")
        self.assertEqual(change["available_before_units"], "40000.000")
        self.assertEqual(change["allocated_units"], "40000.000")
        self.assertEqual(change["available_after_units"], "0.000")
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_stale_revision_is_rejected_before_inventory_change(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "STANDARD", "key-1")
        self._lot("box-1", "STANDARD", "40000")
        with self.assertRaises(InvalidState):
            self.service.dispatch_deployment("dispatch", {"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 1, "idempotency_key": "deploy-1"})
        self.assertEqual(self.service.inventory_lot("box-1")["available_units"], "40000")

    def test_api_returns_constraint_details(self) -> None:
        self._submit_and_allocate("nom-1", "40000", "LOW_TEMP", "key-1")
        self._lot("paper-box", "STANDARD", "40000")
        app = JsonApplication(self.service)
        response = app.handle("POST", "/deployments", {"X-Actor-Id": "dispatch", "Content-Type": "application/json"}, json.dumps({"deployment_id": "dep-1", "dispatch_id": "nom-1", "expected_revision": 2, "idempotency_key": "deploy-1"}).encode())
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "resource_constraint")
        self.assertIn("details", response.body["error"])
        self.assertEqual(response.body["error"]["details"]["required_grade"], "LOW_TEMP")

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE traffic_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/risk_records/summary/HUMIDITY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
