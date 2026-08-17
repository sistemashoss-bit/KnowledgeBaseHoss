from sqlalchemy import text
from app.database import engine

ACTIONS = ("login", "login_fail", "view_document")

with engine.begin() as c:
    rows = c.execute(
        text(
            "SELECT action, count(*) AS n FROM audit_logs "
            "WHERE action IN ('login','login_fail','view_document') "
            "GROUP BY action ORDER BY n DESC"
        )
    ).all()
    print("por accion:", [(r[0], r[1]) for r in rows])
    print("total a borrar:", sum(r[1] for r in rows))

    n = c.execute(
        text("DELETE FROM audit_logs WHERE action IN ('login','login_fail','view_document')")
    ).rowcount
    print("borradas:", n)

    remaining = c.execute(
        text("SELECT count(*) FROM audit_logs WHERE action IN ('login','login_fail','view_document')")
    ).scalar()
    print("restantes con esas acciones:", remaining)
