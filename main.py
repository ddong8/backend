# backend/main.py
import asyncio 
from contextlib import asynccontextmanager 
from fastapi import FastAPI, Depends, HTTPException 
from fastapi.middleware.cors import CORSMiddleware 
import socketio 
from sqlalchemy.ext.asyncio import AsyncSession 
from sqlalchemy.future import select 
from typing import Dict, List, Optional, Any, Callable 
from datetime import datetime 
import pydantic 

from models import ( 
    init_db, get_db, TradingTask, TaskStatus, AsyncSessionLocal 
)
from task_manager import TaskManager 
from logging_config import setup_loguru 
from config import TQ_ACCOUNT, TQ_PASSWORD 

from loguru import logger 
setup_loguru() 

class TaskBase(pydantic.BaseModel):
    name: str = pydantic.Field(..., min_length=1, max_length=128, description="交易任务的自定义名称")
    symbol: str = pydantic.Field(..., min_length=1, max_length=64, description="交易标的的合约代码")

class TaskCreate(TaskBase):
    pass

class TaskResponse(TaskBase):
    id: int
    status: TaskStatus
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = pydantic.ConfigDict(from_attributes=True)

task_manager_instance: Optional[TaskManager] = None

def get_async_session_factory() -> Callable[[], AsyncSession]:
    return AsyncSessionLocal

@asynccontextmanager
async def lifespan(app: FastAPI):
    global task_manager_instance
    logger.info("FastAPI 应用正在启动...")
    if not TQ_ACCOUNT or not TQ_PASSWORD:
        logger.critical("严重警告: TQ_ACCOUNT 或 TQ_PASSWORD 环境变量未设置。")
    await init_db()
    task_manager_instance = TaskManager(
        sio_server=sio, 
        db_session_factory=get_async_session_factory()
    )
    logger.info("任务管理器已成功初始化。")
    logger.info("正在尝试从数据库恢复上次运行时标记为 'RUNNING' 的任务...")
    try:
        async with get_async_session_factory()() as db:
            stmt = select(TradingTask).where(TradingTask.status == TaskStatus.RUNNING)
            result = await db.execute(stmt)
            running_tasks_from_db: List[TradingTask] = result.scalars().all()
            if running_tasks_from_db:
                logger.info(f"发现 {len(running_tasks_from_db)} 个任务状态为 'RUNNING'。正在尝试重启...")
                for task_model in running_tasks_from_db:
                    logger.info(f"正在重新启动任务 ID: {task_model.id}, 名称: '{task_model.name}'")
                    if task_manager_instance:
                         await task_manager_instance.start_task(task_model)
            else:
                logger.info("数据库中未发现状态为 'RUNNING' 的任务需要恢复。")
    except Exception as e:
        logger.error(f"在尝试恢复运行中任务时发生错误: {e}", exc_info=True)
    yield
    logger.info("FastAPI 应用正在关闭...")
    if task_manager_instance:
        logger.info("正在请求任务管理器停止所有活动的交易任务...")
        await task_manager_instance.stop_all_tasks()
    logger.success("FastAPI 应用已成功关闭。所有资源已清理。")

app = FastAPI(
    title="智能交易平台后端 API",
    description="提供交易任务管理、实时行情和账户数据推送等功能。",
    version="0.2.1", # 版本更新
    lifespan=lifespan
)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False, # Loguru 会处理应用日志，这里可以禁用 Socket.IO 自己的日志或设为 True 观察
    engineio_logger=False, # 通常也禁用，除非深度调试 Engine.IO
    ping_timeout=20,
    ping_interval=25
)
socket_app = socketio.ASGIApp(sio,socketio_path="/") 
app.mount("/ws", socket_app) 

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],   
    allow_headers=["*"],   
)

@app.get("/", tags=["通用"], summary="API 健康检查及信息", response_description="API 状态信息")
async def read_root():
    logger.info("API 根路径被访问 (健康检查)。")
    return {
        "message": "欢迎使用智能交易平台后端 API!", "status": "运行中",
        "version": app.version, "timestamp": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/api/tasks", response_model=TaskResponse, status_code=201, tags=["任务管理"], summary="创建新交易任务")
async def create_task_endpoint(task_data: TaskCreate, db: AsyncSession = Depends(get_db)):
    logger.info(f"收到创建新任务的请求: 名称='{task_data.name}', 标的='{task_data.symbol}'")
    db_task = TradingTask(name=task_data.name, symbol=task_data.symbol, status=TaskStatus.PENDING, created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(db_task)
    try:
        await db.commit()  # 显式提交事务
        await db.refresh(db_task)  # 显式刷新对象以获取数据库生成的 ID
        logger.success(
            f"交易任务已成功创建并持久化: ID={db_task.id}, 名称='{db_task.name}'"
        )
    except Exception as e:
        logger.error(
            f"创建任务 (名称='{task_data.name}') 时数据库操作失败: {e}", exc_info=True
        )
        await db.rollback()  # 提交失败时回滚事务
        raise HTTPException(status_code=500, detail=f"创建任务时发生数据库错误。")

    if db_task.id is None:  # 额外的健全性检查
        logger.error(
            f"严重错误：任务 (名称='{task_data.name}') commit 和 refresh 后 ID 仍然为 None！"
        )
        raise HTTPException(status_code=500, detail="创建任务后未能获取任务 ID。")
    return db_task  # FastAPI 会自动将 ORM 对象序列化为 TaskResponse 模型

@app.get("/api/tasks", response_model=List[TaskResponse], tags=["任务管理"], summary="获取所有交易任务列表")
async def get_tasks_endpoint(skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)):
    logger.info(f"收到获取任务列表的请求: skip={skip}, limit={limit}")
    stmt = select(TradingTask).order_by(TradingTask.id.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    tasks: List[TradingTask] = result.scalars().all()
    logger.info(f"成功获取 {len(tasks)} 个任务。")
    return tasks

@app.get("/api/tasks/{task_id}", response_model=TaskResponse, tags=["任务管理"], summary="获取特定交易任务详情")
async def get_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    logger.info(f"收到获取任务详情的请求: 任务 ID={task_id}")
    db_task: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if db_task is None:
        logger.warning(f"尝试获取任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {task_id} 的任务。")
    logger.info(f"成功获取任务详情: ID={db_task.id}, 名称='{db_task.name}'")
    return db_task

@app.post("/api/tasks/{task_id}/start", response_model=TaskResponse, tags=["任务管理"], summary="启动一个指定的交易任务")
async def start_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    logger.info(f"收到启动任务的请求: 任务 ID={task_id}")
    if task_manager_instance is None:
        logger.critical("任务管理器未初始化，无法启动任务。")
        raise HTTPException(status_code=503, detail="服务内部错误：任务管理器当前不可用。")
    
    db_task: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if db_task is None:
        logger.warning(f"启动任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {task_id} 的任务以启动。")
    
    if db_task.status == TaskStatus.RUNNING:
        logger.warning(f"启动任务请求被拒绝：任务 ID={task_id} (名称='{db_task.name}') 已运行。")
        raise HTTPException(status_code=400, detail="任务当前已在运行中。")
    
    if db_task.status == TaskStatus.ERROR:
        logger.info(f"尝试启动处于 ERROR 状态的任务 ID: {task_id}。允许启动。")

    success = await task_manager_instance.start_task(db_task)
    if not success:
        logger.error(f"任务管理器未能成功启动任务 ID={task_id}。")
        # 具体的错误原因应该由 task_manager.start_task 记录
        # 这里可以返回一个通用的 500 错误，或更具体的错误（如果 task_manager 返回了错误信息）
        raise HTTPException(status_code=500, detail="启动任务时发生内部错误。请检查服务器日志。")
    
    # 再次从数据库获取任务，以确保返回的是最新的状态
    # (因为 start_task 内部会修改数据库并通过其自己的会话提交)
    updated_db_task: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if not updated_db_task: # 理论上不应发生
        logger.error(f"启动任务后无法从数据库重新获取任务 ID={task_id}。")
        raise HTTPException(status_code=500, detail="启动任务后状态同步失败。")

    logger.success(f"交易任务已成功启动: ID={updated_db_task.id}, 名称='{updated_db_task.name}', 新状态: {updated_db_task.status.value}")
    return updated_db_task

@app.post("/api/tasks/{task_id}/stop", response_model=TaskResponse, tags=["任务管理"], summary="停止一个指定的交易任务")
async def stop_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)):
    logger.info(f"收到停止任务的请求: 任务 ID={task_id}")
    if task_manager_instance is None:
        logger.critical("任务管理器未初始化，无法停止任务。")
        raise HTTPException(status_code=503, detail="服务内部错误：任务管理器当前不可用。")

    db_task_before_stop: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if db_task_before_stop is None:
        logger.warning(f"停止任务失败：未找到 ID 为 {task_id} 的任务。")
        raise HTTPException(status_code=404, detail=f"未找到 ID 为 {task_id} 的任务以停止。")
    
    is_in_manager = task_id in (task_manager_instance.active_asyncio_tasks if task_manager_instance else {})
    if db_task_before_stop.status not in [TaskStatus.RUNNING, TaskStatus.ERROR] and not is_in_manager:
         logger.warning(f"停止任务请求：任务 ID={task_id} 状态为 '{db_task_before_stop.status.value}' 且不在活动列表，无需停止。")
         return db_task_before_stop 

    success = await task_manager_instance.stop_task(task_id)
    # stop_task 内部会更新数据库状态
    
    updated_db_task: Optional[TradingTask] = await db.get(TradingTask, task_id)
    if not updated_db_task: # 理论上不应发生
        logger.error(f"停止任务后无法从数据库重新获取任务 ID={task_id}。")
        # 即使找不到，也认为停止操作在 TaskManager层面可能已成功
        # 返回一个表示任务可能已被删除或出错的状态
        # 或者，如果 success 为 True，但找不到 db_task，这表示一个严重的不一致
        if success:
            raise HTTPException(status_code=500, detail="停止任务后状态同步失败，数据库记录可能丢失。")
        else: # 如果 stop_task 本身返回 False
            raise HTTPException(status_code=500, detail="停止任务操作失败，请检查服务器日志。")


    logger.info(f"交易任务停止请求已处理: ID={updated_db_task.id}, 名称='{updated_db_task.name}', 当前状态: {updated_db_task.status.value}")
    return updated_db_task

# -------- Socket.IO 事件处理器 (与上一版本基本一致，确保 task_manager_instance.active_tqapis 存在) --------
@sio.event
async def connect(sid: str, environ: dict, auth: Optional[dict] = None):
    remote_addr = environ.get('asgi.scope', {}).get('client', ('N/A', None))[0]
    logger.info(f"Socket.IO 客户端已连接: SID='{sid}', IP='{remote_addr}', Auth: {auth if auth else '无'}")
    await sio.emit("welcome", {"message": "已成功连接到智能交易平台实时数据服务！"}, room=sid)

@sio.event
async def disconnect(sid: str):
    logger.info(f"Socket.IO 客户端已断开连接: SID='{sid}'")

@sio.event
async def join_task_room(
    sid: str, data: Any, ack: Optional[Callable] = None
) -> Optional[Dict[str, Any]]:  # 添加可选的 ack 参数，并调整返回类型
    """
    处理客户端加入特定任务房间的请求。
    客户端应发送包含 "task_id" 的数据。
    成功加入后，该客户端将能接收到对应任务的实时数据推送。
    如果提供了 ack 回调，则调用它。
    """
    logger.debug(
        f"SID='{sid}' 请求加入任务房间，数据: {data}, 是否有 ack: {ack is not None}"
    )
    response_message: Dict[str, Any]

    if not isinstance(data, dict) or "task_id" not in data:
        msg = "请求数据格式错误，必须包含 'task_id'。"
        logger.warning(
            f"来自 SID='{sid}' 的 'join_task_room' 请求格式错误：{msg} 数据: {data}"
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)  # 如果有 ack，调用它
        return None  # 或者直接返回，FastAPI/SocketIO 会处理

    task_id_data = data.get("task_id")
    if not isinstance(task_id_data, int):
        msg = "'task_id' 必须是一个整数。"
        logger.warning(
            f"来自 SID='{sid}' 的 'join_task_room' 请求 'task_id' 类型无效: {task_id_data} (类型: {type(task_id_data).__name__})"
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)
        return None

    task_id: int = task_id_data
    room_name: str = f"task_{task_id}"

    try:
        await sio.enter_room(sid, room_name)
        logger.info(f"客户端 SID='{sid}' 已成功加入 Socket.IO 房间: '{room_name}'")

        # (可选) 推送初始行情
        if task_manager_instance and task_id in task_manager_instance.active_tqapis:
            # ... (推送初始行情的逻辑) ...
            pass  # 省略以保持简洁，之前的代码是正确的

        msg = f"已成功加入房间 '{room_name}'。"
        response_message = {"status": "success", "message": msg}
        if ack:
            await ack(response_message)
        return None  # 如果已经调用 ack，通常不需要再返回 HTTP 响应式的内容
        # 如果不调用 ack，可以返回字典，Socket.IO 会将其作为 ack 的数据
    except Exception as e:
        msg = "加入房间时发生服务器内部错误。"
        logger.error(
            f"客户端 SID='{sid}' 加入房间 '{room_name}' 时发生错误: {e}", exc_info=True
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)
        return None


@sio.event
async def leave_task_room(
    sid: str, data: Any, ack: Optional[Callable] = None
) -> Optional[Dict[str, Any]]:
    """处理客户端离开特定任务房间的请求。"""
    logger.debug(
        f"SID='{sid}' 请求离开任务房间，数据: {data}, 是否有 ack: {ack is not None}"
    )
    response_message: Dict[str, Any]

    if not isinstance(data, dict) or "task_id" not in data:
        msg = "请求数据格式错误，必须包含 'task_id'。"
        logger.warning(
            f"来自 SID='{sid}' 的 'leave_task_room' 请求格式错误：{msg} 数据: {data}"
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)
        return None

    task_id_data = data.get("task_id")
    if not isinstance(task_id_data, int):
        msg = "'task_id' 必须是一个整数。"
        logger.warning(
            f"来自 SID='{sid}' 的 'leave_task_room' 请求 'task_id' 类型无效: {task_id_data}"
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)
        return None

    task_id: int = task_id_data
    room_name: str = f"task_{task_id}"

    try:
        await sio.leave_room(sid, room_name)
        logger.info(f"客户端 SID='{sid}' 已成功离开 Socket.IO 房间: '{room_name}'")
        msg = f"已成功离开房间 '{room_name}'。"
        response_message = {"status": "success", "message": msg}
        if ack:
            await ack(response_message)
        return None
    except Exception as e:
        msg = "离开房间时发生服务器内部错误。"
        logger.error(
            f"客户端 SID='{sid}' 离开房间 '{room_name}' 时发生错误: {e}", exc_info=True
        )
        response_message = {"status": "error", "message": msg}
        if ack:
            await ack(response_message)
        return None

# -------- Uvicorn 启动 (用于直接运行此文件进行本地开发测试) --------
if __name__ == "__main__":
    import uvicorn
    logger.info("正在以开发模式启动 Uvicorn ASGI 服务器 (用于直接运行 backend/main.py)...")
    uvicorn.run(
        "main:app", # 指向 FastAPI 应用实例
        host="0.0.0.0", 
        port=8000, 
        reload=False, # 启用代码热重载 (开发时方便)
        log_level="debug", # Uvicorn 自身的日志级别
    )