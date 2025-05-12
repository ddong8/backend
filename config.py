# backend/config.py
import os  # 用于访问操作系统功能，如环境变量
from dotenv import load_dotenv  # 从 python-dotenv 库导入函数，用于加载 .env 文件
from loguru import logger  # 导入 Loguru 日志库的 logger 对象

# --- 加载环境变量 ---
# 确定 .env 文件的可能路径：
# 1. 与 config.py 在同一目录下的 .env 文件
# 2. 项目根目录 (上一级目录) 的 .env 文件 (用于 docker-compose 可能读取的根 .env)
current_script_dir = os.path.dirname(os.path.abspath(__file__))
backend_env_path = os.path.join(current_script_dir, ".env")
project_root_env_path = os.path.join(os.path.dirname(current_script_dir), ".env")

# 优先加载 backend/.env
if os.path.exists(backend_env_path):
    load_dotenv(
        dotenv_path=backend_env_path, override=True
    )  # override=True 允许此文件覆盖系统环境变量
    logger.info(f"已从 '{backend_env_path}' 加载环境变量。")
elif os.path.exists(
    project_root_env_path
):  # 如果 backend/.env 不存在，尝试加载项目根目录的 .env
    load_dotenv(dotenv_path=project_root_env_path, override=True)
    logger.info(f"已从项目根目录 '{project_root_env_path}' 加载环境变量。")
else:
    logger.warning(
        f"未在 '{backend_env_path}' 或 '{project_root_env_path}' 找到 .env 文件。"
        "应用将依赖于系统级环境变量或代码中的默认回退值。"
    )

# --- 数据库配置 ---
# 从环境变量获取数据库连接字符串，如果未设置，则使用一个明确的回退值并警告
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://default_user:default_pass@db_host_fallback:5432/default_db_name",
)
if "db_host_fallback" in DATABASE_URL:  # 检查是否使用了回退值
    logger.warning(
        "DATABASE_URL 未在环境变量中正确配置，使用了默认的回退值。"
        "请确保在 .env 文件或系统环境变量中设置 DATABASE_URL。"
    )

# --- TQSDK 账户凭证 ---
# 从环境变量获取 TQSDK 账户和密码
TQ_ACCOUNT: str | None = os.getenv("TQ_ACCOUNT")
TQ_PASSWORD: str | None = os.getenv("TQ_PASSWORD")

# 对 TQSDK 凭证进行启动时检查，如果未设置则发出严重警告
if not TQ_ACCOUNT:
    logger.critical(
        "严重警告: TQ_ACCOUNT 未在环境变量中设置！交易相关功能可能无法正常工作。"
    )
if not TQ_PASSWORD:
    logger.critical(
        "严重警告: TQ_PASSWORD 未在环境变量中设置！交易相关功能可能无法正常工作。"
    )

# --- 日志级别配置 ---
# 从环境变量获取日志级别，默认为 "INFO"
# 将获取到的字符串转换为大写，并校验是否为 Loguru 支持的有效级别
log_level_str_from_env = os.getenv("LOG_LEVEL", "INFO").upper()
VALID_LOGURU_LEVELS = [
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
]
LOG_LEVEL: str = (
    log_level_str_from_env if log_level_str_from_env in VALID_LOGURU_LEVELS else "INFO"
)

if log_level_str_from_env not in VALID_LOGURU_LEVELS:
    logger.warning(
        f"环境变量中指定的 LOG_LEVEL '{log_level_str_from_env}' 无效。"
        f"将使用默认日志级别 '{LOG_LEVEL}'。"
    )

# --- Gunicorn 工作进程数 (可选, 用于 docker-compose command) ---
# 从环境变量读取 Gunicorn worker 数量，提供一个基于 CPU 核心数的合理默认值
# (通常在 docker-compose.yml 或启动脚本中直接使用此变量)
# num_cores = os.cpu_count() or 1 # 获取 CPU 核心数，默认为1
# GUNICORN_WORKERS: int = int(os.getenv("GUNICORN_WORKERS", str(2 * num_cores + 1)))


# --- 调试输出已加载的配置 (小心不要打印敏感信息) ---
logger.debug(
    f"配置已加载: "
    f"DATABASE_URL (主机: {DATABASE_URL.split('@')[-1].split('/')[0] if '@' in DATABASE_URL else 'N/A'}), "  # 只显示主机部分
    f"TQ_ACCOUNT (已设置: {bool(TQ_ACCOUNT)}), "
    f"LOG_LEVEL='{LOG_LEVEL}'"
    # f"GUNICORN_WORKERS={GUNICORN_WORKERS}"
)
