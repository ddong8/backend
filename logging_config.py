# backend/logging_config.py
import sys  # 用于访问标准输入输出流
import logging  # Python 标准 logging 模块，用于拦截其日志
import os  # 用于路径操作，例如创建日志目录
from loguru import logger  # 导入 Loguru 的 logger 对象
from config import LOG_LEVEL  # 从项目配置中导入 LOG_LEVEL

# (可选) 为某些特别冗余的第三方库设置更高的日志级别，以减少干扰
# logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING) # SQLAlchemy 引擎日志
# logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # Uvicorn 访问日志


def setup_loguru():
    """
    配置 Loguru 日志记录器。
    包括控制台输出、可选的文件输出，以及对标准 logging 库日志的拦截。
    """
    logger.remove()  # 移除所有预设的 Loguru handlers，以便从头开始配置

    # ---- 控制台 Handler (输出到 stderr) ----
    logger.add(
        sys.stderr,  # 日志输出目标：标准错误流
        level=LOG_LEVEL,  # 日志级别：从配置文件读取 (例如 "INFO", "DEBUG")
        format=(  # 日志格式字符串，使用 Loguru 的标记语言
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS zz}</green> | "  # 时间 (带时区)
            "<level>{level: <8}</level> | "  # 日志级别 (左对齐，占8位)
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "  # 模块名:函数名:行号
            "<level>{message}</level>"  # 日志消息本身
        ),
        colorize=True,  # 启用彩色输出，增强可读性
        backtrace=True,  # 异常时显示更美观、更详细的堆栈跟踪 (Loguru 特色)
        diagnose=True,  # 异常时显示涉及变量的诊断信息 (Loguru 特色)
        enqueue=True,  # (可选) 将日志消息放入队列异步处理，避免I/O阻塞主线程，尤其在文件日志中更常用
    )

    # ---- 文件 Handler (可选，输出到日志文件) ----
    log_file_path = (
        "logs/trading_platform_{time:YYYY-MM-DD}.log"  # 日志文件路径和命名模式
    )
    try:
        # 确保日志目录存在
        os.makedirs("logs", exist_ok=True)
        logger.add(
            log_file_path,
            rotation="100 MB",  # 文件轮转：当文件大小达到 100MB 时创建新文件
            retention="14 days",  # 文件保留：保留最近 14 天的日志文件
            compression="zip",  # 文件压缩：使用 zip 格式压缩旧的日志文件
            level="DEBUG",  # 文件日志级别：通常记录比控制台更详细的信息 (例如 DEBUG)
            format=(  # 文件日志的格式可以与控制台不同，例如包含进程/线程信息
                "{time:YYYY-MM-DD HH:mm:ss.SSS zz} | {level: <8} | "
                "P:{process} | T:{thread} | {name}:{function}:{line} - {message}"
            ),
            enqueue=True,  # 异步写入文件，提高性能
            backtrace=True,
            diagnose=True,
            encoding="utf-8",  # 指定文件编码
        )
        logger.info(f"文件日志已配置，将输出到 '{os.path.abspath('logs')}' 目录。")
    except PermissionError:  # 捕获可能的权限错误
        logger.error(
            f"配置文��日志失败：没有权限写入 '{os.path.abspath('logs')}' 目录。"
            "请检查目录权限或在 Docker 环境中正确配置卷映射和权限。"
        )
    except Exception as e:  # 捕获其他可能的错误
        logger.error(f"配置文��日志时发生未知错误: {e}")

    # ---- 拦截标准 logging 库的日志 (可选但强烈推荐) ----
    # 这使得使用标准 logging 模块的第三方库 (如 uvicorn, sqlalchemy, tqsdk 内部的一些日志)
    # 也能被 Loguru 捕获、格式化并输出。
    class InterceptHandler(logging.Handler):
        """
        自定义 logging.Handler，将标准库的日志记录转发给 Loguru。
        """

        def emit(self, record: logging.LogRecord):
            # 尝试从标准库的日志级别名称获取 Loguru 对应的级别
            try:
                level = logger.level(record.levelname).name
            except ValueError:  # 如果 Loguru 没有完全匹配的级别名称
                level = record.levelno  # 使用原始的数字级别

            # 确定正确的调用栈深度，以便 Loguru 能正确显示日志来源的文件和行号
            frame, depth = logging.currentframe(), 0
            # 循环向上查找调用栈，直到找到不是 logging 模块自身代码的帧
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back  # 向上回溯一帧
                depth += 1

            # 使用 logger.opt() 来传递额外的上下文 (如异常信息和正确的栈深度)
            # depth + 1 是因为我们是从 InterceptHandler.emit 内部调用的 Loguru
            logger.opt(depth=depth + 1, exception=record.exc_info).log(
                level, record.getMessage()
            )

    # 获取根 logger (所有标准库日志的源头)
    std_logging_root = logging.getLogger()
    # 移除所有现有的 handlers，避免重复日志或冲突 (如果其他库已经配置了 handler)
    # std_logging_root.handlers = [] # 谨慎使用，确保了解其影响

    # 添加我们的拦截器到标准 logging 的根 logger
    # 检查是否已添加，避免重复 (尽管 Loguru 的 add 通常是幂等的)
    if not any(isinstance(h, InterceptHandler) for h in std_logging_root.handlers):
        intercept_handler = InterceptHandler()
        std_logging_root.addHandler(intercept_handler)
        std_logging_root.setLevel(
            logging.NOTSET
        )  # 将根 logger 级别设为最低，以便捕获所有日志，由 Loguru 的级别设置进行实际过滤

    logger.success(
        f"Loguru 日志系统已成功配置。控制台日志级别: {LOG_LEVEL}, "
        f"文件日志级别: DEBUG (如果文件日志启用)。标准库日志将被拦截。"
    )
