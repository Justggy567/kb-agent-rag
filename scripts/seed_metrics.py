"""把语义层指标写进数据库（metrics 表），运行时从库里读，改指标不用改代码。

用法：python -m scripts.seed_metrics
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.semantic.metrics import METRICS  # noqa: E402

import asyncpg  # noqa: E402


async def main() -> None:
    conn = await asyncpg.connect(settings.dsn)
    for m in METRICS:
        await conn.execute(
            """
            INSERT INTO metrics (code, name, aliases, domain, expr, unit,
                                 chart_hint, allowed_dims, description)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                aliases = EXCLUDED.aliases,
                domain = EXCLUDED.domain,
                expr = EXCLUDED.expr,
                unit = EXCLUDED.unit,
                chart_hint = EXCLUDED.chart_hint,
                allowed_dims = EXCLUDED.allowed_dims,
                description = EXCLUDED.description,
                updated_at = now()
            """,
            m.code,
            m.name,
            json.dumps([m.name, *m.aliases], ensure_ascii=False),
            m.domain,
            m.expr,
            m.unit,
            m.chart_hint,
            json.dumps(m.allowed_dims, ensure_ascii=False),
            m.description,
        )
        print(f"✓ {m.code:<16} {m.name}")

    total = await conn.fetchval("SELECT count(*) FROM metrics")
    print(f"共 {total} 个指标")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
