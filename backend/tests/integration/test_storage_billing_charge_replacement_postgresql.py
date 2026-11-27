"""中文: 行为测试以真实 PostgreSQL 验证存储费用替换及事务边界。

EN: Behavioral: real PostgreSQL storage-billing replacement and transaction tests.

Only official-provider I/O and host configuration/entitlement inputs are fictional.
The service, profile gates, allocation, snapshots, ORM, constraints and readback
are real. Run only against the explicitly named disposable loopback database.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.base_model import Base
from app.models.system.plugin import Plugin
from app.plugins.module_loader import load_plugin_module

service_module = load_plugin_module(
    "storage-billing", "services.reconciliation_service"
)
models = importlib.import_module("plugins.storage-billing.backend.models")
providers = importlib.import_module("plugins.storage-billing.backend.providers")

Service = service_module.StorageBillingReconciliationService
Binding = models.StorageTenantBinding
Run = models.StorageBillingRun
Source = models.StorageProviderBillSource
Charge = models.StorageTenantDailyCharge
Statement = models.StorageTenantStatement

DSN = os.getenv("PLATFORM_FIXTURE_POSTGRES_DSN")
if DSN:
    fixture_url = make_url(DSN)
    if (
        fixture_url.drivername != "postgresql+asyncpg"
        or fixture_url.host not in {"localhost", "127.0.0.1", "::1"}
        or fixture_url.port is None
        or fixture_url.database != "platform_tasks"
        or fixture_url.username != "audit_fixture"
        or fixture_url.query
    ):
        raise ValueError("The disposable platform_tasks loopback database is required")

pytestmark = [
    pytest.mark.behavioral,
    pytest.mark.asyncio,
    pytest.mark.skipif(not DSN, reason="Disposable PostgreSQL required"),
]

PROVIDER = "tencent-cos"
DAY = date(2026, 3, 21)
TENANT = 7701
LEDGER_MODELS = {
    "runs": Run,
    "sources": Source,
    "charges": Charge,
    "statements": Statement,
}


class FictionalHost:
    def __init__(self, official_bill):
        self._official_bill = official_bill

    async def execute_storage_billing_fetch(self, request):
        return await self._official_bill.fetch_official_bill(
            providers.BillingFetchRequest(**request)
        )

    async def get_platform_storage_context(self):
        return {
            "storage_mode": "platform",
            "storage_config": {
                "driver": PROVIDER,
                "options": {
                    "region": "fixture-region",
                    "secret_id": "fictional-input-id",
                    "secret_key": "fictional-input-key",
                },
            },
        }

    async def get_enabled_storage_drivers(self):
        return [{"code": PROVIDER, "is_available": True, "plugin_status": "enabled"}]

    async def get_tenant_plan_snapshot(self, tenant_id):
        return {
            "tenant_id": tenant_id,
            "plan": {
                "features": {"storage_billing_enabled": tenant_id in {7701, 7702}}
            },
        }

    async def get_tenant_storage_context(self, _tenant_id):
        return {"storage_mode": "platform", "storage_config": {"driver": PROVIDER}}


def _item(amount="12.340001", usage=1234, bucket="fictional-bucket-a"):
    return providers.BillingChargeItem(
        charge_basis="egress_traffic",
        amount_total=Decimal(amount),
        usage_bytes=usage,
        bucket_name=bucket,
    )


@dataclass
class FictionalOfficialBillIO:
    bills: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def fetch_official_bill(self, request):
        self.calls.append((request.period_type, request.billing_date))
        items = self.bills.get(request.billing_date, [_item()])
        return providers.BillingFetchResult(
            provider_code=PROVIDER,
            driver_code=PROVIDER,
            billing_date=request.billing_date,
            period_type=request.period_type,
            period_start=request.period_start,
            period_end=request.period_end,
            source_status="fetched" if items else "empty",
            amount_total=sum((item.amount_total for item in items), Decimal("0")),
            usage_bytes=sum(item.usage_bytes for item in items),
            charge_items=list(items),
            raw_payload_json={"fictional_provider_input": True},
        )


@dataclass
class LedgerFixture:
    sessions: async_sessionmaker
    official_bill: FictionalOfficialBillIO

    async def run(self, day=DAY, *, sessions=None):
        factory = sessions if sessions is not None else self.sessions
        async with factory() as db:
            result = await Service(
                db, host_read=FictionalHost(self.official_bill), operator_id=8801
            ).run_daily_reconciliation(day)
            await db.commit()
            return result

    async def state(self):
        # 中文: 用独立会话验证持久化，避免仅断言内存中的 ORM 状态。
        # EN: Use a new session; identity-map assertions cannot prove commit.
        async with self.sessions() as db:
            return {
                name: [
                    dict(row)
                    for row in (
                        await db.execute(select(model.__table__).order_by(model.id))
                    ).mappings()
                ]
                for name, model in LEDGER_MODELS.items()
            }


@pytest_asyncio.fixture
async def ledger():
    schema = "storage_billing_replacement_" + uuid4().hex
    admin_engine = create_async_engine(str(DSN))
    engine = None
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
        engine = create_async_engine(
            str(DSN),
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "application_name": "storage-billing-replacement-behavioral-fixture",
                    "statement_timeout": "10000",
                    "lock_timeout": "5000",
                }
            },
        )
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=[
                        Plugin.__table__,
                        Binding.__table__,
                        *[model.__table__ for model in LEDGER_MODELS.values()],
                    ],
                )
            )
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with sessions() as db:
            db.add(
                Plugin(
                    name="storage-billing",
                    display_name="Fictional billing fixture",
                    version="0.0.0",
                    scope="platform",
                    status="enabled",
                    config={"providers": {PROVIDER: {"enabled": True}}},
                    manifest={},
                )
            )
            for tenant, bucket in [(7701, "a"), (7702, "b"), (7703, "ineligible")]:
                db.add(
                    Binding(
                        tenant_id=tenant,
                        provider_code=PROVIDER,
                        driver_code=PROVIDER,
                        provider_profile_code="tencent-default",
                        scope_type="bucket",
                        scope_value="fictional-bucket-" + bucket,
                        bucket_name="fictional-bucket-" + bucket,
                        validation_status="valid",
                        is_active=True,
                    )
                )
            await db.commit()
        official_bill = FictionalOfficialBillIO()
        yield LedgerFixture(sessions, official_bill)
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await admin_engine.dispose()


async def _seed_previous_ledger(
    ledger,
    *,
    day=DAY,
    provider=PROVIDER,
    tenant=TENANT,
    period="daily",
    amount="3.210009",
):
    """中文: 直接保存真实历史账本，以独立覆盖已有费用的替换路径。

    EN: Seed a real historical ledger to exercise existing-row replacement.
    """
    async with ledger.sessions() as db:
        run = Run(
            period_type=period,
            billing_date=day,
            period_start=day,
            period_end=day,
            status="completed",
            provider_codes_json=[provider],
        )
        db.add(run)
        await db.flush()
        source = Source(
            run_id=run.id,
            provider_code=provider,
            driver_code=provider,
            period_type=period,
            billing_date=day,
            period_start=day,
            period_end=day,
            source_status="fetched",
            amount_total=Decimal(amount),
            usage_bytes=99,
            raw_payload_json={"historical_fixture": True},
        )
        db.add(source)
        await db.flush()
        statement = await db.scalar(
            select(Statement).where(
                Statement.tenant_id == tenant,
                Statement.period_type == period,
                Statement.billing_date == day,
            )
        )
        if statement is None:
            statement = Statement(
                tenant_id=tenant,
                period_type=period,
                billing_date=day,
                period_start=day,
                period_end=day,
                status="generated",
                amount_total=Decimal("0"),
                charge_count=0,
            )
            db.add(statement)
            await db.flush()
        statement.amount_total += Decimal(amount)
        statement.charge_count += 1
        charge = Charge(
            tenant_id=tenant,
            period_type=period,
            billing_date=day,
            period_start=day,
            period_end=day,
            provider_code=provider,
            driver_code=provider,
            charge_basis="egress_traffic",
            usage_bytes=99,
            amount_total=Decimal(amount),
            source_id=source.id,
            statement_id=statement.id,
            details_json={"historical_fixture": True},
        )
        db.add(charge)
        await db.commit()
        return {
            "run": run.id,
            "source": source.id,
            "charge": charge.id,
            "statement": statement.id,
        }


def _assert_one_current_charge(state, *, amount, usage, source_id):
    assert len(state["charges"]) == 1
    charge = state["charges"][0]
    assert (
        charge["tenant_id"],
        charge["period_type"],
        charge["billing_date"],
        charge["provider_code"],
        charge["driver_code"],
        charge["charge_basis"],
        charge["amount_total"],
        charge["usage_bytes"],
        charge["currency"],
        charge["source_id"],
    ) == (
        TENANT,
        "daily",
        DAY,
        PROVIDER,
        PROVIDER,
        "egress_traffic",
        Decimal(amount),
        usage,
        "CNY",
        None,
    )
    assert charge["details_json"]["source_ids"] == [source_id]
    assert charge["details_json"]["scope_values"] == ["fictional-bucket-a"]
    assert len(state["statements"]) == 1
    statement = state["statements"][0]
    assert (
        statement["tenant_id"],
        statement["amount_total"],
        statement["charge_count"],
        statement["status"],
    ) == (TENANT, Decimal(amount), 1, "generated")
    assert statement["summary_json"]["total_usage_bytes"] == usage
    assert charge["statement_id"] == statement["id"]


async def test_first_nonempty_daily_run_commits_reconstructed_charge(ledger):
    """中文: 回归验证同一批次重建先删除再插入，避免唯一键冲突。

    EN: Regression: the run-scope rebuild flushes deletes before reinsertion.
    """
    result = await ledger.run()
    state = await ledger.state()
    assert result["run"]["status"] == "completed"
    assert len(state["runs"]) == len(state["sources"]) == 1
    source = state["sources"][0]
    assert source["run_id"] == state["runs"][0]["id"]
    assert source["amount_total"] == Decimal("12.340001")
    assert (
        source["raw_payload_json"]["allocation_rows"][0]["amount_total"] == "12.340001"
    )
    _assert_one_current_charge(
        state, amount="12.340001", usage=1234, source_id=source["id"]
    )
    assert ledger.official_bill.calls == [("daily", DAY)]


async def test_seeded_live_charge_is_replaced_without_unique_key_collision(ledger):
    """中文: 回归验证来源分摊路径也先删除已有费用再写入替换行。

    EN: Regression: source allocation also flushes old live-row deletes first.
    """
    old = await _seed_previous_ledger(ledger)
    before = await ledger.state()
    await ledger.run()
    state = await ledger.state()
    assert len(state["runs"]) == len(state["sources"]) == 2
    assert state["sources"][0] == before["sources"][0]
    assert state["charges"][0]["id"] != old["charge"]
    assert state["statements"][0]["id"] == old["statement"]
    _assert_one_current_charge(
        state, amount="12.340001", usage=1234, source_id=state["sources"][1]["id"]
    )


async def test_unchanged_and_changed_reruns_keep_history_and_exact_current_totals(
    ledger,
):
    await ledger.run()
    first = await ledger.state()
    await ledger.run()
    second = await ledger.state()
    _assert_one_current_charge(
        second, amount="12.340001", usage=1234, source_id=second["sources"][1]["id"]
    )
    ledger.official_bill.bills[DAY] = [_item("98.765432", 4321)]
    await ledger.run()
    third = await ledger.state()
    assert len(third["runs"]) == len(third["sources"]) == 3
    assert third["runs"][:2] == second["runs"]
    assert third["sources"][:2] == second["sources"]
    assert third["sources"][0] == first["sources"][0]
    assert third["statements"][0]["id"] == first["statements"][0]["id"]
    _assert_one_current_charge(
        third, amount="98.765432", usage=4321, source_id=third["sources"][2]["id"]
    )


@pytest.mark.parametrize("has_previous", [False, True])
async def test_empty_bill_commits_history_and_clears_only_current_live_rows(
    ledger, has_previous
):
    if has_previous:
        await _seed_previous_ledger(ledger)
    ledger.official_bill.bills[DAY] = []
    result = await ledger.run()
    state = await ledger.state()
    expected_count = 2 if has_previous else 1
    assert len(state["runs"]) == len(state["sources"]) == expected_count
    assert state["sources"][-1]["source_status"] == "empty"
    assert state["sources"][-1]["amount_total"] == Decimal("0.000000")
    assert state["sources"][-1]["raw_payload_json"]["allocation_rows"] == []
    assert state["charges"] == state["statements"] == []
    assert result["run"]["status"] == "completed"


async def test_empty_replacement_preserves_other_provider_date_period_and_tenant(
    ledger,
):
    await _seed_previous_ledger(ledger)
    other_provider = await _seed_previous_ledger(
        ledger, provider="qiniu-kodo", amount="1.000003"
    )
    other_day = await _seed_previous_ledger(ledger, day=DAY + timedelta(days=1))
    other_period = await _seed_previous_ledger(ledger, period="monthly")
    other_tenant = await _seed_previous_ledger(
        ledger, provider="qiniu-kodo", tenant=7702
    )
    before = await ledger.state()
    preserved_ids = {
        row["charge"] for row in [other_provider, other_day, other_period, other_tenant]
    }
    ledger.official_bill.bills[DAY] = []
    await ledger.run()
    state = await ledger.state()
    assert state["charges"] == [
        row for row in before["charges"] if row["id"] in preserved_ids
    ]
    assert state["sources"][:-1] == before["sources"]
    assert state["runs"][:-1] == before["runs"]
    assert len(state["statements"]) == 4
    current = next(
        row for row in state["statements"] if row["id"] == other_provider["statement"]
    )
    assert (
        current["amount_total"],
        current["charge_count"],
        current["summary_json"]["provider_codes"],
    ) == (Decimal("1.000003"), 1, ["qiniu-kodo"])
    for prior in before["statements"]:
        if prior["period_type"] != "daily" or prior["billing_date"] != DAY:
            assert prior in state["statements"]


async def test_real_allocation_preserves_tenant_entitlement_and_six_decimal_amounts(
    ledger,
):
    ledger.official_bill.bills[DAY] = [
        _item("12.340001", 1234),
        _item("0.000007", 7, "fictional-bucket-b"),
        _item("8.000009", 9, "fictional-bucket-ineligible"),
    ]
    result = await ledger.run()
    state = await ledger.state()
    assert result["run"]["status"] == "completed_with_gaps"
    assert sorted(
        (row["tenant_id"], row["amount_total"], row["usage_bytes"])
        for row in state["charges"]
    ) == [
        (7701, Decimal("12.340001"), 1234),
        (7702, Decimal("0.000007"), 7),
    ]
    assert sorted(
        (row["tenant_id"], row["amount_total"], row["charge_count"])
        for row in state["statements"]
    ) == [
        (7701, Decimal("12.340001"), 1),
        (7702, Decimal("0.000007"), 1),
    ]
    source = state["sources"][0]
    assert source["amount_total"] == Decimal("20.340017")
    assert source["raw_payload_json"]["allocation_summary"] == {
        "matched_items": 2,
        "unmatched_items": 1,
        "ambiguous_items": 0,
        "written_charge_rows": 2,
    }


@pytest.mark.parametrize("has_previous", [False, True])
async def test_outer_failure_rolls_back_all_flushed_replacement_and_history(
    ledger, has_previous
):
    if has_previous:
        await _seed_previous_ledger(ledger)
    before = await ledger.state()
    with pytest.raises(RuntimeError, match="fictional failure after reconciliation"):
        async with ledger.sessions() as db, db.begin():
            result = await Service(
                db, host_read=FictionalHost(ledger.official_bill)
            ).run_daily_reconciliation(DAY)
            assert result["run"]["status"] == "completed"
            raise RuntimeError("fictional failure after reconciliation")
    assert await ledger.state() == before


def _concurrent_sessions(ledger):
    barrier = asyncio.Barrier(2)

    class FirstChargeReadBarrierSession(AsyncSession):
        """中文: 仅在真实首次查询后同步执行时序，不改变查询或结果。

        EN: Synchronize after the real first SELECT without altering SQL/results.
        """

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", ())
            if not self.info.get("charge_read_barrier") and any(
                item.get("entity") is Charge for item in descriptions
            ):
                self.info["charge_read_barrier"] = True
                await asyncio.wait_for(barrier.wait(), timeout=8)
            return result

    return async_sessionmaker(
        ledger.sessions.kw["bind"],
        class_=FirstChargeReadBarrierSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def test_same_key_concurrency_rejects_one_transaction_then_whole_run_retry_succeeds(
    ledger,
):
    sessions = _concurrent_sessions(ledger)
    results = await asyncio.wait_for(
        asyncio.gather(
            ledger.run(sessions=sessions),
            ledger.run(sessions=sessions),
            return_exceptions=True,
        ),
        timeout=20,
    )
    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(successes) == len(failures) == 1
    assert isinstance(failures[0], IntegrityError)
    assert failures[0].orig.sqlstate == "23505"
    assert "uq_px_storage_billing_tenant_daily_charge_key" in str(failures[0].orig)
    committed = await ledger.state()
    assert len(committed["runs"]) == len(committed["sources"]) == 1
    _assert_one_current_charge(
        committed,
        amount="12.340001",
        usage=1234,
        source_id=committed["sources"][0]["id"],
    )
    await ledger.run()
    retried = await ledger.state()
    assert len(retried["runs"]) == len(retried["sources"]) == 2
    assert retried["sources"][0] == committed["sources"][0]
    _assert_one_current_charge(
        retried, amount="12.340001", usage=1234, source_id=retried["sources"][1]["id"]
    )


async def test_distinct_date_concurrency_commits_independent_exact_statements(ledger):
    other_day = DAY + timedelta(days=1)
    ledger.official_bill.bills[other_day] = [_item("23.456789", 2345)]
    sessions = _concurrent_sessions(ledger)
    results = await asyncio.wait_for(
        asyncio.gather(
            ledger.run(sessions=sessions), ledger.run(other_day, sessions=sessions)
        ),
        timeout=20,
    )
    assert [item["run"]["status"] for item in results] == ["completed", "completed"]
    state = await ledger.state()
    assert len(state["runs"]) == len(state["sources"]) == 2
    assert sorted(
        (row["billing_date"], row["amount_total"], row["usage_bytes"])
        for row in state["charges"]
    ) == [
        (DAY, Decimal("12.340001"), 1234),
        (other_day, Decimal("23.456789"), 2345),
    ]
    assert sorted(
        (row["billing_date"], row["amount_total"], row["charge_count"])
        for row in state["statements"]
    ) == [
        (DAY, Decimal("12.340001"), 1),
        (other_day, Decimal("23.456789"), 1),
    ]
