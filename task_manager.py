# backend/task_manager.py
import asyncio
from typing import Any, Callable, Dict, Optional

from config import TQ_ACCOUNT, TQ_PASSWORD
from loguru import logger
from models import TaskStatus, TradingTask
from sqlalchemy.ext.asyncio import AsyncSession
from tqsdk import TqAccount, TqApi, TqAuth, TqKq
from tqsdk.exceptions import TqTimeoutError  # TqAuthError 也是从这里


class TaskManager:
    def __init__(self, sio_server: Any, db_session_factory: Callable[[], AsyncSession]):
        self.sio = sio_server
        self.db_session_factory = db_session_factory
        self.active_asyncio_tasks: Dict[int, asyncio.Task] = {}
        self.active_tqapis: Dict[int, TqApi] = (
            {}
        )  # 主要用于调试或特定情况，一般不从外部直接操作

    async def _run_tq_for_task(self, task_model: TradingTask):
        task_id: int = task_model.id
        symbol: str = task_model.symbol
        room_name: str = f"task_{task_id}"
        error_occurred_in_tq_loop: bool = False
        # tqapi_instance: Optional[TqApi] = None # 不再需要在外部声明，使用 async with

        logger.info(f"[任务 {task_id}] 正在为标的 '{symbol}' 启动 TQSDK 运行器...")

        try:
            # 使用 TqApi 作为异步上下文管理器
            async with TqApi(
                account=TqKq(),  # 使用 TqKq 作为账户类型
                auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD),
                # event_loop=asyncio.get_event_loop(), # 通常 TqApi 会自动获取当前循环
            ) as tqapi:  # tqapi 变量现在是 TqApi 实例

                # self.active_tqapis[task_id] = tqapi # 如果仍然需要在外部引用，可以在这里赋值
                # 但要注意生命周期，当 async with 结束时 tqapi 会被 close

                logger.info(
                    f"[任务 {task_id}] TQSDK 实例已创建并进入上下文，正在连接..."
                )
                quote = tqapi.get_quote(symbol)
                # account_info: Optional[TqAccount] = tqapi.get_account()
                logger.info(f"[任务 {task_id}] 已获取标的 '{symbol}' 的行情对象。")

                if (
                    quote
                    and hasattr(quote, "last_price")
                    and quote.last_price is not None
                ):
                    await self._emit_quote_update(
                        task_id, room_name, quote, tqapi
                    )  # 传递 tqapi

                update_chan = tqapi.register_update_notify(quote)
                logger.info(f"[任务 {task_id}] TQSDK 更新通知已注册，进入监听循环...")

                while task_id in self.active_asyncio_tasks:
                    try:
                        has_update = await asyncio.wait_for(
                            update_chan.recv(), timeout=1.0
                        )
                        if not has_update and task_id not in self.active_asyncio_tasks:
                            logger.info(
                                f"[任务 {task_id}] update_chan.recv() 返回 False 且任务被标记停止。"
                            )
                            break
                        if not has_update:
                            logger.debug(
                                f"[任务 {task_id}] update_chan.recv() 返回 False，但任务仍活动。"
                            )
                            if not tqapi.is_online():
                                logger.warning(
                                    f"[任务 {task_id}] TQSDK 离线，等待重连..."
                                )
                                await asyncio.sleep(1)
                            continue
                    except asyncio.TimeoutError:
                        if task_id not in self.active_asyncio_tasks:
                            logger.info(
                                f"[任务 {task_id}] 等待TQSDK更新超时后，检测到停止信号。"
                            )
                            break
                        continue
                    except TqTimeoutError:
                        logger.warning(f"[任务 {task_id}] TQSDK 内部操作超时。")
                        if not tqapi.is_online():
                            logger.error(f"[任务 {task_id}] TQSDK 已离线。")
                        continue
                    except OSError as tq_err:
                        logger.error(
                            f"[任务 {task_id}] TQSDK 发生错误: {tq_err}", exc_info=True
                        )
                        error_occurred_in_tq_loop = True
                        await self.sio.emit(
                            "task_error",
                            {"task_id": task_id, "error": f"TQSDK 内部错误: {tq_err}"},
                            room=room_name,
                        )
                        if (
                            "行情服务用户验证失败" in str(tq_err)
                            or "再连接失败" in str(tq_err)
                            or "密码错误" in str(tq_err)
                        ):
                            logger.error(
                                f"[任务 {task_id}] 遭遇致命TQSDK错误，将停止此任务。"
                            )
                            break
                        continue
                    except Exception as loop_exception:
                        logger.error(
                            f"[任务 {task_id}] TQSDK 监听循环发生意外错误: {loop_exception}",
                            exc_info=True,
                        )
                        error_occurred_in_tq_loop = True
                        await self.sio.emit(
                            "task_error",
                            {"task_id": task_id, "error": "任务运行中发生未知内部错误"},
                            room=room_name,
                        )
                        break

                    if tqapi.is_updated(quote, "last_price"):
                        await self._emit_quote_update(task_id, room_name, quote, tqapi)

                    await asyncio.sleep(0.005)
            # 当退出 'async with tqapi:' 块时 (无论是正常结束还是因为异常)，
            # tqapi.close() 会被自动调用。这是 TQSDK 推荐的资源管理方式。
            logger.info(
                f"[任务 {task_id}] 已退出 TqApi 的 async with 上下文，close() 应已被自动调用。"
            )

        except ConnectionError as conn_err:  # 捕获 TqApi 初始化或早期连接时的错误
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
        except (
            OSError
        ) as auth_err:  # TqAuthError 是 TqError 的子类，也捕获认证等 TqSDK 核心错误
            logger.error(
                f"[任务 {task_id}] TQSDK 初始化或认证失败 (标的: {symbol}): {auth_err}. 请检查账户和密码。",
                exc_info=True,
            )
            error_occurred_in_tq_loop = True
            await self.sio.emit(
                "task_error",
                {"task_id": task_id, "error": f"TQSDK 初始化/认证失败: {auth_err}"},
                room=room_name,
            )
        except Exception as e:
            logger.error(
                f"[任务 {task_id}] TQSDK 运行器启动或初始化时发生严重错误 (标的: {symbol}): {e}",
                exc_info=True,
            )
            error_occurred_in_tq_loop = True
            await self.sio.emit(
                "task_error",
                {
                    "task_id": task_id,
                    "error": f"任务启动时发生内部错误: {type(e).__name__}",
                },
                room=room_name,
            )

        finally:
            # finally 块仍然保留，用于处理数据库状态更新和从 active_asyncio_tasks 移除
            logger.info(f"[任务 {task_id}] 进入 TQSDK 运行器的最外层 finally 清理块...")

            # tqapi_instance 的 close() 现在由 async with TqApi(...) as tqapi: 自动处理了
            # 所以不再需要在这里手动调用 tqapi_instance.close()

            # if task_id in self.active_tqapis: # 如果之前有赋值，这里可以清理
            #     del self.active_tqapis[task_id]

            is_unexpected_exit_or_still_active = False
            if task_id in self.active_asyncio_tasks:
                logger.warning(
                    f"[任务 {task_id}] TQSDK 运行器结束时任务仍标记为活动。可能为意外退出。"
                )
                # 不在这里 del，让 stop_task 或外部管理
                is_unexpected_exit_or_still_active = True

            logger.info(
                f"[任务 {task_id}] TQSDK 运行器 (标的: {symbol}) 清理块执行完毕。"
            )

            if error_occurred_in_tq_loop or is_unexpected_exit_or_still_active:
                try:
                    async with self.db_session_factory() as db:
                        db_task_model: Optional[TradingTask] = await db.get(
                            TradingTask, task_id
                        )
                        if db_task_model and (
                            db_task_model.status == TaskStatus.RUNNING
                            or db_task_model.status == TaskStatus.PENDING
                        ):  # 如果是 PENDING 时就出错了
                            logger.info(
                                f"[任务 {task_id}] 由于运行器问题，更新数据库状态为 ERROR。"
                            )
                            db_task_model.status = TaskStatus.ERROR
                            await db.commit()
                except Exception as db_err:
                    logger.error(
                        f"[任务 {task_id}] 更新任务状态为 ERROR 时数据库错误: {db_err}",
                        exc_info=True,
                    )

    async def _emit_quote_update(
        self, task_id: int, room_name: str, quote: Any, api: TqApi
    ):  # 添加 api 参数
        """辅助函数：向客户端推送行情更新。"""
        # 使用 api.get_rps() 获取当前的 RPS (Requests Per Second)
        # 使用 api.get_api_latency() 获取当前的 API 平均延迟 (ping)
        # 这些是 tqsdk 提供的有用的状态信息
        rps = api.get_rps() if hasattr(api, "get_rps") else None
        latency = api.get_api_latency() if hasattr(api, "get_api_latency") else None

        await self.sio.emit(
            "quote_update",
            {
                "task_id": task_id,
                "symbol": getattr(quote, "instrument_id", None),
                "last_price": getattr(quote, "last_price", None),
                "ask_price1": getattr(quote, "ask_price1", None),
                "bid_price1": getattr(quote, "bid_price1", None),
                "highest": getattr(quote, "highest", None),
                "lowest": getattr(quote, "lowest", None),
                "open": getattr(quote, "open_price", None),
                "close": getattr(
                    quote, "pre_close", None
                ),  # TQSDK 中昨收通常是 pre_close
                "volume": getattr(quote, "volume", None),
                "datetime": getattr(quote, "datetime", None),
                "timestamp_nano": getattr(quote, "timestamp_nano", None),
                "rps": rps,  # (新增) 每秒请求数
                "latency_ms": latency,  # (新增) API 延迟（毫秒）
            },
            room=room_name,
        )

    async def start_task_by_id(self, task_id: int) -> bool:  # 新方法
        if task_id in self.active_asyncio_tasks:
            logger.warning(f"[任务 {task_id}] 尝试启动一个已在活动列表中的任务。")
            return False

        async with self.db_session_factory() as db:  # 在 TaskManager 内部获取会话
            task_model: Optional[TradingTask] = await db.get(TradingTask, task_id)
            if not task_model:
                logger.error(f"[任务 {task_id}] 尝试启动时未在数据库中找到任务。")
                return False  # 指示任务不存在或获取失败
            if task_model.status == TaskStatus.RUNNING:  # 再次检查，以防万一
                logger.warning(
                    f"[任务 {task_id}] 任务 '{task_model.name}' 在数据库中已是 RUNNING 状态。"
                )
                # 根据业务逻辑，这里可以直接返回 True 表示任务“已在运行”，或者认为是某种不一致
                # 如果直接返回 True，FastAPI 端点可能需要调整其对 success 结果的理解
                # return True
                # 或者，如果认为这是一种需要上层处理的情况：
                # return False # 让 FastAPI 端点知道启动没有“新”发生

            logger.info(
                f"[任务 {task_id}] 准备启动任务 '{task_model.name}' (标的: {task_model.symbol})..."
            )
            current_loop = asyncio.get_event_loop()
            tq_coroutine = self._run_tq_for_task(
                task_model
            )  # _run_tq_for_task 接收 ORM 对象
            tq_runner_task = current_loop.create_task(tq_coroutine)
            self.active_asyncio_tasks[task_id] = tq_runner_task
            logger.info(
                f"[任务 {task_id}] 已为标的 '{task_model.symbol}' 创建并调度 TQSDK 运行器任务。"
            )

            # 更新数据库状态
            task_model.status = TaskStatus.RUNNING  # 修改的是当前会话中的 task_model
            try:
                await db.commit()
                logger.info(f"[任务 {task_id}] 数据库中任务状态已更新为 RUNNING。")
            except Exception as e_commit:
                logger.error(
                    f"[任务 {task_id}] 更新数据库状态为 RUNNING 时出错: {e_commit}",
                    exc_info=True,
                )
                await db.rollback()
                # 清理已调度的 TQSDK 任务
                tq_runner_task.cancel()
                if task_id in self.active_asyncio_tasks:
                    del self.active_asyncio_tasks[task_id]
                # 注意：这里返回 False，FastAPI 端点需要根据这个 False 来决定如何响应
                return False
        return True  # 成功

    async def stop_task(self, task_id: int) -> bool:
        logger.info(f"[任务 {task_id}] 正在请求停止任务...")
        tq_runner_task_to_stop: Optional[asyncio.Task] = self.active_asyncio_tasks.pop(
            task_id, None
        )
        # 从 active_tqapis 中也移除，避免在 _run_tq_for_task 的 finally 中再次尝试关闭已经不存在的 TqApi
        # tqapi_to_cleanup: Optional[TqApi] = self.active_tqapis.pop(task_id, None)

        if tq_runner_task_to_stop:
            logger.info(
                f"[任务 {task_id}] 任务在活动列表中，已标记移除，正在等待其协程结束..."
            )
            if (
                not tq_runner_task_to_stop.done()
            ):  # 如果任务还没有完成（可能仍在运行或等待）
                # tq_runner_task_to_stop.cancel() # 尝试取消任务，这会让内部的 await 抛出 CancelledError
                # logger.info(f"[任务 {task_id}] 已向 TQSDK 运行器协程发送取消信号。")
                # 发送取消信号后，_run_tq_for_task 中的 `task_id in self.active_asyncio_tasks` 会变为 false
                # 并且内部的 await 可能会因为 CancelledError 而中断。
                # 我们仍然需要等待它实际结束。
                pass  # _run_tq_for_task 的主循环会因为 task_id 不在 active_asyncio_tasks 中而退出

            try:
                # 等待任务协程实际完成。_run_tq_for_task 的 finally 块应该在这里执行。
                await asyncio.wait_for(tq_runner_task_to_stop, timeout=7.0)
                logger.info(f"[任务 {task_id}] TQSDK 运行器协程已确认结束。")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[任务 {task_id}] 等待 TQSDK 运行器协程结束超时。可能未能优雅关闭。尝试强制取消。"
                )
                if not tq_runner_task_to_stop.done():  # 再次检查，如果仍未完成则取消
                    tq_runner_task_to_stop.cancel()
                    try:
                        await tq_runner_task_to_stop  # 等待取消完成
                    except asyncio.CancelledError:
                        logger.info(f"[任务 {task_id}] TQSDK 运行器协程已被强制取消。")
                    except Exception as e_after_force_cancel:
                        logger.error(
                            f"[任务 {task_id}] 强制取消后等待任务结束时发生错误: {e_after_force_cancel}",
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                logger.info(f"[任务 {task_id}] TQSDK 运行器协程在等待期间被取消。")
            except Exception as e_wait_stop:
                logger.error(
                    f"[任务 {task_id}] 等待 TQSDK 运行器协程结束时发生错误: {e_wait_stop}",
                    exc_info=True,
                )

            # # 不再从这里直接关闭 tqapi_to_cleanup，让 _run_tq_for_task 的 finally 块处理
            # if tqapi_to_cleanup:
            #     try:
            #         logger.info(f"[任务 {task_id}] (stop_task) 尝试关闭已移除的 TqApi 实例...")
            #         # 这里调用 close 仍然可能遇到 "cannot call close" 的问题，
            #         # 因为 _run_tq_for_task 可能还没完全退出其内部循环或清理完毕。
            #         # tqapi_to_cleanup.close()
            #         # 更好的方式是依赖 _run_tq_for_task 的 finally 块。
            #         logger.info(f"[任务 {task_id}] (stop_task) TqApi 实例应该由其自己的协程关闭。")
            #     except Exception as e:
            #         logger.error(f"[任务 {task_id}] (stop_task) 尝试关闭 TqApi 实例时出错: {e}", exc_info=True)

        else:  # 任务不在活动列表中
            logger.warning(
                f"[任务 {task_id}] 尝试停止一个当前不在活动列表中的任务。将仅更新数据库状态（如果需要）。"
            )

        # 更新数据库状态
        try:
            async with self.db_session_factory() as db:
                db_task_to_update: Optional[TradingTask] = await db.get(
                    TradingTask, task_id
                )
                if db_task_to_update:
                    if db_task_to_update.status in [
                        TaskStatus.RUNNING,
                        TaskStatus.ERROR,
                    ]:
                        db_task_to_update.status = TaskStatus.STOPPED
                        await db.commit()
                        logger.info(
                            f"[任务 {task_id}] 数据库中任务状态已更新为 STOPPED。"
                        )
                    else:
                        logger.info(
                            f"[任务 {task_id}] 数据库中任务状态为 {db_task_to_update.status.value}，无需更新为 STOPPED。"
                        )
                else:
                    logger.error(
                        f"[任务 {task_id}] 错误：停止任务时数据库未找到该任务。"
                    )
                    return False  # 表示任务不存在
        except Exception as e:
            logger.error(
                f"[任务 {task_id}] 更新数据库状态为 STOPPED 时出错: {e}", exc_info=True
            )
            return False

        return True

    async def stop_all_tasks(self):
        # ... (与上一版本类似) ...
        logger.info("正在请求停止所有当前活动的 TQSDK 任务...")
        task_ids_to_stop = list(self.active_asyncio_tasks.keys())
        if not task_ids_to_stop:
            logger.info("当前没有活动的 TQSDK 任务需要停止。")
            return
        results = await asyncio.gather(
            *(self.stop_task(task_id) for task_id in task_ids_to_stop),
            return_exceptions=True,
        )
        for task_id, result in zip(task_ids_to_stop, results):
            if isinstance(result, Exception):
                logger.error(
                    f"[任务 {task_id}] 停止任务时发生未捕获的异常: {result}",
                    exc_info=result,
                )
            elif not result:
                logger.warning(f"[任务 {task_id}] 停止任务的请求可能未完全成功。")
        logger.success("所有活动的 TQSDK 任务已处理停止请求。")
