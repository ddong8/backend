# backend/task_manager.py
import asyncio  # Python 异步IO库
from loguru import logger  # 导入 Loguru 日志记录器
from tqsdk import TqApi, TqSim, TqAuth, TqAccount, TqKq  # TQSDK 核心组件
from tqsdk.exceptions import TqTimeoutError  # TQSDK 特定异常
from typing import Dict, Callable, Any, Optional  # Python 类型提示
from sqlalchemy.ext.asyncio import AsyncSession  # SQLAlchemy 异步会话

from models import TradingTask, TaskStatus  # 导入数据库模型和状态枚举
from config import TQ_ACCOUNT, TQ_PASSWORD  # 从项目配置导入 TQSDK 凭证


class TaskManager:
    """
    管理 TQSDK 交易任务的创建、启动、停止和数据推送。
    每个交易任务对应一个独立的 TQSDK 实例。
    """

    def __init__(self, sio_server: Any, db_session_factory: Callable[[], AsyncSession]):
        """
        初始化 TaskManager。
        :param sio_server: Socket.IO 服务器实例，用于向客户端推送数据。
        :param db_session_factory: 一个返回 SQLAlchemy AsyncSession 的可调用对象 (工厂函数)。
        """
        self.sio = sio_server
        self.db_session_factory = db_session_factory
        # 存储活动的 asyncio 任务，键为 task_id (数据库中的ID)，值为对应的 asyncio.Task 实例
        self.active_asyncio_tasks: Dict[int, asyncio.Task] = {}
        # 存储活动的 TqApi 实例，键为 task_id，值为对应的 TqApi 实例
        self.active_tqapis: Dict[int, TqApi] = {}

    async def _run_tq_for_task(self, task_model: TradingTask):
        """
        为单个交易任务运行 TQSDK 核心循环。
        此方法在一个独立的 asyncio.Task 中执行。
        它会连接到 TQSDK，订阅行情，并在数据更新时通过 Socket.IO 推送给客户端。
        """
        task_id: int = task_model.id
        symbol: str = task_model.symbol
        room_name: str = f"task_{task_id}"  # 特定于此任务的 Socket.IO 房间名
        tqapi_instance: Optional[TqApi] = None  # TqApi 实例变量
        error_occurred_in_tq_loop: bool = False  # 标记 TQSDK 循环中是否发生错误

        logger.info(f"[任务 {task_id}] 正在为标的 '{symbol}' 启动 TQSDK 运行器...")

        try:
            # 获取当前事件循环 (asyncio event loop)
            current_loop = asyncio.get_event_loop()
            # 创建 TqApi 实例
            # auth: 身份验证信息
            # event_loop: 传递当前事件循环，确保 TqApi 在正确的循环中运行
            # auto_reconnect=True: 当连接意外断开时，TQSDK 会自动尝试重新连接
            # _stock: (可选) 如果需要股票行情，可以设为 True，需要相应权限
            tqapi_instance = TqApi(
                account=TqKq(),  # 对于只获取行情的场景，可以不指定 TqAccount 或设为 None
                # 如果需要交易或账户信息，应传入 TqKq (快期模拟) 或实盘 TqAccount
                auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD),  # 使用从配置中读取的凭证
                event_loop=current_loop,
                auto_reconnect=True,
                # _stock=False # 根据是否需要股票行情调整
            )
            self.active_tqapis[task_id] = tqapi_instance  # 存储活动的 TqApi 实例

            logger.info(f"[任务 {task_id}] TQSDK 实例已创建，正在连接...")
            # 等待 TQSDK 连接成功或超时 (如果 TQSDK 内部有此机制，或通过 get_quote 间接确认)
            # 通常 get_quote 会阻塞直到获取到初始快照或连接失败

            quote = tqapi_instance.get_quote(symbol)  # 获取指定标的的行情对象
            # account_info: Optional[TqAccount] = tqapi_instance.get_account() # (可选) 获取账户信息

            logger.info(f"[任务 {task_id}] 已获取标的 '{symbol}' 的行情对象。")

            # 推送初始行情数据 (如果 TQSDK 已连接并获取到数据)
            if quote and hasattr(quote, "last_price") and quote.last_price is not None:
                await self._emit_quote_update(task_id, room_name, quote)

            # 注册 TQSDK 数据更新通知，并进入主事件循环
            async with tqapi_instance.register_update_notify(
                quote
            ) as update_chan:  # 可以传入多个对象
                logger.info(f"[任务 {task_id}] TQSDK 更新通知已注册，进入监听循环...")
                while (
                    task_id in self.active_asyncio_tasks
                ):  # 只要任务仍在活动列表中，就继续循环
                    try:
                        # 等待 TQSDK 数据更新信号，设置超时以允许任务优雅停止
                        has_update = await asyncio.wait_for(
                            update_chan.recv(), timeout=1.0
                        )
                        if not has_update:  # 如果 recv() 返回 False (可能在关闭时发生)
                            logger.info(
                                f"[任务 {task_id}] update_chan.recv() 返回 False，可能正在关闭。"
                            )
                            continue
                    except asyncio.TimeoutError:
                        # 超时后，再次检查任务是否已被外部请求停止
                        if task_id not in self.active_asyncio_tasks:
                            logger.info(
                                f"[任务 {task_id}] 在等待TQSDK更新超时后，检测到停止信号。"
                            )
                            break  # 退出主循环
                        continue  # 如果任务仍然活动，继续等待下一次更新
                    except (
                        TqTimeoutError
                    ):  # TQSDK 内部的超时 (例如，如果 wait_update 被调用且超时)
                        logger.warning(f"[任务 {task_id}] TQSDK 内部操作超时。")
                        if not tqapi_instance.is_online():  # 检查 TQSDK 是否在线
                            logger.error(
                                f"[任务 {task_id}] TQSDK 已离线。自动重连机制应会尝试恢复。"
                            )
                        continue
                    except TqError as tq_err:  # 捕获其他 TQSDK 特定错误
                        logger.error(
                            f"[任务 {task_id}] TQSDK 发生错误: {tq_err}", exc_info=True
                        )
                        error_occurred_in_tq_loop = True
                        await self.sio.emit(
                            "task_error",
                            {"task_id": task_id, "error": f"TQSDK 内部错误: {tq_err}"},
                            room=room_name,
                        )
                        # 根据错误严重性决定是否跳出循环或继续
                        # if tq_err.is_致命_error(): break
                        continue

                    # 检查行情数据是否有更新
                    if tqapi_instance.is_updated(
                        quote, "last_price"
                    ):  # 只在最新价等关键字段更新时推送
                        await self._emit_quote_update(task_id, room_name, quote)

                    # (可选) 检查并推送账户信息更新
                    # if account_info and tqapi_instance.is_updated(account_info):
                    #     await self._emit_account_update(task_id, room_name, account_info)

                    await asyncio.sleep(
                        0.005
                    )  # 短暂释放CPU控制权，给其他协程运行的机会 (非常短的 sleep)

        except ConnectionError as conn_err:  # 网络连接相关的错误
            logger.error(
                f"[任务 {task_id}] TQSDK 连接错误 (标的: {symbol}): {conn_err}",
                exc_info=True,
            )
            error_occurred_in_tq_loop = True
            await self.sio.emit(
                "task_error",
                {"task_id": task_id, "error": f"TQSDK 连接失败: {conn_err}"},
                room=room_name,
            )
        except TqAuthError as auth_err:  # TQSDK 认证错误
            logger.error(
                f"[任务 {task_id}] TQSDK 认证失败 (标的: {symbol}): {auth_err}. 请检查账户和密码。",
                exc_info=True,
            )
            error_occurred_in_tq_loop = True
            await self.sio.emit(
                "task_error",
                {"task_id": task_id, "error": f"TQSDK 认证失败: {auth_err}"},
                room=room_name,
            )
        except Exception as e:  # 捕获所有其他未预料的异常
            logger.error(
                f"[任务 {task_id}] TQSDK 运行器发生未捕获的严重错误 (标的: {symbol}): {e}",
                exc_info=True,
            )
            error_occurred_in_tq_loop = True
            # 向客户端推送通用错误信息
            await self.sio.emit(
                "task_error",
                {
                    "task_id": task_id,
                    "error": f"任务运行时发生内部错误: {type(e).__name__}",
                },
                room=room_name,
            )

        finally:
            # --- 清理 TQSDK 相关的资源 ---
            if tqapi_instance:
                try:
                    tqapi_instance.close()  # 确保关闭 TqApi 连接和资源
                    logger.info(f"[任务 {task_id}] TqApi 实例已成功关闭。")
                except Exception as close_err:
                    logger.error(
                        f"[任务 {task_id}] 关闭 TqApi 实例时发生错误: {close_err}",
                        exc_info=True,
                    )

            if task_id in self.active_tqapis:  # 从活动 TqApi 字典中移除
                del self.active_tqapis[task_id]

            # --- 处理任务状态和字典 ---
            # 检查任务是否是由于外部调用 stop_task 而停止的，还是因为内部错误/循环结束
            is_unexpected_exit_or_still_active = False
            if task_id in self.active_asyncio_tasks:
                # 如果任务 ID 仍然在 active_asyncio_tasks 中，说明它不是通过 stop_task() 的正常流程移除的。
                # 这可能意味着 _run_tq_for_task 协程因为一个未在主循环中捕获的异常而提前结束，
                # 或者 TQSDK 内部发生了导致协程退出的严重问题。
                logger.warning(
                    f"[任务 {task_id}] TQSDK 运行器在结束时发现任务仍被标记为活动状态。这通常表示一次非预期的退出。"
                    "正在从活动任务列表中移除。"
                )
                del self.active_asyncio_tasks[task_id]  # 确保从活动任务列表中移除
                is_unexpected_exit_or_still_active = True

            logger.info(f"[任务 {task_id}] TQSDK 运行器 (标的: {symbol}) 已完全停止。")

            # --- 更新数据库状态 ---
            # 如果任务是因为错误 (error_occurred_in_tq_loop) 或意外退出 (is_unexpected_exit_or_still_active) 而停止的，
            # 并且数据库中的状态仍然是 RUNNING，则应将其更新为 ERROR。
            # 正常的停止操作 (通过 stop_task 调用) 会自行更新数据库状态为 STOPPED。
            if error_occurred_in_tq_loop or is_unexpected_exit_or_still_active:
                try:
                    async with self.db_session_factory() as db:  # 获取新的数据库会话
                        db_task_model: Optional[TradingTask] = await db.get(
                            TradingTask, task_id
                        )
                        if db_task_model and db_task_model.status == TaskStatus.RUNNING:
                            logger.info(
                                f"[任务 {task_id}] 由于 TQSDK 运行器的问题，正在更新数据库中任务状态为 ERROR。"
                            )
                            db_task_model.status = TaskStatus.ERROR  # 更新状态为错误
                            await db.commit()  # 提交数据库更改
                except Exception as db_err:
                    logger.error(
                        f"[任务 {task_id}] 在尝试将任务状态更新为 ERROR 时发生数据库错误: {db_err}",
                        exc_info=True,
                    )

    async def _emit_quote_update(self, task_id: int, room_name: str, quote: Any):
        """辅助函数：向客户端推送行情更新。"""
        await self.sio.emit(
            "quote_update",
            {
                "task_id": task_id,
                "symbol": getattr(quote, "instrument_id", None),
                "last_price": getattr(quote, "last_price", None),
                "ask_price1": getattr(quote, "ask_price1", None),
                "bid_price1": getattr(quote, "bid_price1", None),
                "highest": getattr(quote, "highest", None),  # 最高价
                "lowest": getattr(quote, "lowest", None),  # 最低价
                "open": getattr(
                    quote, "open_price", None
                ),  # 开盘价 (TQSDK 中可能是 open_price)
                "close": getattr(
                    quote, "close_price", None
                ),  # (昨)收盘价 (TQSDK 中可能是 pre_close)
                "volume": getattr(quote, "volume", None),
                "datetime": getattr(quote, "datetime", None),  # 行情时间戳
                "timestamp_nano": getattr(
                    quote, "timestamp_nano", None
                ),  # (可选) 更精确的时间戳
            },
            room=room_name,
        )

    async def _emit_account_update(
        self, task_id: int, room_name: str, account: TqAccount
    ):
        """辅助函数：向客户端推送账户信息更新。"""
        await self.sio.emit(
            "account_update",
            {
                "task_id": task_id,
                "account_id": getattr(account, "account_id", None),
                "broker_id": getattr(account, "broker_id", None),
                "available": getattr(account, "available", None),  # 可用资金
                "balance": getattr(account, "balance", None),  # 动态权益
                "static_balance": getattr(account, "static_balance", None),  # 静态权益
                "margin": getattr(account, "margin", None),  # 持仓保证金
                "commission": getattr(account, "commission", None),  # 当日手续费
                "profit": getattr(
                    account, "position_profit", None
                ),  # 持仓盈亏 (浮动盈亏)
                "float_profit": getattr(
                    account, "float_profit", None
                ),  # 也是浮动盈亏，可能与上面一个同义或有细微差别
                "close_profit": getattr(account, "close_profit", None),  # 当日平仓盈亏
                "risk_ratio": getattr(account, "risk_ratio", None),  # 风险度
            },
            room=room_name,
        )

    async def start_task(self, task_model: TradingTask) -> bool:
        """
        启动一个指定的交易任务。
        :param task_model: 从数据库获取的 TradingTask ORM 对象。
        :return: True 如果任务成功启动排队，False 如果任务已在运行或启动失败。
        """
        task_id = task_model.id
        if task_id in self.active_asyncio_tasks:  # 检查任务是否已在活动任务列表中
            logger.warning(
                f"[任务 {task_id}] 尝试启动一个已经在活动列表中的任务 (名称: '{task_model.name}')。"
            )
            return False  # 任务已经在运行

        logger.info(
            f"[任务 {task_id}] 准备启动任务 '{task_model.name}' (标的: {task_model.symbol})..."
        )
        # 创建并存储运行 TQSDK 的 asyncio.Task
        current_loop = asyncio.get_event_loop()
        tq_coroutine = self._run_tq_for_task(task_model)  # 获取要执行的协程
        # 将协程包装成一个 asyncio.Task，并添加到事件循环中调度执行
        tq_runner_task = current_loop.create_task(tq_coroutine)
        self.active_asyncio_tasks[task_id] = (
            tq_runner_task  # 将 asyncio.Task 实例存入活动任务字典
        )
        logger.info(
            f"[任务 {task_id}] 已为标的 '{task_model.symbol}' 成功创建并调度 TQSDK 运行器任务。"
        )

        # 更新数据库中的任务状态为 RUNNING
        try:
            async with self.db_session_factory() as db:  # 获取数据库会话
                # 再次从数据库获取任务对象，确保操作的是当前会话中的受管对象
                db_task_to_update: Optional[TradingTask] = await db.get(
                    TradingTask, task_id
                )
                if db_task_to_update:
                    db_task_to_update.status = TaskStatus.RUNNING  # 更新状态
                    await db.commit()  # 提交更改
                    logger.info(
                        f"[任务 {task_id}] 数据库中任务状态已成功更新为 RUNNING。"
                    )
                else:
                    # 这种情况理论上不应发生，因为 task_model 参数本身就是从数据库查询得到的
                    logger.error(
                        f"[任务 {task_id}] 严重错误：在尝试将数据库状态更新为 RUNNING 时，未找到任务 '{task_model.name}'。"
                        "正在取消刚创建的 TQSDK 运行器任务。"
                    )
                    tq_runner_task.cancel()  # 取消刚创建的 asyncio.Task
                    if task_id in self.active_asyncio_tasks:
                        del self.active_asyncio_tasks[task_id]  # 从活动列表移除
                    if task_id in self.active_tqapis:
                        del self.active_tqapis[task_id]  # 清理 TqApi 实例 (如果已创建)
                    return False  # 启动失败
        except Exception as e:
            logger.error(
                f"[任务 {task_id}] 更新数据库状态为 RUNNING 时发生错误: {e}",
                exc_info=True,
            )
            # 如果数据库更新失败，也应该尝试清理已调度的 TQSDK 任务
            tq_runner_task.cancel()
            if task_id in self.active_asyncio_tasks:
                del self.active_asyncio_tasks[task_id]
            if task_id in self.active_tqapis:
                del self.active_tqapis[task_id]
            return False  # 启动失败

        return True  # 任务成功启动排队

    async def stop_task(self, task_id: int) -> bool:
        """
        停止一个指定的交易任务。
        :param task_id: 要停止的任务的 ID。
        :return: True 如果任务成功请求停止或已停止，False 如果任务不存在或停止过程中发生问题。
        """
        logger.info(f"[任务 {task_id}] 正在请求停止任务...")
        if task_id not in self.active_asyncio_tasks:  # 检查任务是否在活动列表中
            logger.warning(f"[任务 {task_id}] 尝试停止一个当前不在活动列表中的任务。")
            # 检查数据库状态，如果数据库显示为 RUNNING，但管理器中没有，可能需要同步状态
            try:
                async with self.db_session_factory() as db:
                    db_task_to_sync: Optional[TradingTask] = await db.get(
                        TradingTask, task_id
                    )
                    if db_task_to_sync and db_task_to_sync.status == TaskStatus.RUNNING:
                        logger.warning(
                            f"[任务 {task_id}] 任务在管理器中不活跃，但数据库状态仍为 RUNNING。"
                            "正在将数据库状态同步为 STOPPED。"
                        )
                        db_task_to_sync.status = (
                            TaskStatus.STOPPED
                        )  # 将数据库状态更正为 STOPPED
                        await db.commit()
            except Exception as e:
                logger.error(
                    f"[任务 {task_id}] 同步非活动任务的数据库状态时发生错误: {e}",
                    exc_info=True,
                )
            return True  # 即使不在活动列表，也认为“停止”操作已完成（或无需操作）

        # 从 active_asyncio_tasks 字典中移除任务ID。
        # _run_tq_for_task 中的主循环会检查 task_id 是否仍在此字典中，以此作为停止信号。
        tq_runner_task_to_stop: Optional[asyncio.Task] = self.active_asyncio_tasks.pop(
            task_id, None
        )

        if tq_runner_task_to_stop:  # 如果成功从字典中获取到 asyncio.Task 实例
            logger.info(
                f"[任务 {task_id}] 已从活动任务列表移除，正在等待 TQSDK 运行器优雅退出..."
            )
            try:
                # 等待 _run_tq_for_task 协程优雅地结束 (它会检查 active_asyncio_tasks)。
                # 设置一个超时时间，以防协程由于某种原因卡住。
                await asyncio.wait_for(
                    tq_runner_task_to_stop, timeout=7.0
                )  # 增加超时到7秒
                logger.info(f"[任务 {task_id}] TQSDK 运行器已成功优雅停止。")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[任务 {task_id}] TQSDK 运行器未在指定的超时时间内优雅停止。"
                    "将尝试强制取消该任务。"
                )
                tq_runner_task_to_stop.cancel()  # 强制取消 asyncio.Task
                try:
                    await tq_runner_task_to_stop  # 等待取消操作完成 (可能会抛出 CancelledError)
                except asyncio.CancelledError:
                    logger.info(f"[任务 {task_id}] TQSDK 运行器已被成功取消。")
                except Exception as e_after_cancel:  # 捕获取消后可能出现的其他异常
                    logger.error(
                        f"[任务 {task_id}] 等待任务取消完成时发生错误: {e_after_cancel}",
                        exc_info=True,
                    )
            except asyncio.CancelledError:  # 如果在 wait_for 之前任务就被取消了
                logger.info(f"[任务 {task_id}] TQSDK 运行器在等待期间已被取消。")
            except Exception as e_during_stop:  # 捕获等待任务结束时可能发生的其他异常
                logger.error(
                    f"[任务 {task_id}] 在等待 TQSDK 运行器停止时发生错误: {e_during_stop}",
                    exc_info=True,
                )
        else:  # 理论上不应发生，因为上面已检查 task_id in self.active_asyncio_tasks
            logger.error(
                f"[任务 {task_id}] 严重逻辑错误：任务在活动列表中但无法获取其 asyncio.Task 实例。"
            )

        # _run_tq_for_task 的 finally 块会负责关闭 TqApi 实例和从 active_tqapis 中清理。

        # 更新数据库中的任务状态为 STOPPED
        try:
            async with self.db_session_factory() as db:
                db_task_to_update: Optional[TradingTask] = await db.get(
                    TradingTask, task_id
                )
                if db_task_to_update:
                    # 只有当任务当前状态是 RUNNING 或 ERROR 时，才将其更新为 STOPPED。
                    # 如果已经是 PENDING 或 STOPPED，则无需更改。
                    if db_task_to_update.status in [
                        TaskStatus.RUNNING,
                        TaskStatus.ERROR,
                    ]:
                        db_task_to_update.status = TaskStatus.STOPPED
                        await db.commit()
                        logger.info(
                            f"[任务 {task_id}] 数据库中任务状态已成功更新为 STOPPED。"
                        )
                    else:
                        logger.info(
                            f"[任务 {task_id}] 数据库中任务状态为 '{db_task_to_update.status.value}'，"
                            "无需再次更新为 STOPPED。"
                        )
                else:
                    logger.error(
                        f"[任务 {task_id}] 错误：在尝试将数据库状态更新为 STOPPED 时，未找到任务。"
                    )
        except Exception as e:
            logger.error(
                f"[任务 {task_id}] 更新数据库状态为 STOPPED 时发生错误: {e}",
                exc_info=True,
            )
            return False  # 指示停止过程中可能存在问题

        return True  # 任务已成功请求停止

    async def stop_all_tasks(self):
        """
        请求停止所有当前活动的交易任务。
        通常在应用关闭时调用。
        """
        logger.info("正在请求停止所有当前活动的 TQSDK 任务...")
        # 创建当前活动任务 ID 列表的副本进行迭代，
        # 因为 self.active_asyncio_tasks 会在 stop_task 方法中被修改。
        task_ids_to_stop = list(self.active_asyncio_tasks.keys())

        if not task_ids_to_stop:
            logger.info("当前没有活动的 TQSDK 任务需要停止。")
            return

        # 使用 asyncio.gather 并发地请求停止所有任务
        results = await asyncio.gather(
            *(self.stop_task(task_id) for task_id in task_ids_to_stop),
            return_exceptions=True,  # 如果某个 stop_task 调用抛出异常，gather 不会立即失败，而是收集异常
        )

        for task_id, result in zip(task_ids_to_stop, results):
            if isinstance(result, Exception):
                logger.error(
                    f"[任务 {task_id}] 停止任务时发生未捕获的异常: {result}",
                    exc_info=result,
                )
            elif not result:  # 如果 stop_task 返回 False
                logger.warning(
                    f"[任务 {task_id}] 停止任务的请求可能未完全成功 (stop_task 返回 False)。"
                )

        logger.success("所有活动的 TQSDK 任务均已处理停止请求。")


# (可选) 认证错误类，如果需要更具体的错误处理
class TqAuthError(OSError):
    """TQSDK 认证失败时抛出的自定义错误。"""

    pass
