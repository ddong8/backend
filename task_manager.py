# backend/task_manager.py
import asyncio
from loguru import logger
from tqsdk import TqApi, TqAuth, TqAccount, TqKq
from tqsdk.exceptions import TqTimeoutError # TQSDK 的超时异常
from typing import Dict, Callable, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from models import TradingTask, TaskStatus
from config import TQ_ACCOUNT, TQ_PASSWORD

class TaskManager:
    def __init__(self, sio_server: Any, db_session_factory: Callable[[], AsyncSession]):
        self.sio = sio_server
        self.db_session_factory = db_session_factory
        self.active_asyncio_tasks: Dict[int, asyncio.Task] = {}
        self.active_tqapis: Dict[int, TqApi] = {} 

    async def _run_tq_for_task(self, task_model: TradingTask):
        task_id: int = task_model.id
        symbol: str = task_model.symbol
        room_name: str = f"task_{task_id}"
        tqapi_instance: Optional[TqApi] = None
        error_occurred_flag: bool = False 

        logger.info(f"[任务 {task_id}] 准备为标的 '{symbol}' 初始化 TQSDK...")
        
        try:
            tqapi_instance = TqApi(
                account=None, 
                auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD),
            )
            logger.info(f"[任务 {task_id}] TQSDK 实例已创建。")
            self.active_tqapis[task_id] = tqapi_instance
            
            try:
                logger.info(f"[任务 {task_id}] 正在获取标的 '{symbol}' 的行情对象...")
                quote = tqapi_instance.get_quote(symbol)
                logger.success(f"[任务 {task_id}] 已成功获取标的 '{symbol}' 的行情对象。")
                
                logger.info(f"[任务 {task_id}] 正在获取账户信息对象...")
                account_info: Optional[TqAccount] = tqapi_instance.get_account()
                logger.success(f"[任务 {task_id}] 已成功获取账户信息对象 (可能为 None)。")

            except Exception as get_obj_err:
                logger.error(f"[任务 {task_id}] 获取 TQSDK 对象时发生错误: {get_obj_err}", exc_info=True)
                # ... (错误上报 emit) ...
                raise 

            # 初始数据推送
            if quote and hasattr(quote, 'last_price') and quote.last_price is not None:
                await self._emit_quote_update(task_id, room_name, quote, tqapi_instance)
            if account_info:
                 await self._emit_account_update(task_id, room_name, account_info)

            logger.info(f"[任务 {task_id}] 进入 TQSDK 更新监听循环 (使用 register_update_notify 和 is_changing)...")
            
            # register_update_notify() 不传入参数会监控所有已 get 的对象，
            # 或者传入你希望其更新能“唤醒” update_chan 的主要对象。
            # 对于账户信息这种可能独立于行情更新的对象，我们会在每次循环迭代后都用 is_changing 检查。
            async with tqapi_instance.register_update_notify() as update_chan: 
                while task_id in self.active_asyncio_tasks:
                    try:
                        # 等待 TQSDK 通知有数据更新，或者超时
                        # async for _ in update_chan: 这种写法是正确的，
                        # 它等同于在循环中调用 await update_chan.recv() 并检查其返回值。
                        # 如果想加入超时，需要改写成 wait_for(update_chan.recv(), timeout)
                        
                        # 改为使用 wait_for 来明确处理超时
                        try:
                            await asyncio.wait_for(update_chan.recv(), timeout=1.0)
                            # 当 recv() 返回时（无论True/False），表示 TQSDK 完成了一轮数据处理
                            logger.trace(f"[任务 {task_id}] update_chan 收到信号或超时后唤醒。")
                        except asyncio.TimeoutError:
                            if task_id not in self.active_asyncio_tasks:
                                logger.info(f"[任务 {task_id}] 等待TQSDK更新超时后，检测到停止信号。")
                                break
                            # 超时是正常的，表示在1秒内 channel 没有收到“主要”更新信号
                            # 但我们仍然需要检查对象是否发生了变化
                            # logger.trace(f"[任务 {task_id}] 等待TQSDK更新超时，继续检查对象变化...")
                            pass # 继续执行下面的 is_changing 检查
                        
                        if task_id not in self.active_asyncio_tasks: # 再次检查停止信号
                            logger.info(f"[任务 {task_id}] 检测到停止信号，退出监听循环。")
                            break

                        # ***** 使用 is_changing() 判断特定对象是否更新 *****
                        if tqapi_instance.is_changing(quote): # 检查 quote 对象是否有任何字段发生变化
                        # if tqapi_instance.is_changing(quote, "last_price"): # 或者检查特定字段
                            logger.trace(f"[任务 {task_id}] quote 对象已更新。")
                            await self._emit_quote_update(task_id, room_name, quote, tqapi_instance)
                        
                        if account_info and tqapi_instance.is_changing(account_info): # 检查账户信息是否有变化
                            logger.trace(f"[任务 {task_id}] account_info 对象已更新。")
                            await self._emit_account_update(task_id, room_name, account_info)
                        
                        # （可选）检查其他你关心的对象，例如订单、持仓等
                        # orders = tqapi_instance.get_orders() # 假设你获取了订单列表
                        # for order_id, order in orders.items():
                        #     if tqapi_instance.is_changing(order):
                        #         logger.info(f"[任务 {task_id}] 订单 {order_id} 已更新: {order}")
                        #         # ... 推送订单更新 ...

                    except TqTimeoutError: # TQSDK 内部的超时
                        logger.warning(f"[任务 {task_id}] TQSDK 内部操作超时。")
                        if tqapi_instance and not tqapi_instance.is_online():
                            logger.error(f"[任务 {task_id}] TQSDK 在操作超时后检测为离线。")
                        # 即使 TqTimeoutError，也可能需要检查对象状态
                        if tqapi_instance and quote and tqapi_instance.is_changing(quote):
                            await self._emit_quote_update(task_id, room_name, quote, tqapi_instance)
                        if tqapi_instance and account_info and tqapi_instance.is_changing(account_info):
                            await self._emit_account_update(task_id, room_name, account_info)
                        continue
                    
                    except Exception as loop_err: 
                        # ... (错误处理逻辑与上一版本相同) ...
                        err_msg = str(loop_err)
                        logger.error(f"[任务 {task_id}] TQSDK 监听循环中发生错误: {err_msg}", exc_info=True)
                        error_occurred_flag = True
                        # ... (emit task_error) ...
                        if "行情服务用户验证失败" in err_msg or "登录失败" in err_msg or "密码错误" in err_msg or "auth fail" in err_msg.lower():
                            await self.sio.emit("task_error", {"task_id": task_id, "error": f"TQSDK认证/连接问题: {err_msg[:150]}"}, room=room_name)
                        elif isinstance(loop_err, ConnectionError):
                             await self.sio.emit("task_error", {"task_id": task_id, "error": f"网络连接错误: {err_msg[:150]}"}, room=room_name)
                        else: 
                            await self.sio.emit("task_error", {"task_id": task_id, "error": f"TQSDK运行时错误: {err_msg[:200]}"}, room=room_name)
                        
                        if "行情服务用户验证失败" in err_msg or "再连接失败" in err_msg :
                            logger.error(f"[任务 {task_id}] 遭遇严重TQSDK错误，将停止此任务的监听循环。")
                            break 
                        continue # 对于其他可恢复的错误，尝试继续
                    
                    await asyncio.sleep(0.005) # 避免CPU空转，如果循环非常快且无更新
            logger.info(f"[任务 {task_id}] 已退出 TQSDK 更新监听循环。")

        except asyncio.CancelledError:
            logger.info(f"[任务 {task_id}] TQSDK 运行器任务已被取消。")
        except Exception as e: 
            # ... (启动或初始化时的错误处理逻辑与上一版本相同) ...
            err_msg = str(e)
            logger.error(f"[任务 {task_id}] TQSDK 运行器启动或初始化时发生错误 (标的: {symbol}): {err_msg}", exc_info=True)
            error_occurred_flag = True
            if "行情服务用户验证失败" in err_msg or "登录失败" in err_msg or "密码错误" in err_msg or "auth fail" in err_msg.lower():
                await self.sio.emit("task_error", {"task_id": task_id, "error": f"TQSDK认证/连接问题: {err_msg[:150]}"}, room=room_name)
            elif isinstance(e, ConnectionError):
                 await self.sio.emit("task_error", {"task_id": task_id, "error": f"网络连接错误: {err_msg[:150]}"}, room=room_name)
            else:
                await self.sio.emit("task_error", {"task_id": task_id, "error": f"TQSDK启动时错误: {err_msg[:200]}"}, room=room_name)
        
        finally:
            # ... (finally 块中的清理逻辑与上一版本相同) ...
            logger.info(f"[任务 {task_id}] 进入 TQSDK 运行器的 finally 清理块...")
            if tqapi_instance:
                try:
                    logger.info(f"[任务 {task_id}] 准备关闭 TqApi 实例...")
                    tqapi_instance.close()
                    logger.success(f"[任务 {task_id}] TqApi 实例已成功关闭。")
                except RuntimeError as rt_err:
                    err_str = str(rt_err).lower()
                    if "event loop is closed" in err_str or "cannot call " in err_str or "is_closed" in err_str or "already running" in err_str:
                        logger.error(f"[任务 {task_id}] 关闭 TqApi 时发生 RuntimeError: {rt_err}。", exc_info=False)
                    else:
                        logger.error(f"[任务 {task_id}] 关闭 TqApi 实例时发生未预期的 RuntimeError: {rt_err}", exc_info=True)
                except Exception as close_err:
                    logger.error(f"[任务 {task_id}] 关闭 TqApi 实例时发生其他类型的错误: {close_err}", exc_info=True)
            
            if task_id in self.active_tqapis: 
                del self.active_tqapis[task_id]
                logger.info(f"[任务 {task_id}] 已从 active_tqapis 字典中移除 (finally块)。")

            was_still_in_active_list = self.active_asyncio_tasks.pop(task_id, None) is not None
            logger.info(
                f"[任务 {task_id}] TQSDK 运行器 (标的: {symbol}) 清理完毕。 "
                f"任务是否在活动列表中被发现并移除: {was_still_in_active_list}"
            )

            if error_occurred_flag:
                try:
                    async with self.db_session_factory() as db:
                        db_task_model: Optional[TradingTask] = await db.get(TradingTask, task_id)
                        if db_task_model and db_task_model.status == TaskStatus.RUNNING:
                            logger.info(f"[任务 {task_id}] 由于运行器内部发生错误，正在更新数据库中任务状态为 ERROR。")
                            db_task_model.status = TaskStatus.ERROR
                            await db.commit()
                except Exception as db_err:
                    logger.error(f"[任务 {task_id}] 在尝试将任务状态更新为 ERROR 时发生数据库错误: {db_err}", exc_info=True)

    # _emit_quote_update, _emit_account_update, start_task, stop_task, stop_all_tasks 方法保持不变
    # ... (确保这些方法的代码与上一版本一致) ...
    async def _emit_quote_update(self, task_id: int, room_name: str, quote: Any, api: TqApi):
        rps = api.get_rps() if hasattr(api, 'get_rps') else None
        latency_ms = api.get_api_latency() if hasattr(api, 'get_api_latency') else None
        quote_data_to_send = {
            "task_id": task_id, "symbol": getattr(quote, 'instrument_id', None),
            "last_price": getattr(quote, 'last_price', None), "ask_price1": getattr(quote, 'ask_price1', None),
            "bid_price1": getattr(quote, 'bid_price1', None), "highest": getattr(quote, 'highest', None),
            "lowest": getattr(quote, 'lowest', None), "open": getattr(quote, 'open_price', None),
            "close": getattr(quote, 'pre_close', None), "volume": getattr(quote, 'volume', None),
            "datetime": getattr(quote, 'datetime', None), "timestamp_nano": getattr(quote, 'timestamp_nano', None),
            "rps": rps, "latency_ms": latency_ms
        }
        await self.sio.emit("quote_update", quote_data_to_send, room=room_name)

    async def _emit_account_update(self, task_id: int, room_name: str, account: TqAccount):
        account_data_to_send = {
            "task_id": task_id, "account_id": getattr(account, 'account_id', None),
            "broker_id": getattr(account, 'broker_id', None), "available": getattr(account, 'available', None),
            "balance": getattr(account, 'balance', None), "static_balance": getattr(account, 'static_balance', None),
            "margin": getattr(account, 'margin', None), "commission": getattr(account, 'commission', None),
            "position_profit": getattr(account, 'position_profit', None),
            "close_profit": getattr(account, 'close_profit', None), "risk_ratio": getattr(account, 'risk_ratio', None),
        }
        await self.sio.emit("account_update", account_data_to_send, room=room_name)


    async def start_task(self, task_model: TradingTask) -> bool:
        task_id = task_model.id
        if task_id in self.active_asyncio_tasks:
            logger.warning(f"[任务 {task_id}] 尝试启动一个已在活动列表中的任务 (名称: '{task_model.name}')。")
            return False

        logger.info(f"[任务 {task_id}] 准备启动任务 '{task_model.name}' (标的: {task_model.symbol})...")
        current_loop = asyncio.get_event_loop()
        tq_coroutine = self._run_tq_for_task(task_model)
        
        def _task_done_callback(future: asyncio.Future, task_id_cb: int = task_id):
            try:
                future.result() 
                logger.info(f"[任务 {task_id_cb}] TQSDK 运行器任务已正常完成 (回调中确认)。")
            except asyncio.CancelledError:
                logger.info(f"[任务 {task_id_cb}] TQSDK 运行器任务已被取消 (回调中捕获)。")
            except Exception as e:
                logger.error(
                    f"[任务 {task_id_cb}] TQSDK 运行器任务发生未捕获的严重异常 (回调中捕获): {e}", 
                    exc_info=True
                )
            finally:
                if task_id_cb in self.active_asyncio_tasks: 
                    logger.warning(f"[任务 {task_id_cb}] 在任务完成回调的 finally 中发现任务仍活动，现移除。")
                    del self.active_asyncio_tasks[task_id_cb]
                if task_id_cb in self.active_tqapis: 
                    logger.warning(f"[任务 {task_id_cb}] 在任务完成回调的 finally 中发现 TqApi 实例仍活动，现移除。")
                    del self.active_tqapis[task_id_cb]
        
        tq_runner_task = current_loop.create_task(tq_coroutine, name=f"TQTaskRunner-{task_id}")
        tq_runner_task.add_done_callback(_task_done_callback)
        
        self.active_asyncio_tasks[task_id] = tq_runner_task
        logger.info(f"[任务 {task_id}] 已为标的 '{task_model.symbol}' 创建并调度 TQSDK 运行器任务。")

        try:
            async with self.db_session_factory() as db:
                db_task_to_update: Optional[TradingTask] = await db.get(TradingTask, task_id)
                if db_task_to_update:
                    db_task_to_update.status = TaskStatus.RUNNING
                    await db.commit()
                    logger.info(f"[任务 {task_id}] 数据库中任务状态已更新为 RUNNING。")
                else:
                    logger.error(f"[任务 {task_id}] 严重错误：启动时数据库未找到任务。取消 TQSDK 任务。")
                    if not tq_runner_task.done(): tq_runner_task.cancel()
                    return False
        except Exception as e:
            logger.error(f"[任务 {task_id}] 更新数据库状态为 RUNNING 时出错: {e}", exc_info=True)
            if not tq_runner_task.done(): tq_runner_task.cancel()
            return False
        return True

    async def stop_task(self, task_id: int) -> bool:
        logger.info(f"[任务 {task_id}] 正在请求停止任务...")
        tq_runner_task_to_stop: Optional[asyncio.Task] = self.active_asyncio_tasks.get(task_id)

        if tq_runner_task_to_stop:
            logger.info(f"[任务 {task_id}] 任务在活动列表中。将从列表移除并通过取消信号尝试停止其协程...")
            if task_id in self.active_asyncio_tasks: 
                del self.active_asyncio_tasks[task_id] 
            
            if not tq_runner_task_to_stop.done():
                logger.info(f"[任务 {task_id}] 向 TQSDK 协程 (Task: {tq_runner_task_to_stop.get_name()}) 发送取消信号...")
                tq_runner_task_to_stop.cancel()
                try:
                    await asyncio.wait_for(tq_runner_task_to_stop, timeout=10.0) 
                    logger.info(f"[任务 {task_id}] TQSDK 运行器协程已确认结束。")
                except asyncio.TimeoutError:
                    logger.warning(f"[任务 {task_id}] 等待TQSDK协程响应取消并结束时超时。")
                except asyncio.CancelledError: 
                     logger.info(f"[任务 {task_id}] 等待TQSDK协程结束的操作被取消。")
                except Exception as e_wait_stop:
                    logger.error(f"[任务 {task_id}] 等待TQSDK协程结束时发生错误: {e_wait_stop}", exc_info=True)
            else:
                logger.info(f"[任务 {task_id}] TQSDK 协程在请求停止时已完成。")
        else:
            logger.warning(f"[任务 {task_id}] 尝试停止一个当前不在活动列表中的任务。")

        if task_id in self.active_tqapis: # 确保在各种情况下都能清理 active_tqapis
            logger.info(f"[任务 {task_id}] (stop_task) 尝试从 active_tqapis 中移除。")
            del self.active_tqapis[task_id]


        try:
            async with self.db_session_factory() as db:
                db_task_to_update: Optional[TradingTask] = await db.get(TradingTask, task_id)
                if db_task_to_update:
                    if db_task_to_update.status == TaskStatus.RUNNING:
                        db_task_to_update.status = TaskStatus.STOPPED
                        await db.commit()
                        logger.info(f"[任务 {task_id}] 数据库任务状态已更新为 STOPPED。")
                    elif db_task_to_update.status == TaskStatus.ERROR and not tq_runner_task_to_stop:
                        db_task_to_update.status = TaskStatus.STOPPED 
                        await db.commit()
                        logger.info(f"[任务 {task_id}] 任务原为 ERROR 且不在活动列表，数据库状态更新为 STOPPED。")
                    else:
                        logger.info(f"[任务 {task_id}] 数据库任务状态为 '{db_task_to_update.status.value}'，无需更新。")
                else:
                    logger.error(f"[任务 {task_id}] 错误：停止任务时数据库未找到记录。")
                    return False 
        except Exception as e_db_update:
            logger.error(f"[任务 {task_id}] 更新数据库状态为 STOPPED 时出错: {e_db_update}", exc_info=True)
            return False
        
        return True
    
    async def stop_all_tasks(self):
        logger.info("正在请求停止所有当前活动的 TQSDK 任务...")
        task_ids_to_stop = list(self.active_asyncio_tasks.keys())
        if not task_ids_to_stop:
            logger.info("当前没有活动的 TQSDK 任务需要停止。")
            return
        
        stop_results = await asyncio.gather(
            *(self.stop_task(task_id) for task_id in task_ids_to_stop),
            return_exceptions=True 
        )
        for task_id, result in zip(task_ids_to_stop, stop_results):
            if isinstance(result, Exception):
                logger.error(f"[任务 {task_id}] 停止任务时发生未捕获的异常 (gather 中收集): {result}", exc_info=result)
            elif not result:
                 logger.warning(f"[任务 {task_id}] 停止任务的请求可能未完全成功或任务不存在 (stop_task 返回 False)。")
        
        await asyncio.sleep(1.5) 
        
        if self.active_asyncio_tasks or self.active_tqapis: 
            logger.warning(
                f"执行停止所有任务操作后，仍有 {len(self.active_asyncio_tasks)} 个任务在活动协程列表 "
                f"和 {len(self.active_tqapis)} 个 TqApi 实例在活动字典中。 "
                f"协程列表: {list(self.active_asyncio_tasks.keys())}, TqApi列表: {list(self.active_tqapis.keys())}。"
            )
        else:
            logger.success("所有活动的 TQSDK 任务已处理停止请求，活动协程和TqApi列表已清空。")