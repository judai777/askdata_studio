from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, StrictInt

from ..errors import PipelineStageError
from ..eval_models import model_catalog
from ..evaluation import EvaluationService
from ..models import (
    ClarificationRequest,
    LoginRequest,
    QueryRequest,
    QueryResult,
    SaveFieldMemoryRequest,
    SaveMemoryRequest,
    SchemaTable,
)
from ..security import AuthService, AuthUser
from ..services.askdata_service import AskDataService


router = APIRouter(prefix="/api")
service = AskDataService()
auth_service = AuthService()
evaluation_service = EvaluationService()


class EvalRunRequest(BaseModel):
    mode: str
    task_ids: list[str]
    trials: int = 1
    fixture_id: str = "base"
    request_limit: StrictInt | None = None
    model: str | None = None


class EvalReviewRequest(BaseModel):
    decision: str
    note: str
    fingerprint: str


class EvalCandidateRequest(BaseModel):
    model: str


class EvalCandidateSqlRequest(BaseModel):
    task_id: str
    sql: str
    fixture_id: str = "base"


class EvalLabelRequest(BaseModel):
    fingerprint: str
    labels: dict[str, str]
    note: str


def require_user(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AuthUser:
    user = auth_service.authenticate(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以执行该操作")
    return user


@router.get("/eval/catalog")
def eval_catalog(_: AuthUser = Depends(require_admin)) -> dict:
    return evaluation_service.catalog()


@router.get("/eval/benchmark-readiness")
def eval_benchmark_readiness(_: AuthUser = Depends(require_admin)) -> dict:
    return evaluation_service.benchmark_readiness()


@router.get("/eval/models")
def eval_models(_: AuthUser = Depends(require_admin)) -> dict:
    try:
        return model_catalog()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/eval/audit")
def eval_audit(_: AuthUser = Depends(require_admin)) -> dict:
    return evaluation_service.audit()


@router.post("/eval/candidate-sql/check")
def eval_candidate_sql_check(payload: EvalCandidateSqlRequest, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.check_candidate_sql(payload.task_id, payload.sql, payload.fixture_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/eval/calibration")
def eval_calibration(_: AuthUser = Depends(require_admin)) -> dict:
    return evaluation_service.calibration()


@router.get("/eval/reviews")
def eval_reviews(_: AuthUser = Depends(require_admin)) -> dict:
    return evaluation_service.reviews()


@router.get("/eval/reviews/{task_id}/preview")
def eval_review_preview(task_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.review_preview(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="评测任务不存在") from error


@router.post("/eval/reviews/{task_id}")
def eval_submit_review(task_id: str, payload: EvalReviewRequest, user: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.submit_review(task_id, payload.decision, payload.note, payload.fingerprint, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="评测任务不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/eval/runs")
def eval_runs(_: AuthUser = Depends(require_admin)) -> list[dict]:
    return evaluation_service.list_runs()


@router.get("/eval/compare")
def eval_compare(baseline: str, candidate: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.compare(baseline, candidate)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/eval/runs")
def eval_run(payload: EvalRunRequest, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.run(payload.mode, payload.task_ids, payload.trials, fixture_id=payload.fixture_id, request_limit=payload.request_limit, model=payload.model)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/eval/runs/start")
def eval_start(payload: EvalRunRequest, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.start(payload.mode, payload.task_ids, payload.trials, payload.fixture_id, payload.request_limit, payload.model)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/eval/runs/{run_id}/candidate")
def eval_start_candidate(run_id: str, payload: EvalCandidateRequest, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.start_candidate(run_id, payload.model)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/eval/runs/{run_id}/labels")
def eval_run_labels(run_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.labels(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error


@router.post("/eval/runs/{run_id}/labels/{task_id}/{trial}")
def eval_submit_label(run_id: str, task_id: str, trial: int, payload: EvalLabelRequest, user: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.submit_label(run_id, task_id, trial, payload.fingerprint, payload.labels, payload.note, user.user_id)
    except (FileNotFoundError, KeyError) as error:
        raise HTTPException(status_code=404, detail="评测试验不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/eval/runs/{run_id}")
def eval_run_detail(run_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.get_run(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error


@router.get("/eval/runs/{run_id}/export")
def eval_run_export(run_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.export(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/eval/runs/{run_id}/cancel")
def eval_cancel(run_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.cancel(run_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/eval/runs/{run_id}/replay")
def eval_replay(run_id: str, _: AuthUser = Depends(require_admin)) -> dict:
    try:
        return evaluation_service.get_run(run_id, replay=True)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="评测运行不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/health")
def health() -> dict:
    return {"status": "ok", **service.status()}


@router.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    result = auth_service.login(payload.username, payload.password)
    if not result:
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token, user = result
    return {"access_token": token, "token_type": "bearer", "user": user.public()}


@router.get("/auth/me")
def me(user: AuthUser = Depends(require_user)) -> dict[str, str]:
    return user.public()


@router.post("/auth/logout")
def logout(
    authorization: str | None = Header(default=None, alias="Authorization"),
    _: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    auth_service.logout(authorization)
    return {"logged_out": True}


@router.get("/schema", response_model=list[SchemaTable])
def schema(
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    return service.visible_schema(user.user_id)


@router.get("/schema/search")
def search_schema(
    q: str,
    threshold: float | None = None,
    user: AuthUser = Depends(require_user),
) -> dict:
    if not q.strip():
        raise HTTPException(status_code=400, detail="检索问题不能为空")
    if threshold is not None and not 0 <= threshold <= 1:
        raise HTTPException(status_code=400, detail="阈值必须在0到1之间")
    try:
        return service.search_schema(q.strip(), threshold, user.user_id)
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.post("/schema/index/rebuild")
def rebuild_schema_index(_: AuthUser = Depends(require_admin)) -> dict:
    try:
        return service.rebuild_schema_index()
    except PipelineStageError as error:
        raise HTTPException(
            status_code=502,
            detail=f"{error.stage}失败：{error.message}",
        ) from error


@router.get("/config")
def config(_: AuthUser = Depends(require_user)) -> dict:
    return service.status()


@router.get("/mcp/tools")
def mcp_tools(
    user: AuthUser = Depends(require_user),
) -> list[dict]:
    """查看模型可用的MCP工具及其输入输出Schema。"""
    return service.list_mcp_tools(user.user_id)


@router.get("/skills")
def skills(_: AuthUser = Depends(require_user)) -> list[dict]:
    """查看当前启用的应用级Skill。"""
    return service.list_skills()


@router.post("/query", response_model=QueryResult)
def query(
    payload: QueryRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    workspace = payload.workspace.model_dump(exclude_none=True) if payload.workspace else None
    return service.submit(payload.query, payload.session_id, workspace, user.user_id)


@router.post("/tasks/{task_id}/clarify", response_model=QueryResult)
def clarify(
    task_id: str,
    payload: ClarificationRequest,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        return service.clarify(task_id, payload.option_id, user_id=user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="无效的澄清选项") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.get("/tasks/{task_id}", response_model=QueryResult)
def task(
    task_id: str,
    user: AuthUser = Depends(require_user),
) -> QueryResult:
    try:
        return service.get_task(task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="查询任务不存在") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@router.post("/memories")
def save_memory(
    payload: SaveMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        service.save_memory(payload.task_id, user.user_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="没有可保存的查询结果") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return {"saved": True}


@router.get("/memories")
def memories(user: AuthUser = Depends(require_user)) -> list[dict]:
    return service.list_memories(user.user_id)


@router.post("/memories/fields")
def save_field_memory(
    payload: SaveFieldMemoryRequest,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    try:
        service.save_field_memory(
            payload.table_id,
            payload.name,
            payload.label,
            payload.field_type,
            user.user_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"saved": True}


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: str,
    user: AuthUser = Depends(require_user),
) -> dict[str, bool]:
    service.delete_memory(memory_id, user.user_id)
    return {"deleted": True}
