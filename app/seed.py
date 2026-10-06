"""演示种子数据：覆盖临界出发、到场争议、已付款结算、改判、退款重试等场景。"""
from __future__ import annotations

from app.clock import now_ms
from app.database import Base, SessionLocal, engine
from app.models import PricingRule
from app import services

MIN = 60_000


def reset_and_seed() -> dict:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    db = SessionLocal()
    t0 = now_ms()
    try:
        rule = PricingRule(
            name="2026 标准版（提前2小时免费取消）",
            free_cancel_before_minutes=120,
            departed_fee_percent=30,
            arrived_fee_percent=60,
            min_fee_cents=2000,
            max_fee_cents=0,
            created_at_ms=t0,
        )
        db.add(rule)
        db.flush()

        def make(code, name, start_in_min, cents, paid):
            return services.create_order(
                db, customer_name=name, service_start_at_ms=t0 + start_in_min * MIN,
                total_cents=cents, paid=paid, rule=rule, now_ms=t0, code=code,
            )

        # 1) 未出发：客户可自行预览取消（距开始 60 分钟 → 若先出发则 30%）
        make("OD-RACE", "临界时序·待演示", 60, 20000, True)

        # 2) 已出发未到场、已付款
        o2 = make("OD-DEPART", "王女士（已出发）", 60, 20000, True)
        services.dispatch(db, o2, now_ms=t0 - 5 * MIN)

        # 3) 已到场、已付款
        o3 = make("OD-ARRIVE", "李先生（已到场）", 60, 30000, True)
        services.dispatch(db, o3, now_ms=t0 - 20 * MIN)
        services.arrive(db, o3, lat=31.2308, lng=121.4739, now_ms=t0 - 8 * MIN)

        # 4) 距开始 3 小时，免费取消窗口内
        make("OD-FREE", "赵女士（免费窗口内）", 180, 20000, True)

        # 5) 已出发、未付款
        o5 = make("OD-UNPAID", "陈先生（未付款）", 60, 20000, False)
        services.dispatch(db, o5, now_ms=t0 - 5 * MIN)

        # 6) 到场争议：签到定位在围栏外（约400m），系统未确认到场；
        #    服务员声称已到场，客户否认 —— 交客服核对签到与定位后改判
        o6 = make("OD-DISPUTE", "周女士（到场争议）", 60, 20000, True)
        services.dispatch(db, o6, now_ms=t0 - 15 * MIN)
        services.arrive(db, o6, lat=31.2340, lng=121.4737, now_ms=t0 - 6 * MIN,
                        note="服务员签到，定位距服务地址约400米，围栏外")

        # 7) 已取消、已退款：演示重复取消去重与退款重试不重复退款
        o7 = make("OD-CANCELED", "吴女士（已取消已退款）", 60, 20000, True)
        services.dispatch(db, o7, now_ms=t0 - 12 * MIN)
        services.confirm_cancel(db, o7, reason="客户临时取消（已结算样例）",
                                now_ms=t0 - 4 * MIN)

        # 8) 已取消并经客服改判：初判“已出发30%”，改判“已到场60%”，
        #    原金额保留、差额补收单追加，双方可查看
        o8 = make("OD-REVIEWED", "郑先生（已改判补收）", 60, 20000, True)
        services.dispatch(db, o8, now_ms=t0 - 14 * MIN)
        services.confirm_cancel(db, o8, reason="客户电话取消", now_ms=t0 - 6 * MIN)
        services.agent_review(
            db, o8, new_stage="arrived",
            reason="核对服务员端补传的签到照片与定位轨迹（围栏内 80m），确认取消前已到场。",
            agent="客服001", now_ms=t0 - 2 * MIN,
        )

        db.commit()
        return {"seeded": True, "at_ms": t0,
                "codes": ["OD-RACE", "OD-DEPART", "OD-ARRIVE", "OD-FREE",
                          "OD-UNPAID", "OD-DISPUTE", "OD-CANCELED", "OD-REVIEWED"]}
    finally:
        db.close()
