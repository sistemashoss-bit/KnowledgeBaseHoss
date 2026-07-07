from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import get_current_user
from app.permissions import build_access_filter
from app import audit, rag
from app.templating import templates

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/", response_class=HTMLResponse)
def ask(
    request: Request,
    question: str = Form(...),
    mode: str = Query(default="full"),
    user=Depends(get_current_user),
):
    if not question.strip():
        return HTMLResponse("")

    access_filter = build_access_filter(user)
    dept_id = str(user.department_id) if user and user.department_id else None
    role = user.role if user else "anon"

    result = rag.answer_question(question.strip(), access_filter, role=role, dept_id=dept_id)

    audit.log_search(question.strip(), user=user, result_count=len(result["sources"]), search_type="rag")

    tpl = "chat/_widget_response.html" if mode == "widget" else "chat/_response.html"
    return templates.TemplateResponse(
        request, tpl,
        {
            "question": question.strip(),
            "answer": result["answer"],
            "sources": result["sources"],
        },
    )
