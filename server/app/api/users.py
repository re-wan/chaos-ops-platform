"""用户管理 API 路由（主路径 /api/v1/users）。"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlmodel import Session, select, update

from app.core.error_messages import error_detail
from app.api.deps import get_db, get_pagination, require_admin, require_feature
from app.schemas.page import Page, PageParams
from app.core.security import hash_password
from app.models.user import User
from app.models.user_session import UserSession
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services import audit_log

router = APIRouter(prefix="/api/v1/users", tags=["users"])


def _user_to_read(user: User) -> UserRead:
    """将 User ORM 对象转换为响应模型。"""
    return UserRead(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
    )


def _count_active_admins(session: Session) -> int:
    """统计当前处于启用状态的 admin 用户数量。"""
    return len(
        session.exec(
            select(User).where(User.role == "admin", User.is_active == True)  # noqa: E712
        ).all()
    )


def _deactivate_user_sessions(session: Session, user_id: int) -> None:
    """将指定用户的所有 Session Token 标记为失效。"""
    session.exec(
        update(UserSession)
        .where(UserSession.user_id == user_id)
        .values(is_active=False)
    )
    session.commit()


@router.get("", response_model=Page[UserRead])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    pagination: PageParams = Depends(get_pagination),
):
    """列出所有用户（admin），支持服务端分页。"""
    total = db.exec(select(func.count(User.id))).one() or 0
    start = (pagination.page - 1) * pagination.page_size
    users = db.exec(
        select(User)
        # id 作为 tiebreaker，保证同 created_at 时翻页稳定不重复不漏
        .order_by(User.created_at, User.id)
        .offset(start)
        .limit(pagination.page_size)
    ).all()
    return Page[UserRead](
        items=[_user_to_read(u) for u in users],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    request: Request,
    body: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
    _: None = Depends(require_feature("multi_user_rbac")),
):
    """创建新用户（admin）。

    License 门控：multi_user_rbac（专业版及以上）。免费版仅内置 admin 账号，
    创建第二个用户即 403；list/get/update/delete 不挂门控，保证免费版 admin
    能管理自己的账号（「完整可用」定位）。
    """
    existing = db.exec(select(User).where(User.username == body.username)).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_detail(request, "users.username_exists"),
        )

    if body.email is not None:
        email_existing = db.exec(select(User).where(User.email == body.email)).first()
        if email_existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_detail(request, "users.email_exists"),
            )

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        role=body.role,
        is_active=body.is_active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    audit_log.record_user_created(db, user, created_by=current_user.id)
    return _user_to_read(user)


@router.get("/{user_id}", response_model=UserRead)
def get_user(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """获取用户详情（admin）。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "users.not_found"),
        )
    return _user_to_read(user)


@router.put("/{user_id}", response_model=UserRead)
def update_user(
    request: Request,
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """更新用户信息/密码/状态（admin）。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "users.not_found"),
        )

    update_data = body.model_dump(exclude_unset=True)
    changes: dict = {}

    # 邮箱变更：唯一性校验
    if "email" in update_data and update_data["email"] != user.email:
        new_email = update_data["email"]
        if new_email is not None:
            email_existing = db.exec(
                select(User).where(User.email == new_email, User.id != user.id)
            ).first()
            if email_existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=error_detail(request, "users.email_exists"),
                )
        changes["email"] = (user.email, new_email)
        user.email = new_email

    # 角色变更：记录变更并保护最后一个 active admin
    if "role" in update_data and update_data["role"] != user.role:
        if (
            user.role == "admin"
            and update_data["role"] != "admin"
            and user.is_active
            and _count_active_admins(db) <= 1
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail(request, "users.last_admin_demote"),
            )
        changes["role"] = (user.role, update_data["role"])
        user.role = update_data["role"]

    # 状态变更：禁用用户时立即失效其所有 Session Token，并保护最后一个 active admin
    if "is_active" in update_data and update_data["is_active"] != user.is_active:
        if (
            not update_data["is_active"]
            and user.role == "admin"
            and user.is_active
            and _count_active_admins(db) <= 1
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail(request, "users.last_admin_disable"),
            )
        changes["is_active"] = (user.is_active, update_data["is_active"])
        user.is_active = update_data["is_active"]
        if not user.is_active:
            _deactivate_user_sessions(db, user.id)

    # 密码变更
    if "password" in update_data:
        user.hashed_password = hash_password(update_data["password"])
        changes["password"] = ("*", "*")

    if not changes:
        return _user_to_read(user)

    db.add(user)
    db.commit()
    db.refresh(user)

    audit_log.record_user_updated(db, user, changes=changes, created_by=current_user.id)
    return _user_to_read(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """删除用户（admin）。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_detail(request, "users.not_found"),
        )

    # 禁止删除最后一个 active admin
    if user.role == "admin" and user.is_active and _count_active_admins(db) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "users.last_admin_delete"),
        )

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_detail(request, "users.delete_self"),
        )

    audit_log.record_user_deleted(db, user, created_by=current_user.id)
    db.delete(user)
    db.commit()
    return None
