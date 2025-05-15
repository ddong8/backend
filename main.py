import asyncio
import enum  # 用于创建枚举类型
import os
import sys
from contextlib import asynccontextmanager  # 导入异步上下文管理器
from datetime import datetime, timezone  # 用于处理带时区的时间
from typing import Any, AsyncGenerator, Dict  # AsyncGenerator 用于 lifespan

import socketio  # 用于 WebSocket 通信
from dotenv import load_dotenv  # 用于从 .env 文件加载环境变量
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from tqsdk import TqApi, TqAuth  # 天勤SDK核心组件
from tqsdk.objs import Order  # 天勤SDK中的订单和行情对象

# --- 全局变量与配置 ---
load_dotenv() # 加载 .env 文件中的环境变量 (如果存在)

# 从环境变量或默认值获取天勤账户信息
TQ_ACCOUNT = os.getenv("TQ_ACCOUNT", "你的天勤模拟账号")
TQ_PASSWORD = os.getenv("TQ_PASSWORD", "你的天勤模拟密码")

# Loguru 日志配置
logger.add(sys.stderr, level="INFO") # 添加一个新的标准错误输出，级别为 INFO
logger.add("logs/trading_system_nod_db_{time}.log", rotation="1 day", retention="7 days", level="DEBUG") # 添加文件日志，每天轮换，保留7天，级别为 DEBUG

# 内存中存储活动任务和API实例
# active_trading_tasks: 键为任务ID (例如 "strategy_合约代码_目标手数"), 值为 asyncio.Task 对象
active_trading_tasks: Dict[str, asyncio.Task] = {}
# active_apis: 键为任务ID, 值为对应的 TqApi 实例
active_apis: Dict[str, TqApi] = {}


# --- 应用生命周期管理 (Lifespan) ---
@asynccontextmanager
async def lifespan(app_instance: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI 应用的生命周期管理器。
    在应用启动前执行 yield 之前的代码 (启动逻辑)。
    在应用关闭后执行 yield 之后的代码 (关闭逻辑)。
    """
    # --- 启动逻辑 ---
    logger.info("应用启动 (通过 lifespan)...")
    logger.info(f"正在使用天勤账户: {TQ_ACCOUNT}")
    # 如果需要，可以在这里初始化其他应用级资源
    # 例如: app_instance.state.some_resource = await setup_resource()

    yield  # 应用在此处运行

    # --- 关闭逻辑 ---
    logger.info("应用关闭序列已启动 (通过 lifespan)...")
    # 创建任务和API字典的副本进行迭代，因为在迭代过程中可能会删除项目
    tasks_to_cancel = list(active_trading_tasks.items())
    apis_to_close = list(active_apis.items())

    # 取消所有活动的 asyncio 任务
    for task_id, task in tasks_to_cancel:
        if not task.done(): # 如果任务尚未完成
            logger.info(f"正在取消任务 {task_id} (应用关闭)...")
            task.cancel() # 请求取消任务
            try:
                # 等待任务实际完成取消，设置超时以防任务无法正常取消
                await asyncio.wait_for(task, timeout=5.0)
                logger.info(f"任务 {task_id} 已取消并完成。")
            except asyncio.CancelledError:
                logger.info(f"任务 {task_id} 成功取消 (符合预期)。")
            except asyncio.TimeoutError:
                logger.warning(f"任务 {task_id} 在关闭期间未能及时取消/完成。")
            except Exception as e:
                logger.error(f"关闭期间取消任务 {task_id} 时发生错误: {e}")
        if task_id in active_trading_tasks: # 再次检查并移除，以防万一
            del active_trading_tasks[task_id]

    # 关闭所有活动的 TqApi 实例
    for task_id, api in apis_to_close:
        logger.info(f"正在关闭任务 {task_id} 的 TqApi 实例 (应用关闭)...")
        try:
            await api.close() # 关闭天勤API连接
            logger.info(f"任务 {task_id} 的 TqApi 实例已关闭。")
        except Exception as e:
            logger.error(f"关闭期间关闭任务 {task_id} 的 TqApi 实例时发生错误: {e}")
        if task_id in active_apis: # 再次检查并移除
            del active_apis[task_id]

    logger.info("所有活动的 TqApi 实例已关闭，任务已处理完毕 (应用关闭)。")
    # 如果在启动时初始化了其他资源，在此处释放它们
    # 例如: await app_instance.state.some_resource.close()


# FastAPI 应用实例 - 使用 lifespan 进行生命周期管理
app = FastAPI(title="最小化多任务交易系统 (无数据库, 使用 Lifespan)", lifespan=lifespan)

# CORS (跨源资源共享) 中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源 (生产环境应配置具体来源)
    allow_credentials=True, # 允许携带凭证 (cookies, authorization headers)
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有 HTTP 头部
)

# Socket.IO 服务器实例
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*") # 异步模式, 允许所有来源的 CORS
socket_app = socketio.ASGIApp(sio, app) # 将 Socket.IO 应用挂载到 FastAPI 应用上


# --- 订单状态枚举 (内存中使用) ---
class OrderStatus(enum.Enum):
    PENDING = "PENDING"     # 待处理 (本地状态，尚未发送到交易所)
    OPEN = "OPEN"           # 已报单 (已发送到交易所，等待撮合)
    FILLED = "FILLED"       # 已成交
    CANCELLED = "CANCELLED" # 已撤单
    REJECTED = "REJECTED"   # 已拒绝 (交易所拒绝)
    ERROR = "ERROR"         # 错误状态



async def emit_trade_update(trade_details: Dict[str, Any]):
    """通过 Socket.IO 发送交易/订单更新。参数为一个包含交易详情的字典。"""
    await sio.emit("trade_update", trade_details) # 推送交易更新数据
    # 同时记录一条日志
    logger.warning(
        f"交易更新: 任务 {trade_details.get('task_id')}, 合约 {trade_details.get('symbol')}, 状态 {trade_details.get('status')}",
        task_id=trade_details.get('task_id')
    )

# --- 交易逻辑 (tqSDK) ---
async def simple_trading_strategy(
    api: TqApi,      # TqApi 实例
    symbol: str,     # 交易合约代码, 例如 "SHFE.rb2410"
    target_volume: int # 目标持仓手数 (正数为多头, 负数为空头 - 此处简化为只处理绝对值开仓)
):
    """
    一个非常简单的交易策略示例：
    为指定合约开仓到目标手数。
    """
    task_id = f"strategy_{symbol}_{target_volume}" # 为此策略任务生成唯一ID
    logger.warning(f"启动交易策略: 合约 {symbol}, 目标手数 {target_volume}", task_id=task_id)

    # 内存中表示当前交易/订单的详情
    current_trade_details = {
        "task_id": task_id,
        "tq_order_id": None, # 天勤订单ID，下单后获取
        "symbol": symbol,
        "direction": "BUY" if target_volume > 0 else "SELL", # 根据目标手数决定买卖方向
        "offset": "OPEN", # 简化处理：总是开仓
        "volume": abs(target_volume), # 交易手数 (取绝对值)
        "price": None, # 下单价格或最终成交价格
        "status": OrderStatus.PENDING.value, # 初始状态为待处理
        "created_at": datetime.now(timezone.utc).isoformat(), # 创建时间
        "updated_at": datetime.now(timezone.utc).isoformat(), # 最后更新时间
        "remark": f"目标持仓 {target_volume} 为合约 {symbol}" # 备注
    }
    await emit_trade_update(current_trade_details.copy()) # 推送初始状态

    try:
        quote = api.get_quote(symbol) # 获取合约的最新行情
        # 检查行情数据是否有效
        if not quote or not hasattr(quote, 'last_price') or quote.last_price == float('nan'):
            logger.warning(f"获取合约 {symbol} 的有效行情失败。", level="error", task_id=task_id)
            current_trade_details.update({
                "status": OrderStatus.REJECTED.value,
                "remark": "获取行情失败",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            await emit_trade_update(current_trade_details.copy())
            return # 获取行情失败，策略终止

        logger.warning(f"合约 {symbol} 当前行情: 最新价 {quote.last_price}", task_id=task_id)

        direction = current_trade_details["direction"]
        offset = current_trade_details["offset"]
        volume_to_trade = current_trade_details["volume"]

        # 确定下单价格 (简化：使用对手价，买入用卖一价，卖出用买一价)
        order_price = quote.ask_price1 if direction == "BUY" else quote.bid_price1
        # 如果对手价无效 (例如涨跌停封板，或值为0/inf)，则尝试使用最新价
        if not order_price or order_price == float('inf') or order_price == float('-inf') or order_price == 0:
            order_price = quote.last_price
        # 如果最新价也无效，则无法下单
        if not order_price or order_price == float('inf') or order_price == float('-inf') or order_price == 0:
            logger.warning(f"无法为合约 {symbol} 确定有效的下单价格。行情: ask1={quote.ask_price1}, bid1={quote.bid_price1}, last={quote.last_price}", level="error", task_id=task_id)
            current_trade_details.update({
                "status": OrderStatus.REJECTED.value,
                "remark": "无法确定有效的下单价格",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            await emit_trade_update(current_trade_details.copy())
            return

        logger.warning(f"准备下单: {direction} {offset} 合约 {symbol}, 手数: {volume_to_trade}, 价格: {order_price}", task_id=task_id)
        # 通过 TqApi 插入订单
        order: Order = api.insert_order(
            symbol=symbol,
            direction=direction,
            offset=offset,
            volume=volume_to_trade,
            limit_price=order_price # 限价单
        )
        # 更新内存中的订单详情
        current_trade_details.update({
            "tq_order_id": order.order_id, # 获取天勤订单ID
            "price": order_price,          # 记录下单价格
            "status": OrderStatus.OPEN.value, # 状态更新为已报单
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        await emit_trade_update(current_trade_details.copy()) # 推送更新

        # 循环等待订单状态变化，直到订单出错或进入最终状态 (is_dead)
        # 同时检查 api.is_running() 确保在API关闭时能正确退出循环
        while not order.is_error and not order.is_dead:
            api.wait_update() # 等待TqSDK推送更新
            # 检查订单的关键字段是否有变化
            if api.is_changing(order, ["status", "volume_orign", "volume_left", "last_msg", "trade_price"]):
                logger.warning(f"订单 {order.order_id} 状态: {order.status}, 已成交/总手数: {order.volume_left}/{order.volume_orign}, 信息: {order.last_msg}", task_id=task_id)
                new_status_str = current_trade_details["status"] # 当前状态字符串
                # 根据天勤的订单状态更新本地状态
                if order.status == "AL成交" or order.status == "FINISHED": # FINISHED 是天勤内部C++接口的状态
                    new_status_str = OrderStatus.FILLED.value
                    current_trade_details["price"] = order.trade_price or order_price # 更新为实际成交价
                    current_trade_details["remark"] = f"以价格 {current_trade_details['price']} 成交"
                elif order.status == "CANCELLED" or order.status == "已撤单":
                    new_status_str = OrderStatus.CANCELLED.value
                elif order.status == "REJECTED" or order.status == "已拒绝":
                    new_status_str = OrderStatus.REJECTED.value
                # 可以根据需要映射其他天勤订单状态

                # 如果状态发生变化，则更新并推送
                if new_status_str != current_trade_details["status"]:
                    current_trade_details["status"] = new_status_str
                current_trade_details["updated_at"] = datetime.now(timezone.utc).isoformat()
                await emit_trade_update(current_trade_details.copy())

        # 订单循环结束后处理最终状态
        if order.is_error: # 如果订单出错
            logger.warning(f"订单 {order.order_id} 发生错误: {order.last_msg}", level="error", task_id=task_id)
            current_trade_details.update({
                "status": OrderStatus.ERROR.value,
                "remark": order.last_msg or "未知的订单错误",
                "updated_at": datetime.now(timezone.utc).isoformat()
            })
            await emit_trade_update(current_trade_details.copy())
        elif order.is_dead: # 如果订单进入最终状态 (非错误)
             logger.warning(f"订单 {order.order_id} 处理完成。最终状态: {order.status}", task_id=task_id)
             # 再次确认最终状态，以防循环中未能正确设置 (例如，直接进入 FINISHED 但未触发 is_changing)
             final_status_map = {
                 "AL成交": OrderStatus.FILLED.value, "FINISHED": OrderStatus.FILLED.value,
                 "CANCELLED": OrderStatus.CANCELLED.value, "已撤单": OrderStatus.CANCELLED.value,
                 "REJECTED": OrderStatus.REJECTED.value, "已拒绝": OrderStatus.REJECTED.value,
             }
             final_status = final_status_map.get(order.status, current_trade_details["status"]) # 如果状态不在map中，保持原样
             # 如果状态有变，或者之前是OPEN状态但现在已结束，则更新
             if final_status != current_trade_details["status"] or current_trade_details["status"] == OrderStatus.OPEN.value:
                current_trade_details["status"] = final_status
                if final_status == OrderStatus.FILLED.value: # 如果是成交，确保价格和备注正确
                    current_trade_details["price"] = order.trade_price or current_trade_details["price"]
                    current_trade_details["remark"] = f"以价格 {current_trade_details['price']} 成交"
                current_trade_details["updated_at"] = datetime.now(timezone.utc).isoformat()
                await emit_trade_update(current_trade_details.copy())

    except asyncio.CancelledError: # 捕获任务被取消的异常
        logger.warning(f"策略任务 {task_id} (合约 {symbol}) 被取消。", level="warning", task_id=task_id)
        current_trade_details.update({
            "status": OrderStatus.CANCELLED.value, # 可以定义一个特定的“策略取消”状态
            "remark": "策略任务被取消",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        await emit_trade_update(current_trade_details.copy())
        raise # 重新抛出 CancelledError，以便上层任务管理逻辑能感知到
    except Exception as e: # 捕获其他所有异常
        logger.exception(f"交易策略 {symbol} 执行时发生未捕获的错误: {e}") # 使用 logger.exception 记录完整堆栈跟踪
        logger.warning(f"策略执行异常 ({symbol}): {e}", level="error", task_id=task_id)
        current_trade_details.update({
            "status": OrderStatus.ERROR.value,
            "remark": str(e),
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        await emit_trade_update(current_trade_details.copy())
    finally:
        logger.warning(f"交易策略 {symbol} 执行完毕或退出。", task_id=task_id)
        # active_trading_tasks 和 active_apis 的清理由API端点和lifespan的关闭逻辑管理

# --- FastAPI HTTP API 端点 ---
@app.post("/trade/start_strategy/{symbol}")
async def start_trading_strategy_endpoint(
    symbol: str,      # 路径参数：合约代码
    target_volume: int, # 查询参数：目标手数
):
    """
    启动一个简单的交易策略任务。
    为每个策略任务创建一个新的 TqApi 实例。
    """
    task_id = f"strategy_{symbol}_{target_volume}"
    # 检查是否已有同名任务在运行
    if task_id in active_trading_tasks and not active_trading_tasks[task_id].done():
        raise HTTPException(status_code=400, detail=f"交易任务 {task_id} 已在运行。")

    try:
        
        # 创建 TqApi 实例，不启动 web_gui
        api = TqApi(auth=TqAuth(user_name=TQ_ACCOUNT, password=TQ_PASSWORD))
        active_apis[task_id] = api # 存储 API 实例

        # 创建并启动异步策略任务
        strategy_task = asyncio.create_task(
            simple_trading_strategy(api, symbol, target_volume)
        )
        active_trading_tasks[task_id] = strategy_task # 存储任务实例

        logger.warning(f"交易策略任务 {task_id} (合约 {symbol}) 已创建。", task_id=task_id)
        return {"message": "交易策略已启动。", "task_id": task_id, "symbol": symbol, "target_volume": target_volume}

    except Exception as e: # 启动过程中的任何异常
        logger.error(f"启动交易策略 {symbol} 失败: {e}")
        # 如果 API 实例已创建但任务启动失败，尝试关闭 API
        if task_id in active_apis:
            try:
                await active_apis[task_id].close()
            except Exception as api_close_err:
                logger.error(f"关闭失败任务 {task_id} 的 API 时出错: {api_close_err}")
            del active_apis[task_id]
        raise HTTPException(status_code=500, detail=f"启动策略失败: {str(e)}")

@app.post("/trade/stop_strategy/{task_id}")
async def stop_trading_strategy_endpoint(task_id: str): # 路径参数：任务ID
    """停止指定的交易策略任务。"""
    if task_id not in active_trading_tasks:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 未找到。")

    task = active_trading_tasks[task_id]
    api_instance = active_apis.get(task_id) # 获取对应的 API 实例

    if not task.done(): # 如果任务尚未完成
        task.cancel() # 请求取消任务
        logger.warning(f"已请求取消交易任务 {task_id}。", task_id=task_id)
        try:
            await task # 等待任务实际完成或被取消
        except asyncio.CancelledError:
            logger.warning(f"交易任务 {task_id} 成功取消。", task_id=task_id)
        except Exception as e: # 捕获任务在取消过程中可能抛出的其他异常
            logger.warning(f"任务 {task_id} (取消请求后)发生异常: {e}", level="error", task_id=task_id)

    # 关闭与此任务关联的 TqApi 实例
    if api_instance:
        try:
            api_instance.close()
            logger.warning(f"任务 {task_id} 的 TqApi 实例已关闭。", task_id=task_id)
        except Exception as e:
            logger.warning(f"关闭任务 {task_id} 的 TqApi 实例时出错: {e}", level="error", task_id=task_id)
        if task_id in active_apis: # 从字典中移除
            del active_apis[task_id]

    if task_id in active_trading_tasks: # 从字典中移除任务记录
         del active_trading_tasks[task_id]

    return {"message": f"交易任务 {task_id} 已停止 (或已处理取消请求)。"}

@app.get("/tasks/status")
async def get_tasks_status():
    """获取所有活动交易任务的状态。"""
    statuses = {}
    for task_id, task in active_trading_tasks.items():
        status_info = {
            "done": task.done(), # 任务是否已完成
            "cancelled": task.cancelled(), # 任务是否被取消
        }
        # 如果任务已完成且未被取消，检查是否有未处理的异常
        if task.done() and not task.cancelled():
            try:
                task.result() # 如果任务有异常，调用 result() 会重新抛出它
                status_info["exception"] = None
            except Exception as e:
                status_info["exception"] = str(e)
        else:
            status_info["exception"] = None # 对于未完成或已取消的任务，不检查异常
        statuses[task_id] = status_info
    return statuses

# --- Socket.IO 事件处理器 ---
@sio.event
async def connect(sid, environ): # 当客户端连接时触发
    """处理新的 Socket.IO 客户端连接。"""
    logger.warning(f"客户端已连接: {sid}")
    # 可以向新连接的客户端发送一些初始状态信息
    # await sio.emit('initial_data', {'message': '欢迎!'}, room=sid)

@sio.event
async def disconnect(sid): # 当客户端断开连接时触发
    """处理 Socket.IO 客户端断开连接。"""
    logger.warning(f"客户端已断开: {sid}")

# (可以添加更多 @sio.event 来处理来自客户端的消息)
# @sio.event
# async def client_message(sid, data):
#     await emit_log(f"收到来自客户端 {sid} 的消息: {data}")
#     await sio.emit('server_response', {'received': data}, room=sid)


# --- Uvicorn 服务器运行入口 ---
if __name__ == "__main__":
    import uvicorn
    logger.info("正在启动 Uvicorn 服务器...")
    # 使用字符串 "main:socket_app" 以便 uvicorn 的 --reload 功能可以正常工作
    uvicorn.run("main:socket_app", host="0.0.0.0", port=8000, log_level="info", reload=False)