# backend/main.py
import asyncio  # Python 标准库，用于异步IO编程
from contextlib import (
    asynccontextmanager,
)  # 用于创建异步上下文管理器 (FastAPI lifespan)
from datetime import datetime  # Python 标准库，用于处理日期和时间
from typing import Any, Callable, Dict, List, Optional  # Python 类型提示

import pydantic  # Pydantic 库，用于数据校验和序列化
import socketio  # Socket.IO Python 服务器库
from config import TQ_ACCOUNT, TQ_PASSWORD  # 从项目配置导入 TQSDK 凭证
from fastapi import Depends, FastAPI, HTTPException  # FastAPI 核心组件
from fastapi.middleware.cors import CORSMiddleware  # CORS (跨域资源共享) 中间件
from logging_config import setup_loguru  # 导入 Loguru 日志配置函数 (如果单独配置)

# --- 日志配置 ---
from loguru import logger  # 直接从 Loguru 导入 logger 对象

# --- 导入项目内部模块 ---
from models import AsyncSessionLocal  # SQLAlchemy 异步会话工厂
from models import TaskStatus  # 任务状态的枚举类型
from models import TradingTask  # 交易任务的 SQLAlchemy ORM 模型
from models import get_db  # FastAPI 依赖项，用于获取数据库会话
from models import init_db  # 从 models.py 导入数据库相关; 初始化数据库表的函数
from sqlalchemy.ext.asyncio import AsyncSession  # SQLAlchemy 异步会话
from sqlalchemy.future import (
    select,
)  # SQLAlchemy 2.0+风格的查询语句 (select 而不是 query)
from task_manager import TaskManager  # 导入任务管理器类

setup_loguru()  # 在应用启动初期调用以配置 Loguru (假设已在 logging_config.py 定义)

# --- Pydantic 数据验证和序列化模型 ---
# 使用 Pydantic V2 风格 (如果使用 V1，Config 写法略有不同)


class TaskBase(pydantic.BaseModel):
    """任务基础模型，定义了创建和读取任务时共有的字段。"""

    name: str = pydantic.Field(
        ...,  # ... 表示此字段为必需
        min_length=1,
        max_length=128,
        description="交易任务的自定义名称，例如 '螺纹钢日内突破策略'",
    )
    symbol: str = pydantic.Field(
        ...,
        min_length=1,
        max_length=64,
        description="交易标的的合约代码，格式如 'SHFE.rb2410' 或 'DCE.i2409'",
    )


class TaskCreate(TaskBase):
    """用于创建新交易任务的请求体模型。"""

    pass  # 目前与 TaskBase 相同，但可以扩展以包含创建时特有的字段


class TaskResponse(TaskBase):
    """API 响应中返回的交易任务数据模型。"""

    id: int  # 任务的唯一数据库 ID
    status: TaskStatus  # 任务的当前状态 (使用枚举类型)
    created_at: datetime  # 任务创建时间戳
    updated_at: Optional[datetime] = None  # 任务最后更新时间戳 (可能为空)

    # Pydantic V2 的配置方式，允许从 ORM 对象的属性直接创建 Pydantic 模型实例
    model_config = pydantic.ConfigDict(from_attributes=True)

    # Pydantic V1 的配置方式 (如果使用 Pydantic V1)
    # class Config:
    #     orm_mode = True


# --- 全局状态变量和辅助函数 ---
task_manager_instance: Optional[TaskManager] = (
    None  # 全局的任务管理器实例，在应用启动时初始化
)


def get_async_session_factory() -> Callable[[], AsyncSession]:
    """返回一个 SQLAlchemy 异步数据库会话的工厂函数。"""
    return AsyncSessionLocal


# --- FastAPI 应用生命周期事件 (Lifespan Manager) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 应用的生命周期管理器。
    用于在应用启动时执行初始化操作，在应用关闭时执行清理操作。
    """
    global task_manager_instance  # 声明要修改全局变量
    logger.info("FastAPI 应用正在启动...")

    # 启动时检查关键配置是否已设置
    if not TQ_ACCOUNT or not TQ_PASSWORD:
        logger.critical(
            "严重警告: TQ_ACCOUNT 或 TQ_PASSWORD 环境变量未设置。"
            "交易相关功能可能无法正常工作，请检查您的 .env 文件或系统环境变量。"
        )

    await init_db()  # 初始化数据库表 (如果它们尚不存在)

    # 初始化任务管理器实例，传入 Socket.IO 服务器实例和数据库会话工厂
    task_manager_instance = TaskManager(
        sio_server=sio,  # sio 实例在下面全局定义
        db_session_factory=get_async_session_factory(),
    )
    logger.info("任务管理器已成功初始化。")

    # (可选功能) 尝试从数据库恢复在应用上次关闭时仍处于 "RUNNING" 状态的任务
    logger.info("正在尝试从数据库恢复上次运行时标记为 'RUNNING' 的任务...")
    try:
        async with get_async_session_factory()() as db:  # 获取一个新的数据库会话
            # 查询所有状态为 RUNNING 的任务
            stmt = select(TradingTask).where(TradingTask.status == TaskStatus.RUNNING)
            result = await db.execute(stmt)
            running_tasks_from_db: List[TradingTask] = result.scalars().all()

            if running_tasks_from_db:
                logger.info(
                    f"发现 {len(running_tasks_from_db)} 个任务状态为 'RUNNING'。正在尝试重新启动它们..."
                )
                for task_model in running_tasks_from_db:
                    logger.info(
                        f"正在重新启动任务 ID: {task_model.id}, 名称: '{task_model.name}'"
                    )
                    if task_manager_instance:  # 确保任务管理器实例已创建
                        # 调用任务管理器的 start_task 方法，它会处理数据库状态更新和 TQSDK 启动
                        await task_manager_instance.start_task(task_model)
            else:
                logger.info("数据库中未发现状态为 'RUNNING' 的任务需要恢复。")
    except Exception as e:
        logger.error(
            f"在尝试恢复运行中任务时发生错误: {e}", exc_info=True
        )  # 记录完整错误信息

    yield  # 在此点，FastAPI 应用开始处理外部请求

    # --- 应用关闭时的清理操作 ---
    logger.info("FastAPI 应用正在关闭...")
    if task_manager_instance:
        logger.info("正在请求任务管理器停止所有活动的交易任务...")
        await task_manager_instance.stop_all_tasks()  # 请求停止所有活动的 TQSDK 任务
    logger.success("FastAPI 应用已成功关闭。所有资源已清理。")


# --- FastAPI 应用实例和 Socket.IO 服务器实例 ---
app = FastAPI(
    title="智能交易平台后端 API",  # API 文档标题
    description="提供交易任务管理、实时行情和账户数据推送等功能。",  # API 描述
    version="0.2.0",  # API 版本号
    lifespan=lifespan,  # 使用上面定义的 lifespan 管理器
)

# 创建 Socket.IO 异步服务器实例
sio = socketio.AsyncServer(
    async_mode="asgi",  # 使用 ASGI 模式，以便与 FastAPI (ASGI框架) 集成
    cors_allowed_origins="*",  # 允许所有来源的 CORS 请求 (生产环境应配置为具体的前端域名)
    # cors_allowed_origins=["http://localhost:3000", "https://your-frontend-domain.com"],
    logger=False,  # True 会使用标准 logging 输出 Socket.IO 日志, False 禁用, 或传递 logger 实例
    engineio_logger=False,  # True 会输出 Engine.IO 的详细调试日志 (通常很冗余)
    ping_timeout=20,  # 服务器在未收到客户端 PONG 响应后认为连接超时的时间 (秒)
    ping_interval=25,  # 服务器向客户端发送 PING 包的间隔时间 (秒)
)
# 将 Socket.IO 服务器包装为 ASGI 应用
socket_app = socketio.ASGIApp(sio, socketio_path="/")
# 将 Socket.IO 应用挂载到 FastAPI 应用的 "/ws" 路径下
# 这意味着客户端连接 Socket.IO 时，应指向 "http://your_server/ws/socket.io/"
app.mount("/ws", socket_app)

# --- CORS (跨域资源共享) 中间件配置 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000"
    ],  # 明确允许前端开发服务器的地址 (生产环境应替换为实际前端域名)
    allow_credentials=True,  # 允许客户端请求携带凭证 (如 Cookies)
    allow_methods=["*"],  # 允许所有标准的 HTTP 方法 (GET, POST, PUT, DELETE 等)
    allow_headers=["*"],  # 允许所有类型的 HTTP 请求头
)

# --- API 端点 (HTTP Endpoints) 定义 ---


@app.get(
    "/",
    tags=["通用"],
    summary="API 健康检查及信息",
    response_description="API 状态信息",
)
async def read_root():
    """
    根路径端点，用于健康检查或返回 API 的基本信息。
    """
    logger.info("API 根路径被访问 (健康检查)。")
    return {
        "message": "欢迎使用智能交易平台后端 API!",
        "status": "运行中",
        "version": app.version,  # 从 FastAPI app 实例获取版本号
        "timestamp": datetime.utcnow().isoformat() + "Z",  # 当前 UTC 时间
    }


@app.post(
    "/api/tasks",
    response_model=TaskResponse,  # 指定响应体的数据模型
    status_code=201,  # HTTP 状态码：201 Created
    tags=["任务管理"],
    summary="创建新的交易任务",
    response_description="成功创建的交易任务详情",
)
async def create_task_endpoint(
    task_data: TaskCreate, db: AsyncSession = Depends(get_db)
):
    """
    创建一个新的交易任务。
    接收任务名称和交易标的，将其存入数据库并返回创建的任务对象。
    """
    logger.info(
        f"收到创建新任务的请求: 名称='{task_data.name}', 标的='{task_data.symbol}'"
    )
    # (可选) 在这里可以添加逻辑来检查任务名称或标的是否已存在，以避免重复
    # existing_task_by_name = await db.execute(select(TradingTask).where(TradingTask.name == task_data.name))
    # if existing_task_by_name.scalars().first():
    #     logger.warning(f"创建任务失败：名称 '{task_data.name}' 已存在。")
    #     raise HTTPException(status_code=400, detail=f"具有相同名称 '{task_data.name}' 的任务已存在。")

    # 创建 TradingTask ORM 对象实例
    db_task = TradingTask(
        name=task_data.name,
        symbol=task_data.symbol,
        status=TaskStatus.PENDING,  # 新创建的任务默认为 PENDING 状态
    )
    db.add(db_task)  # 将新对象添加到 SQLAlchemy 会话中
    # await db.commit() # get_db 依赖的 finally 块会自动处理 commit
    # await db.refresh(db_task) # 如果需要立即获取数据库生成的 id 等字段，可以在 commit 后 refresh
    # 在 get_db 的 commit 之后，db_task 会自动拥有 id 等信息
    logger.success(f"交易任务已成功创建: ID 将在 commit 后生成, 名称='{db_task.name}'")
    return db_task  # FastAPI 会自动将 ORM 对象序列化为 TaskResponse 模型


@app.get(
    "/api/tasks",
    response_model=List[TaskResponse],
    tags=["任务管理"],
    summary="获取所有交易任务列表",
    response_description="交易任务列表",
)
async def get_tasks_endpoint(
    skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)
):
    """
    获取所有交易任务的列表，支持分页 (通过 skip 和 limit 参数)。
    按任务 ID 降序排列 (新创建的在前)。
    """
    logger.info(f"收到获取任务列表的请求: skip={skip}, limit={limit}")
    stmt = select(TradingTask).order_by(TradingTask.id.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    tasks: List[TradingTask] = result.scalars().all()
    logger.info(f"成功获取 {len(tasks)} 个任务。")
    return tasks


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskResponse,
    tags=["任务管理"],
    summary="获取特定交易任务的详情",
    response_description="指定 ID 的交易任务详情",
)
async def get_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    """
    根据提供的任务 ID (task_id) 获取单个交易任务的详细信息。
    如果找不到任务，则返回 404 Not Found。
    """
    logger.info(f"收到获取任务详情的请求: 任务 ID={task_id}")
    # 使用 SQLAlchemy session.get() 方法通过主键高效获取对象
    db_task: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if db_task is None:
        logger.warning(f"尝试获取任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {task_id} 的任务。")
    logger.info(f"成功获取任务详情: ID={db_task.id}, 名称='{db_task.name}'")
    return db_task


@app.post(
    "/api/tasks/{task_id}/start",
    response_model=TaskResponse,
    tags=["任务管理"],
    summary="启动一个指定的交易任务",
    response_description="成功启动后的交易任务详情 (状态已更新)",
)
async def start_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    """
    启动一个指定的交易任务。任务状态将变为 RUNNING。
    """
    logger.info(f"收到启动任务的请求: 任务 ID={task_id}")
    if task_manager_instance is None:  # 防御性编程：检查任务管理器是否已初始化
        logger.critical(
            "任务管理器未初始化，无法启动任务。这是一个严重的服务配置问题。"
        )
        raise HTTPException(
            status_code=503, detail="服务内部错误：任务管理器当前不可用。"
        )

    db_task: Optional[TradingTask] = await db.get(
        TradingTask, task_id
    )  # 从数据库获取任务
    if db_task is None:
        logger.warning(f"启动任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(
            status_code=404, detail=f"未找到 ID 为 {task_id} 的任务以启动。"
        )

    if db_task.status == TaskStatus.RUNNING:  # 检查任务是否已在运行
        logger.warning(
            f"启动任务请求被拒绝：任务 ID={task_id} (名称='{db_task.name}') 已处于运行状态。"
        )
        raise HTTPException(
            status_code=400, detail="任务当前已在运行中，无需重复启动。"
        )

    # (可选) 对于处于 ERROR 状态的任务，可以决定是否允许直接重启，或需要其他操作
    if db_task.status == TaskStatus.ERROR:
        logger.info(
            f"尝试启动一个处于 ERROR 状态的任务 ID: {task_id} (名称='{db_task.name}')。允许启动。"
        )
        # 如果不允许，可以抛出异常：
        # raise HTTPException(status_code=400, detail="任务处于错误状态。请先检查任务日志或尝试重置任务。")

    # 调用任务管理器的 start_task 方法来实际启动 TQSDK 实例等
    success = await task_manager_instance.start_task(db_task)
    if not success:
        # task_manager.start_task 内部会记录具体的失败原因
        logger.error(
            f"任务管理器未能成功启动任务 ID={task_id} (名称='{db_task.name}')。"
        )
        raise HTTPException(
            status_code=500, detail="启动任务时发生内部错误。请检查服务器日志。"
        )

    # await db.refresh(db_task) # start_task 方法内部会更新数据库状态，
    # Pydantic response_model 会从 db_task 当前的属性创建响应，所以 refresh 可能不是必须的，
    # 但显式 refresh 可以确保返回的是数据库中最新的状态（如果 start_task 异步更新了它）。
    # 鉴于 get_db 会在最后 commit，这里 db_task 的状态应该已经是最新的了。
    logger.success(
        f"交易任务已成功启动: ID={db_task.id}, 名称='{db_task.name}', 新状态: {db_task.status.value}"
    )
    return db_task


@app.post(
    "/api/tasks/{task_id}/stop",
    response_model=TaskResponse,
    tags=["任务管理"],
    summary="停止一个指定的交易任务",
    response_description="成功停止后的交易任务详情 (状态已更新)",
)
async def stop_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    """
    停止一个指定的交易任务。任务状态将变为 STOPPED。
    """
    logger.info(f"收到停止任务的请求: 任务 ID={task_id}")
    if task_manager_instance is None:  # 防御性检查
        logger.critical("任务管理器未初始化，无法停止任务。")
        raise HTTPException(
            status_code=503, detail="服务内部错误：任务管理器当前不可用。"
        )

    db_task: Optional[TradingTask] = await db.get(
        TradingTask, task_id
    )  # 从数据库获取任务
    if db_task is None:
        logger.warning(f"停止任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(
            status_code=404, detail=f"未找到 ID 为 {task_id} 的任务以停止。"
        )

    # 检查任务是否确实需要停止 (例如，不是 PENDING 或 STOPPED 状态)
    is_in_manager_active_list = task_id in task_manager_instance.active_asyncio_tasks
    if (
        db_task.status != TaskStatus.RUNNING
        and db_task.status != TaskStatus.ERROR
        and not is_in_manager_active_list
    ):
        logger.warning(
            f"停止任务请求被拒绝：任务 ID={task_id} (名称='{db_task.name}') 当前状态为 '{db_task.status.value}'，"
            "且不在任务管理器的活动列表中，无需停止。"
        )
        # 仍然返回当前任务状态，因为“停止”操作对于一个已停止或未启动的任务可以认为是“已完成”
        return db_task
        # 或者抛出 400 错误：
        # raise HTTPException(status_code=400, detail="任务未在运行中，无法停止。")

    # 调用任务管理器的 stop_task 方法
    success = await task_manager_instance.stop_task(task_id)
    # stop_task 方法内部会处理数据库状态的更新

    # await db.refresh(db_task) # 获取最新的任务状态
    logger.info(
        f"交易任务停止请求已处理: ID={db_task.id}, 名称='{db_task.name}', "
        f"处理后状态: {db_task.status.value if db_task else 'N/A (任务可能已删除)'}"
    )
    return db_task


# --- Socket.IO 事件处理器 ---


@sio.event
async def connect(sid: str, environ: dict, auth: Optional[dict] = None):
    """
    当一个新客户端成功连接到 Socket.IO 服务器时触发。
    :param sid: 客户端的唯一会话 ID。
    :param environ: WSGI/ASGI 环境字典，包含请求信息 (如 IP 地址)。
    :param auth: (可选) 客户端连接时传递的认证数据。
    """
    remote_addr = environ.get("asgi.scope", {}).get("client", ("N/A", None))[
        0
    ]  # 尝试获取客户端 IP
    logger.info(
        f"Socket.IO 客户端已连接: SID='{sid}', IP='{remote_addr}', 认证数据: {auth if auth else '无'}"
    )

    # (可选) 在这里可以进行基于 auth 数据的身份验证逻辑
    # if not auth or not verify_token(auth.get("token")):
    #     logger.warning(f"客户端 SID='{sid}' 身份验证失败，将断开连接。")
    #     raise socketio.exceptions.ConnectionRefusedError('身份验证失败，无效的凭证。')

    # 向连接成功的客户端发送欢迎消息
    await sio.emit(
        "welcome", {"message": "已成功连接到智能交易平台实时数据服务！"}, room=sid
    )


@sio.event
async def disconnect(sid: str):
    """当一个客户端从 Socket.IO 服务器断开连接时触发。"""
    logger.info(f"Socket.IO 客户端已断开连接: SID='{sid}'")
    # (可选) 在这里可以执行一些与该 SID 相关的清理工作，
    # 例如，如果该客户端订阅了特定的房间，可以记录此事件或更新内部状态。
    # Socket.IO 会自动处理将 SID 从其加入的所有房间中移除。


@sio.event
async def join_task_room(sid: str, data: Any) -> Dict[str, Any]:  # 返回一个字典作为 ACK
    """
    处理客户端加入特定任务房间的请求。
    客户端应发送包含 "task_id" 的数据。
    成功加入后，该客户端将能接收到对应任务的实时数据推送。
    """
    logger.debug(f"SID='{sid}' 请求加入任务房间，数据: {data}")
    if not isinstance(data, dict) or "task_id" not in data:
        logger.warning(
            f"来自 SID='{sid}' 的 'join_task_room' 请求格式错误：缺少 'task_id'。数据: {data}"
        )
        return {"status": "error", "message": "请求数据格式错误，必须包含 'task_id'。"}

    task_id_data = data.get("task_id")
    if not isinstance(task_id_data, int):
        logger.warning(
            f"来自 SID='{sid}' 的 'join_task_room' 请求 'task_id' 类型无效: {task_id_data} (类型: {type(task_id_data).__name__})"
        )
        return {"status": "error", "message": "'task_id' 必须是一个整数。"}

    task_id: int = task_id_data
    room_name: str = f"task_{task_id}"  # 定义房间名称格式

    try:
        sio.enter_room(sid, room_name)  # 将客户端 SID 加入到指定的房间
        logger.info(f"客户端 SID='{sid}' 已成功加入 Socket.IO 房间: '{room_name}'")

        # (可选) 当客户端成功加入房间时，立即向其推送该任务的最新行情快照 (如果任务正在运行且有数据)
        if task_manager_instance and task_id in task_manager_instance.active_tqapis:
            tqapi = task_manager_instance.active_tqapis[
                task_id
            ]  # 获取该任务的 TqApi 实例
            # 需要从数据库获取任务的 symbol，因为 TqApi 实例可能没有直接存储它
            async with get_async_session_factory()() as db_session:  # 创建一个新的数据库会话
                task_model_from_db: Optional[TradingTask] = await db_session.get(
                    TradingTask, task_id
                )

            if task_model_from_db:
                quote = tqapi.get_quote(task_model_from_db.symbol)  # 获取行情对象
                if (
                    quote
                    and hasattr(quote, "last_price")
                    and quote.last_price is not None
                ):
                    # 使用 TaskManager 中的辅助方法来构建和发送行情数据
                    await task_manager_instance._emit_quote_update(
                        task_id, sid, quote
                    )  # room=sid 表示只发给这个客户端
                    logger.debug(
                        f"已向 SID='{sid}' 推送加入房间 '{room_name}' 后的初始行情快照。"
                    )

        return {"status": "success", "message": f"已成功加入房间 '{room_name}'。"}
    except Exception as e:
        logger.error(
            f"客户端 SID='{sid}' 加入房间 '{room_name}' 时发生错误: {e}", exc_info=True
        )
        return {"status": "error", "message": f"加入房间时发生服务器内部错误。"}


@sio.event
async def leave_task_room(sid: str, data: Any) -> Dict[str, Any]:
    """
    处理客户端离开特定任务房间的请求。
    客户端应发送包含 "task_id" 的数据。
    """
    logger.debug(f"SID='{sid}' 请求离开任务房间，数据: {data}")
    if not isinstance(data, dict) or "task_id" not in data:
        logger.warning(
            f"来自 SID='{sid}' 的 'leave_task_room' 请求格式错误：缺少 'task_id'。数据: {data}"
        )
        return {"status": "error", "message": "请求数据格式错误，必须包含 'task_id'。"}

    task_id_data = data.get("task_id")
    if not isinstance(task_id_data, int):
        logger.warning(
            f"来自 SID='{sid}' 的 'leave_task_room' 请求 'task_id' 类型无效: {task_id_data}"
        )
        return {"status": "error", "message": "'task_id' 必须是一个整数。"}

    task_id: int = task_id_data
    room_name: str = f"task_{task_id}"

    try:
        sio.leave_room(sid, room_name)  # 将客户端 SID 从指定房间移除
        logger.info(f"客户端 SID='{sid}' 已成功离开 Socket.IO 房间: '{room_name}'")
        return {"status": "success", "message": f"已成功离开房间 '{room_name}'。"}
    except Exception as e:
        logger.error(
            f"客户端 SID='{sid}' 离开房间 '{room_name}' 时发生错误: {e}", exc_info=True
        )
        return {"status": "error", "message": f"离开房间时发生服务器内部错误。"}


# -------- Uvicorn 启动 (仅用于直接运行此文件进行本地开发测试) --------
# 在生产环境中，通常通过 docker-compose.yml 中的 command 指令来启动 Gunicorn + Uvicorn worker。
if __name__ == "__main__":
    import uvicorn  # 导入 Uvicorn 服务器

    logger.info(
        "正在以开发模式启动 Uvicorn ASGI 服务器 (用于直接运行 backend/main.py)..."
    )

    # uvicorn.run() 参数说明:
    # "backend.main:app": 指定要运行的 ASGI 应用实例 (模块路径:应用对象)
    # host="0.0.0.0": 使服务器监听所有可用的网络接口 (而不仅仅是 localhost)
    # port=8000: 服务器监听的端口号
    # reload=True: 启用热重载，当代码文件发生更改时自动重启服务器 (仅限开发环境)
    # log_level="info": Uvicorn 自身的日志级别 (Loguru 的配置会影响应用日志，但 Uvicorn 也有自己的日志)
    # use_colors=True: (可选) Uvicorn 日志是否使用颜色
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,  # 启用代码热重载 (仅用于开发)
        log_level="debug",  # Uvicorn 日志级别，可以设为 debug 获取更详细信息
        # reload_dirs=["backend"], # (可选) 指定热重载监控的目录
    )
