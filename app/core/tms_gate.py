"""TMS 写通道（2026-09-10 拍板：TMS 侧不支持并发写；2026-09-11 下沉 core 并覆盖全部写链路）。

TMS 的业务编号生成非原子（查 max+1 后插入），并发写请求会撞唯一约束
（Duplicate entry）——服务侧可异步接收多个导入批次，但对 TMS 的全部写调用
必须共用同一条串行通道：宽度恒为 1，任意时刻至多 1 个写请求在飞（串行是
TMS 侧硬约束，不设可配置项防误配撞唯一约束）。

消费点（新增 TMS 写调用时必须一并接入本通道）：
- orders.bill.submission.client：账单下单提交段（_create_downstream_slots 即本通道）；
- orders.bill.master_data.client.create_archives_async：建档/费目自举每次 POST；
- orders.text.client.publish_create_order_async：自由文本下单（与 AddWork 同 s3 域）；
- orders.manifest.submission.client.submit_manifest_async：舱单 addBill。
"""

from __future__ import annotations

import asyncio

#: 全局唯一 TMS 写通道（进程级单例；测试经 conftest 重建防跨事件循环遗留）；
#: 宽度恒 1（写必须全串行，非可调参数）
tms_write_slots = asyncio.Semaphore(1)
