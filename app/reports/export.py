"""Exportación del "Resumen de Tareas" (reportería: pendientes y en progreso) a XLSX y PDF.

Recibe los grupos de `_open_tasks` (por departamento). Las fechas de comentario
vienen en UTC naive y se muestran en la hora local del negocio
(settings.local_utc_offset_hours), igual que el horario laboral.
"""
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.config import settings
from app.models import TASK_IN_PROGRESS

TITLE = "Resumen de Tareas — pendientes y en progreso"
HEADERS = [
    "Departamento", "Tarea", "Estado", "Responsable", "Vence", "Vencida",
    "Seguimientos", "Último comentario", "Comentado por", "Fecha comentario",
]
# ✓ hecho · ✗ no hecho (pasó su hora sin marcarse) · ○ próximo
FOLLOW_UP_MARKS = {"done": "✓", "missed": "✗", "upcoming": "○"}
LEGEND = "Seguimientos: ✓ completado · ✗ no completado · ○ próximo"
# DejaVu (licencia en app/assets/fonts/DejaVu-LICENSE.txt): las fuentes base de
# PDF solo cubren latin-1 y no tienen ✓ ✗ ○.
_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _local(dt):
    return dt + timedelta(hours=settings.local_utc_offset_hours) if dt else None


def _status(t) -> str:
    return "En progreso" if t["status"] == TASK_IN_PROGRESS else "Pendiente"


def _follow_up_lines(follow_ups: list[dict]) -> str:
    return "\n".join(f"{FOLLOW_UP_MARKS[f['state']]} {f['date'].strftime('%d/%m/%Y')}" for f in follow_ups)


def open_tasks_xlsx(groups: list[dict], subtitle: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Tareas abiertas"
    ws.append([TITLE])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"{subtitle} · {LEGEND}"])
    ws["A2"].font = Font(italic=True, color="666666")
    ws.append([])
    ws.append(HEADERS)
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F2937")
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for g in groups:
        for t in g["tasks"]:
            ws.append([
                g["department"], t["title"], _status(t), t["assignee"] or "",
                t["due_date"], "Sí" if t["overdue"] else "No",
                _follow_up_lines(t["follow_ups"]) or "No",
                t["last_comment"] or "", t["last_comment_by"] or "",
                _local(t["last_comment_at"]),
            ])
            row = ws.max_row
            ws.cell(row, 5).number_format = "DD/MM/YYYY"
            ws.cell(row, 10).number_format = "DD/MM/YYYY HH:MM"
            for col in (2, 7, 8):
                ws.cell(row, col).alignment = Alignment(wrap_text=True, vertical="top")
            if t["overdue"]:
                ws.cell(row, 5).font = Font(color="DC2626")

    widths = [22, 40, 13, 24, 12, 9, 16, 60, 24, 17]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(header_row + 1, 1)
    if ws.max_row > header_row:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(HEADERS))}{ws.max_row}"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_text(text: str) -> str:
    """DejaVu no trae emojis (fuera del plano básico): se omiten en vez de
    dejar huecos con advertencias de glifo faltante."""
    return "".join(ch for ch in (text or "") if ord(ch) <= 0xFFFF)


def _truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", size=8)
        self.set_text_color(150)
        self.cell(0, 8, f"Página {self.page_no()}/{{nb}}", align="R")


def open_tasks_pdf(groups: list[dict], subtitle: str) -> bytes:
    pdf = _PDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(10, 10, 10)
    pdf.add_font("DejaVu", "", _FONTS / "DejaVuSans.ttf")
    pdf.add_font("DejaVu", "B", _FONTS / "DejaVuSans-Bold.ttf")
    pdf.add_page()

    pdf.set_font("DejaVu", "B", 15)
    pdf.cell(0, 8, TITLE, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("DejaVu", "", 9)
    pdf.set_text_color(110)
    pdf.cell(0, 6, _pdf_text(f"{subtitle} · {LEGEND}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0)
    pdf.ln(2)

    if not groups:
        pdf.set_font("DejaVu", "", 10)
        pdf.cell(0, 10, "Sin tareas pendientes ni en progreso.")
        return bytes(pdf.output())

    header_style = FontFace(emphasis="BOLD", color=255, fill_color=(31, 41, 55))
    col_widths = (70, 24, 40, 26, 117)
    for g in groups:
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, _pdf_text(
            f"{g['department']} — {g['pending']} pendiente{'s' if g['pending'] != 1 else ''}"
            f" · {g['in_progress']} en progreso"
        ), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("DejaVu", "", 8)
        with pdf.table(
            col_widths=col_widths, headings_style=header_style, line_height=4.5,
            text_align="LEFT", repeat_headings=1, padding=1.2,
        ) as table:
            table.row(["Tarea", "Estado", "Responsable", "Seguimiento", "Último comentario"])
            for t in g["tasks"]:
                title = t["title"]
                if t["due_date"]:
                    title += f"\nVence {t['due_date'].strftime('%d/%m/%Y')}" + (" (vencida)" if t["overdue"] else "")
                follow = _follow_up_lines(t["follow_ups_shown"]) or "No"
                if t["follow_ups_hidden"]:
                    follow += f"\n+{t['follow_ups_hidden']} más"
                if t["last_comment_at"]:
                    comment = (
                        _truncate(t["last_comment"], 400)
                        + f"\n— {t['last_comment_by']} · {_local(t['last_comment_at']).strftime('%d/%m/%Y %H:%M')}"
                    )
                else:
                    comment = "Sin comentarios"
                row = table.row()
                for value in (title, _status(t), t["assignee"] or "—", follow, comment):
                    row.cell(_pdf_text(value))
        pdf.ln(4)

    return bytes(pdf.output())
