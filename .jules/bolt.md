## 2026-10-16 - Prevent SQLAlchemy `count()` Subquery Generation
**Learning:** In SQLAlchemy, calling `.count()` on a query like `db.query(Model).count()` or `db.query(Model.id).count()` generates a subquery (e.g., `SELECT count(*) FROM (SELECT id FROM ...)`), which is highly inefficient for large tables.
**Action:** When performing simple counts, always use `db.query(func.count(Model.id)).scalar()` to force a direct `SELECT count(id)` query and avoid subquery overhead. Use `.scalar() or 0` to defend against unexpected `None` returns, though `COUNT` natively returns integers.
