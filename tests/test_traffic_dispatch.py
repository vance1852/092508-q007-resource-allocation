from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from collection_logistics.api import JsonApplication
from collection_logistics.clock import FrozenClock
from collection_logistics.errors import Conflict, Forbidden
from collection_logistics.planning import (
    AllocationRequest,
    RiskPoint,
    allocate_capacity,
    latest_streak,
    lot_eligibility_violations,
    sequence_lot_deductions,
)
from collection_logistics.service import CollectionLogisticsService
from collection_logistics.storage import initialize
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
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        deployment = self.service.dispatch_deployment("dispatch", "deployment-1", "nom-1", "lot-1", 2)
        self.assertEqual(deployment["deployed_units"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_units"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.risk_record(23, "98")
        self.service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-1", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "库房环境恢复", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

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


def _bootstrap(service: CollectionLogisticsService) -> None:
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    service.create_facility("plan", {"center_id": "collection-east", "name": "北部标本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
    service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
    service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})


class DeploymentResourceMatchingTests(unittest.TestCase):
    """调拨发料的资源匹配、扣减一致性、失败回滚、重试与审计。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        _bootstrap(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def _submit_and_allocate(self, dispatch_id: str, requested: str, required_grade: str | None = None) -> None:
        payload = {
            "dispatch_id": dispatch_id,
            "corridor_id": "transfer-east-1",
            "specimen_event_id": f"evt-{dispatch_id}",
            "duty_date": "2026-09-25",
            "requested_units": requested,
            "priority": 10,
            "idempotency_key": f"key-{dispatch_id}",
        }
        if required_grade is not None:
            payload["required_grade"] = required_grade
        self.service.submit_dispatch("dispatch", payload)
        self.service.allocate("dispatch", "transfer-east-1", "2026-09-25")

    def _lot(self, lot_id: str, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "preservation_resource_lot_id": lot_id,
            "center_id": "collection-east",
            "preservation_resource_kind": "preservation-box",
            "grade": "COLD-CHAIN",
            "quantity_units": "40000",
            "unit_cost_cny": "10",
            "received_at": "2026-09-24T06:00:00Z",
        }
        payload.update(overrides)
        return self.service.add_inventory_lot("dispatch", payload)

    def test_incompatible_lots_are_rejected_with_specific_constraints(self) -> None:
        self._submit_and_allocate("nom-cold", "40000", required_grade="COLD-CHAIN")
        self._lot("lot-wrong-kind", preservation_resource_kind="tow-truck")
        self._lot("lot-wrong-grade", grade="PLAIN")
        self._lot("lot-wrong-center", center_id="receiving-vault-b")
        self._lot("lot-expired", expires_at="2026-09-24T23:59:59Z")
        self._lot("lot-quarantined", state="quarantined")
        cases = {
            "lot-wrong-kind": "材料类别",
            "lot-wrong-grade": "保藏等级",
            "lot-wrong-center": "起运库房",
            "lot-expired": "有效期",
            "lot-quarantined": "可用状态",
        }
        for lot_id, keyword in cases.items():
            with self.assertRaises(Conflict) as caught:
                self.service.dispatch_deployment("dispatch", f"dep-{lot_id}", "nom-cold", lot_id, 2)
            self.assertIn(keyword, str(caught.exception))
            self.assertIn(lot_id, str(caught.exception))
            self.assertTrue(caught.exception.details["violations"])
            self.assertEqual(self.service.inventory_lot(lot_id)["available_units"], "40000")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deployments").fetchone()[0], 0)
        row = self.connection.execute("SELECT state,revision FROM dispatch_requests WHERE dispatch_id='nom-cold'").fetchone()
        self.assertEqual((row["state"], row["revision"]), ("allocated", 2))
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_multi_lot_combination_retry_and_conflicting_reuse(self) -> None:
        self._submit_and_allocate("nom-multi", "40000")
        self._lot("lot-a", quantity_units="30000")
        self._lot("lot-b", quantity_units="20000", expires_at="2026-09-26T00:00:00Z")
        first = self.service.dispatch_deployment("dispatch", "dep-multi", "nom-multi", ["lot-a", "lot-b"], 2)
        self.assertEqual(first["deployed_units"], "40000.000")
        self.assertEqual([item["preservation_resource_lot_id"] for item in first["lots"]], ["lot-a", "lot-b"])
        self.assertEqual(first["lots"][0]["deducted_units"], "30000.000")
        self.assertEqual(first["lots"][1]["deducted_units"], "10000.000")
        self.assertEqual(self.service.inventory_lot("lot-a")["available_units"], "0.000")
        self.assertEqual(self.service.inventory_lot("lot-b")["available_units"], "10000.000")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deployment_lots WHERE deployment_id='dep-multi'").fetchone()[0], 2)
        second = self.service.dispatch_deployment("dispatch", "dep-multi", "nom-multi", ["lot-a", "lot-b"], 2)
        self.assertEqual(first, second)
        self.assertEqual(self.service.inventory_lot("lot-b")["available_units"], "10000.000")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deployments").fetchone()[0], 1)
        with self.assertRaises(Conflict):
            self.service.dispatch_deployment("dispatch", "dep-multi", "nom-multi", ["lot-b", "lot-a"], 2)

    def test_insufficient_stock_reports_exact_quantities_and_failed_request_can_be_retried(self) -> None:
        self._submit_and_allocate("nom-short", "40000")
        self._lot("lot-small", quantity_units="15000")
        with self.assertRaises(Conflict) as caught:
            self.service.dispatch_deployment("dispatch", "dep-short", "nom-short", "lot-small", 2)
        self.assertIn("40000.000", str(caught.exception))
        self.assertIn("15000.000", str(caught.exception))
        self.assertEqual(caught.exception.details["required_units"], "40000.000")
        self.assertEqual(caught.exception.details["eligible_available_units"], "15000.000")
        self.assertEqual(self.service.inventory_lot("lot-small")["available_units"], "15000")
        self._lot("lot-topup", quantity_units="30000")
        result = self.service.dispatch_deployment("dispatch", "dep-short", "nom-short", ["lot-small", "lot-topup"], 2)
        self.assertEqual(result["deployed_units"], "40000.000")

    def test_failed_combination_rolls_back_everything(self) -> None:
        self._submit_and_allocate("nom-rb", "40000")
        self._lot("lot-good", quantity_units="30000")
        self._lot("lot-bad", preservation_resource_kind="tow-truck", quantity_units="30000")
        with self.assertRaises(Conflict):
            self.service.dispatch_deployment("dispatch", "dep-rb", "nom-rb", ["lot-good", "lot-bad"], 2)
        self.assertEqual(self.service.inventory_lot("lot-good")["available_units"], "30000")
        self.assertEqual(self.service.inventory_lot("lot-bad")["available_units"], "30000")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deployments").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM deployment_lots").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM traffic_idempotency WHERE scope='deployment'").fetchone()[0], 0)

    def test_concurrent_deductions_never_oversell(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        service = CollectionLogisticsService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        try:
            _bootstrap(service)
            for dispatch_id in ("nom-t1", "nom-t2"):
                service.submit_dispatch("dispatch", {"dispatch_id": dispatch_id, "corridor_id": "transfer-east-1", "specimen_event_id": f"evt-{dispatch_id}", "duty_date": "2026-09-25", "requested_units": "30000", "priority": 10, "idempotency_key": f"key-{dispatch_id}"})
            service.allocate("dispatch", "transfer-east-1", "2026-09-25")
            service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-shared", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "COLD-CHAIN", "quantity_units": "40000", "unit_cost_cny": "10", "received_at": "2026-09-24T06:00:00Z"})
            outcomes: list[object] = []

            def deploy(deployment_id: str, dispatch_id: str) -> None:
                try:
                    outcomes.append(service.dispatch_deployment("dispatch", deployment_id, dispatch_id, "lot-shared", 2))
                except Conflict as exc:
                    outcomes.append(exc)

            threads = [threading.Thread(target=deploy, args=("dep-t1", "nom-t1")), threading.Thread(target=deploy, args=("dep-t2", "nom-t2"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            successes = [item for item in outcomes if isinstance(item, dict)]
            failures = [item for item in outcomes if isinstance(item, Conflict)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIn("低于任务需求", str(failures[0]))
            self.assertEqual(service.inventory_lot("lot-shared")["available_units"], "10000.000")
            self.assertEqual(connection.execute("SELECT count(*) FROM deployments").fetchone()[0], 1)
        finally:
            connection.close()

    def test_successful_deployment_writes_reviewable_audit(self) -> None:
        self._submit_and_allocate("nom-audit", "40000")
        self._lot("lot-x", quantity_units="25000")
        self._lot("lot-y", quantity_units="25000")
        self.service.dispatch_deployment("dispatch", "dep-audit", "nom-audit", ["lot-x", "lot-y"], 2)
        row = self.connection.execute("SELECT actor_id,payload_json FROM traffic_audit_events WHERE entity_type='deployment' AND entity_id='dep-audit'").fetchone()
        self.assertEqual(row["actor_id"], "dispatch")
        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["requirements"]["origin_center_id"], "collection-east")
        self.assertEqual(payload["requirements"]["preservation_resource_kind"], "preservation-box")
        self.assertEqual(len(payload["lots"]), 2)
        first = payload["lots"][0]
        self.assertEqual(first["deducted_units"], "25000.000")
        self.assertEqual(first["previous_available_units"], "25000")
        self.assertEqual(first["remaining_available_units"], "0.000")
        self.assertIn("selection_basis", first)
        lot_events = self.connection.execute("SELECT event_type FROM traffic_audit_events WHERE entity_type='inventory_lot' AND entity_id='lot-x'").fetchall()
        self.assertIn("inventory.allocated", [event[0] for event in lot_events])
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_api_multi_lot_success_and_detailed_constraint_error(self) -> None:
        app = JsonApplication(self.service)
        self._submit_and_allocate("nom-api", "40000", required_grade="COLD-CHAIN")
        self._lot("lot-plain", grade="PLAIN")
        headers = {"X-Actor-Id": "dispatch"}
        bad = app.handle("POST", "/deployments", headers, json.dumps({"deployment_id": "dep-api-1", "dispatch_id": "nom-api", "preservation_resource_lot_ids": ["lot-plain"], "expected_revision": 2}).encode())
        self.assertEqual(bad.status, 409)
        self.assertIn("保藏等级", bad.body["error"]["message"])
        self.assertTrue(bad.body["error"]["details"]["violations"])
        self._lot("lot-cold", grade="COLD-CHAIN")
        ok = app.handle("POST", "/deployments", headers, json.dumps({"deployment_id": "dep-api-2", "dispatch_id": "nom-api", "preservation_resource_lot_ids": ["lot-cold"], "expected_revision": 2}).encode())
        self.assertEqual(ok.status, 201)
        self.assertEqual(ok.body["lots"][0]["preservation_resource_lot_id"], "lot-cold")
        replay = app.handle("POST", "/deployments", headers, json.dumps({"deployment_id": "dep-api-2", "dispatch_id": "nom-api", "preservation_resource_lot_ids": ["lot-cold"], "expected_revision": 2}).encode())
        self.assertEqual(replay.status, 201)
        self.assertEqual(replay.body, ok.body)

    def test_legacy_database_is_migrated(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("CREATE TABLE preservation_resource_lots(preservation_resource_lot_id TEXT PRIMARY KEY,center_id TEXT NOT NULL,preservation_resource_kind TEXT NOT NULL,grade TEXT NOT NULL,quantity_units TEXT NOT NULL,available_units TEXT NOT NULL,unit_cost_cny TEXT NOT NULL,received_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT NOT NULL,created_at TEXT NOT NULL)")
            connection.execute("CREATE TABLE dispatch_requests(dispatch_id TEXT PRIMARY KEY,corridor_id TEXT NOT NULL,specimen_event_id TEXT NOT NULL,duty_date TEXT NOT NULL,requested_units TEXT NOT NULL,allocated_units TEXT NOT NULL DEFAULT '0',arrived_units TEXT NOT NULL DEFAULT '0',priority INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'submitted',revision INTEGER NOT NULL DEFAULT 1,idempotency_key TEXT NOT NULL UNIQUE,submitted_by TEXT NOT NULL,submitted_at TEXT NOT NULL)")
            initialize(connection)
            lot_columns = {row[1] for row in connection.execute("PRAGMA table_info(preservation_resource_lots)")}
            self.assertIn("expires_at", lot_columns)
            self.assertIn("state", lot_columns)
            dispatch_columns = {row[1] for row in connection.execute("PRAGMA table_info(dispatch_requests)")}
            self.assertIn("required_grade", dispatch_columns)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("deployment_lots", tables)
        finally:
            connection.close()


class LotMatchingPlanningTests(unittest.TestCase):
    def test_lot_eligibility_violations_list_every_constraint(self) -> None:
        violations = lot_eligibility_violations(
            lot_id="lot-1",
            kind="paper-box",
            grade="PLAIN",
            center_id="collection-west",
            state="quarantined",
            expires_at="2026-09-24T00:00:00Z",
            required_kind="preservation-box",
            required_grade="COLD-CHAIN",
            origin_center_id="collection-east",
            duty_date="2026-09-25",
        )
        self.assertEqual(len(violations), 5)
        self.assertTrue(all("lot-1" in item for item in violations))

    def test_lot_eligibility_accepts_matching_lot_without_grade_requirement(self) -> None:
        violations = lot_eligibility_violations(
            lot_id="lot-2",
            kind="preservation-box",
            grade="ANY",
            center_id="collection-east",
            state="usable",
            expires_at=None,
            required_kind="preservation-box",
            required_grade=None,
            origin_center_id="collection-east",
            duty_date="2026-09-25",
        )
        self.assertEqual(violations, [])

    def test_sequence_lot_deductions_spills_in_order_and_detects_shortage(self) -> None:
        plan = sequence_lot_deductions([("a", Decimal("30000")), ("b", Decimal("20000"))], Decimal("40000"))
        self.assertEqual(plan, [("a", Decimal("30000.000")), ("b", Decimal("10000.000"))])
        self.assertIsNone(sequence_lot_deductions([("a", Decimal("10"))], Decimal("40000")))


if __name__ == "__main__":
    unittest.main()
