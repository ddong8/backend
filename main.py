import asyncio
import enum
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

import pydantic
import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from tqsdk import TqApi, TqAuth, TqSim
from tqsdk.exceptions import TqTimeoutError
from tqsdk.objs import Quote

# --- 全局变量与配置 ---
load_dotenv()
TQ_ACCOUNT = os.getenv("TQ_ACCOUNT", "你的天勤模拟账号")
TQ_PASSWORD = os.getenv("TQ_PASSWORD", "你的天勤模拟密码")

# 日志配置
logger.remove()
logger.add(
    sys.stderr,
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS zz}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)
try:
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/adapted_frontend_system_v3_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG",
        enqueue=True,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS zz} | {level: <8} | P:{process} | T:{thread} | {name}:{function}:{line} - {message}",
    )
except PermissionError:
    logger.error("无法创建日志目录 'logs/' 或写入文件，请检查权限。")


# --- 数据模型 ---
class TaskStatusEnum(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class TradingTaskInMemory:
    _id_counter = 0

    def __init__(self, name: str, symbol: str, target_volume: int = 0):
        TradingTaskInMemory._id_counter += 1
        self.id: int = TradingTaskInMemory._id_counter
        self.name: str = name
        self.symbol: str = symbol
        self.status: TaskStatusEnum = TaskStatusEnum.PENDING
        now = datetime.now(timezone.utc).isoformat()
        self.created_at: str = now
        self.updated_at: str = now
        self.target_volume: int = target_volume
        self.asyncio_task_ref: Optional[asyncio.Task] = None
        self.tqapi_instance_ref: Optional[TqApi] = None


in_memory_tasks_db: Dict[int, TradingTaskInMemory] = {}
active_apis: Dict[int, TqApi] = {}


# --- 应用生命周期 ---
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("应用启动: 适配版交易系统后端V3 (Lifespan)")
    yield
    logger.info("应用关闭: 正在清理所有任务...")
    # 取消并关闭所有任务
    for task_id, task in list(in_memory_tasks_db.items()):
        if task.asyncio_task_ref and not task.asyncio_task_ref.done():
            task.asyncio_task_ref.cancel()
            try:
                await asyncio.wait_for(task.asyncio_task_ref, timeout=5.0)
            except Exception:
                pass
        api = active_apis.pop(task_id, None) or task.tqapi_instance_ref
        if api:
            try:
                await asyncio.get_event_loop().run_in_executor(None, api.close)
            except Exception:
                pass
        del in_memory_tasks_db[task_id]
    logger.info("所有任务已清理完毕。")


# --- FastAPI 应用 ---
app = FastAPI(title="适配前端交易系统 V3", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Socket.IO 服务器实例和挂载 ---
sio = socketio.AsyncServer(
    async_mode="asgi", cors_allowed_origins="*", logger=False, engineio_logger=False
)
# socketio_path 保持 "/"
sio_app = socketio.ASGIApp(sio, socketio_path="/")
# 挂载到 /ws
app.mount("/ws", sio_app)


# --- 辅助函数 ---
def update_task_status(
    task_id: int, status: TaskStatusEnum, remark: Optional[str] = None
):
    task = in_memory_tasks_db.get(task_id)
    if task:
        task.status = status
        task.updated_at = datetime.now(timezone.utc).isoformat()
        msg = f"任务 {task_id} 状态更新为 {status.value}"
        if remark:
            msg += f" ({remark})"
        logger.info(msg)


# --- 核心任务逻辑和 emit 函数 ---
async def emit_quote_update_for_frontend(task_id: int, quote: Quote, symbol: str):
    await sio.emit(
        "quote_update",
        {
            "task_id": task_id,
            "symbol": symbol,
            "last_price": quote.last_price,
            "bid_price1": quote.bid_price1,
            "ask_price1": quote.ask_price1,
            "datetime": quote.datetime,
        },
        namespace="/",
        room=f"task_{task_id}",
    )


async def emit_account_update_for_frontend(task_id: int, account_info: dict):
    await sio.emit(
        "account_update",
        {
            "task_id": task_id,
            "balance": account_info.get("balance"),
            "available": account_info.get("available"),
            "margin": account_info.get("margin"),
            "risk_ratio": account_info.get("risk_ratio"),
            "datetime": datetime.now(timezone.utc).isoformat(),
        },
        namespace="/",
        room=f"task_{task_id}",
    )


async def run_task_logic_for_frontend(
    task_id: int, api: TqApi, symbol: str, target_volume: int
):
    try:
        quote = api.get_quote(symbol)
        account = api.get_account()
        logger.info(
            f"[Task {task_id}] 启动策略，合约: {symbol}，目标手数: {target_volume}"
        )

        while True:
            api.wait_update()
            await emit_quote_update_for_frontend(task_id, quote, symbol)
            await emit_account_update_for_frontend(task_id, account)
            await asyncio.sleep(0.5)

    except asyncio.CancelledError:
        logger.warning(f"[Task {task_id}] 被取消。清理资源中...")
        update_task_status(task_id, TaskStatusEnum.STOPPED, "任务被取消")
    except TqTimeoutError as e:
        logger.error(f"[Task {task_id}] 超时异常: {str(e)}")
        update_task_status(task_id, TaskStatusEnum.ERROR, "TqSdk 超时")
    except Exception as e:
        logger.exception(f"[Task {task_id}] 运行出错: {e}")
        update_task_status(task_id, TaskStatusEnum.ERROR, str(e))
    finally:
        api.close()
        active_apis.pop(task_id, None)
        logger.info(f"[Task {task_id}] 已关闭 TqApi")


# --- Pydantic 模型 ---
class TaskCreateSchema(pydantic.BaseModel):
    name: Optional[str]
    symbol: str
    target_volume: int = 0


class TaskResponseSchema(pydantic.BaseModel):
    id: int
    name: str
    symbol: str
    status: TaskStatusEnum
    created_at: str
    updated_at: Optional[str]
    target_volume: int

    model_config = pydantic.ConfigDict(from_attributes=True)


# --- HTTP API 端点 ---
@app.get("/", tags=["通用"])
async def read_root_api():
    return {"message": "适配版交易系统后端V3运行中"}


@app.post(
    "/api/tasks", response_model=TaskResponseSchema, status_code=201, tags=["任务管理"]
)
async def create_task_api(task_data: TaskCreateSchema):
    name = task_data.name or (
        f"策略_{task_data.symbol}_vol{task_data.target_volume}"
        if task_data.target_volume != 0
        else f"监控_{task_data.symbol}"
    )
    # 去重
    for t in in_memory_tasks_db.values():
        if (
            t.name == name
            and t.symbol == task_data.symbol
            and t.target_volume == task_data.target_volume
        ):
            raise HTTPException(status_code=409, detail=f"任务 '{name}' 已存在。")
    new_task = TradingTaskInMemory(name, task_data.symbol, task_data.target_volume)
    in_memory_tasks_db[new_task.id] = new_task
    # 创建 TqApi
    api = TqApi(
        auth=TqAuth(user_name=TQ_ACCOUNT, password=TQ_PASSWORD), account=TqSim()
    )
    new_task.tqapi_instance_ref = api
    active_apis[new_task.id] = api
    # 启动任务逻辑
    coro = run_task_logic_for_frontend(
        new_task.id, api, new_task.symbol, new_task.target_volume
    )
    new_task.asyncio_task_ref = asyncio.create_task(coro, name=f"Task-{new_task.id}")
    update_task_status(new_task.id, TaskStatusEnum.RUNNING, "已启动")
    return TaskResponseSchema(**new_task.__dict__)


@app.get("/api/tasks", response_model=List[TaskResponseSchema], tags=["任务管理"])
async def get_tasks_api(skip: int = 0, limit: int = 100):
    tasks = sorted(in_memory_tasks_db.values(), key=lambda x: x.id, reverse=True)
    sliced = tasks[skip : skip + limit]
    return [TaskResponseSchema(**t.__dict__) for t in sliced]


@app.get("/api/tasks/{task_id}", response_model=TaskResponseSchema, tags=["任务管理"])
async def get_task_api(task_id: int):
    t = in_memory_tasks_db.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"任务 ID {task_id} 未找到。")
    return TaskResponseSchema(**t.__dict__)


@app.post(
    "/api/tasks/{task_id}/start", response_model=TaskResponseSchema, tags=["任务管理"]
)
async def start_task_api(task_id: int):
    t = in_memory_tasks_db.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"任务 ID {task_id} 未找到。")
    if t.asyncio_task_ref and not t.asyncio_task_ref.done():
        return TaskResponseSchema(**t.__dict__)
    # 清理旧实例
    if t.asyncio_task_ref:
        t.asyncio_task_ref.cancel()
    if t.tqapi_instance_ref:
        await asyncio.get_event_loop().run_in_executor(None, t.tqapi_instance_ref.close)
        active_apis.pop(task_id, None)
    # 重启
    api = TqApi(
        auth=TqAuth(user_name=TQ_ACCOUNT, password=TQ_PASSWORD), account=TqSim()
    )
    t.tqapi_instance_ref = api
    active_apis[task_id] = api
    coro = run_task_logic_for_frontend(task_id, api, t.symbol, t.target_volume)
    t.asyncio_task_ref = asyncio.create_task(coro, name=f"Task-{task_id}-restart")
    update_task_status(task_id, TaskStatusEnum.RUNNING, "重新启动")
    return TaskResponseSchema(**t.__dict__)


@app.post(
    "/api/tasks/{task_id}/stop", response_model=TaskResponseSchema, tags=["任务管理"]
)
async def stop_task_api(task_id: int):
    t = in_memory_tasks_db.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail=f"任务 ID {task_id} 未找到。")
    if t.asyncio_task_ref and not t.asyncio_task_ref.done():
        t.asyncio_task_ref.cancel()
        try:
            await asyncio.wait_for(t.asyncio_task_ref, timeout=5.0)
        except Exception:
            pass
    if t.tqapi_instance_ref:
        await asyncio.get_event_loop().run_in_executor(None, t.tqapi_instance_ref.close)
        active_apis.pop(task_id, None)
    update_task_status(task_id, TaskStatusEnum.STOPPED, "已停止")
    return TaskResponseSchema(**t.__dict__)


# --- Socket.IO 事件处理 ---
@sio.event
async def connect(sid: str, environ: dict, auth: Optional[dict] = None):
    logger.info(f"Socket.IO 客户端已连接: {sid}")
    await sio.emit("welcome", {"message": "连接成功！"}, room=sid)


@sio.event
async def disconnect(sid: str):
    logger.info(f"Socket.IO 客户端断开: {sid}")


@sio.event
async def join_task_room(sid: str, data: Any, ack: any) -> Dict[str, Any]:
    task_id = data.get("task_id")
    if not task_id or task_id not in in_memory_tasks_db:
        return {"status": "error", "message": "无效的任务 ID"}
    await sio.enter_room(sid, f"task_{task_id}")
    logger.info(f"Socket.IO 客户端 {sid} 加入任务房间 task_{task_id}")
    return {"status": "ok", "message": f"加入任务 {task_id} 房间成功"}


@sio.event
async def leave_task_room(sid: str, data: Any, ack: any) -> Dict[str, Any]:
    task_id = data.get("task_id")
    if not task_id or task_id not in in_memory_tasks_db:
        return {"status": "error", "message": "无效的任务 ID"}
    await sio.leave_room(sid, f"task_{task_id}")
    logger.info(f"Socket.IO 客户端 {sid} 离开任务房间 task_{task_id}")
    return {"status": "ok", "message": f"离开任务 {task_id} 房间成功"}


# 如果需要，将 _handle_join 和 _handle_leave 实现粘贴于此

if __name__ == "__main__":
    import uvicorn

    logger.info("启动 Uvicorn on http://0.0.0.0:8000 ws路径=/ws socketio_path=/")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info", reload=False)
