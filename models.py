# backend/models.py
import enum  # Python 标准库的 enum 模块
from sqlalchemy import (  # 从 SQLAlchemy 导入核心组件
    Column,
    Integer,
    String,
    JSON,
    DateTime,
    Enum as SAEnum,
    func,
    MetaData,
    ForeignKey,  # ForeignKey 用于外键约束 (如果需要)
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
)  # 异步数据库操作组件
from sqlalchemy.orm import sessionmaker, declarative_base, relationship  # ORM 相关组件
from config import DATABASE_URL  # 从项目配置中导入数据库连接字符串
from loguru import logger  # 导入 Loguru 日志记录器

# --- SQLAlchemy 命名约定 ---
# 定义一个命名约定字典，用于 SQLAlchemy 自动生成约束名称 (如索引、外键等)
# 这有助于 Alembic 生成一致的、可预测的迁移脚本名称。
convention = {
    "ix": "ix_%(column_0_label)s",  # 索引名称格式
    "uq": "uq_%(table_name)s_%(column_0_name)s",  # 唯一约束名称格式
    "ck": "ck_%(table_name)s_%(constraint_name)s",  # 检查约束名称格式
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",  # 外键约束名称格式
    "pk": "pk_%(table_name)s",  # 主键约束名称格式
}
# 创建一个 MetaData 实例，并应用上述命名约定
metadata_obj = MetaData(naming_convention=convention)
# 创建所有 ORM 模型的基类，并传入 metadata_obj
Base = declarative_base(metadata=metadata_obj)

# --- 异步数据库引擎和会话工厂 ---
# 创建异步数据库引擎实例
async_engine = create_async_engine(
    DATABASE_URL,  # 使用从配置中获取的数据库连接字符串
    echo=False, 
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,    # 例如 30 分钟回收一次
    pool_pre_ping=True    # ***** 启用 pre-ping *****
)

# 创建异步数据库会话的工厂 (factory)
AsyncSessionLocal = sessionmaker(
    autocommit=False,  # 禁用自动提交事务
    autoflush=False,  # 禁用自动刷新 (在查询前将挂起的更改写入数据库)
    bind=async_engine,  # 将会话工厂绑定到创建的异步引擎
    class_=AsyncSession,  # 指定使用 SQLAlchemy 的异步会话类
)


# --- 枚举类型定义 ---
# 定义交易任务的状态枚举
# 继承自 str 和 enum.Enum，使其与 Pydantic 和 SQLAlchemy 的 Enum 类型良好兼容
class TaskStatus(str, enum.Enum):
    PENDING = "pending"  # 任务已创建，等待启动
    RUNNING = "running"  # 任务正在运行中
    STOPPED = "stopped"  # 任务已被手动停止
    ERROR = "error"  # 任务在运行过程中发生错误


# --- SQLAlchemy ORM 模型定义 ---
# 定义交易任务 (TradingTask) 的数据模型
class TradingTask(Base):
    __tablename__ = "trading_tasks"  # 数据库中对应的表名

    # 定义表的列 (字段)
    id = Column(Integer, primary_key=True, index=True, comment="任务的唯一标识符，主键")
    name = Column(String(128), index=True, nullable=False, comment="任务的自定义名称")
    symbol = Column(
        String(64), nullable=False, comment="交易标的合约代码 (例如 SHFE.rb2410)"
    )
    # (可选) 更多与策略相关的字段
    # strategy_name = Column(String(128), nullable=True, comment="应用的策略名称")
    # strategy_params = Column(JSON, nullable=True, comment="策略参数 (JSON格式)")

    # 任务状态字段，使用上面定义的 TaskStatus 枚举
    # SAEnum 用于将 Python 枚举映射到数据库的 ENUM 类型 (如果数据库支持，如 PostgreSQL)
    # name="task_status_enum": 在数据库中创建的 ENUM 类型的名称 (推荐为 PostgreSQL 指定)
    # create_type=True: (对 PostgreSQL 有效) SQLAlchemy 会尝试在 `create_all` 时创建这个 ENUM 类型
    status = Column(
        SAEnum(TaskStatus, name="task_status_enum", create_type=True),
        default=TaskStatus.PENDING,
        nullable=False,
        comment="任务的当前状态 (pending, running, stopped, error)",
    )

    # 创建时间字段，数据库服务器端自动设置默认值为当前时间 (带时区)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="任务创建时间",
    )
    # 更新时间字段，数据库服务器端在记录更新时自动设置为当前时间，创建时也设为当前时间
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="任务最后更新时间",
    )

    # (可选) 如果有用户表，可以添加外键关联
    # user_id = Column(Integer, ForeignKey("users.id"), nullable=True, comment="关联的用户ID")
    # owner = relationship("User", back_populates="tasks") # 定义与 User 模型的关系

    # __repr__ 方法用于在调试时方便地打印对象信息
    def __repr__(self) -> str:
        return f"<TradingTask(id={self.id}, name='{self.name}', symbol='{self.symbol}', status='{self.status.value}')>"


# --- 数据库会话管理和初始化 ---
# FastAPI 依赖注入函数，用于在请求处理函数中获取数据库会话
async def get_db():
    """
    FastAPI 依赖项，提供一个数据库会话。
    事务的提交和回滚由调用此依赖的端点函数负责。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session  # 将会话提供给请求处理函数
            # 注意：移除了这里的 await session.commit() 和 await session.rollback()
        finally:
            await session.close()  # 确保会话最终被关闭


# 初始化数据库表的函数
async def init_db():
    """
    (重新)创建数据库中所有由 Base.metadata 定义的表。
    通常在应用启动时调用一次，或者由数据库迁移工具 (如 Alembic) 管理。
    """
    logger.info("正在检查并初始化数据库表...")
    async with async_engine.begin() as conn:  # 使用引擎开始一个事务性连接
        # await conn.run_sync(Base.metadata.drop_all) # 警告：此行会删除所有表！仅用于开发初期快速重置。
        await conn.run_sync(Base.metadata.create_all)  # 创建所有尚未存在的表
    logger.success("数据库表已成功初始化或确认存在。")
